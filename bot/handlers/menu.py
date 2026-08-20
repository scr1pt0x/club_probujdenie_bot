import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.access_control.service import grant_access
from bot.db.models import (
    Flow,
    Membership,
    MembershipStatus,
    Payment,
    PaymentStatus,
    User,
)
from bot.payments.verification import validate_remote_payment
from bot.payments.yookassa_adapter import YooKassaAdapter
from bot.repositories import flows as flow_repo
from bot.repositories import memberships as membership_repo
from bot.repositories import promos as promo_repo
from bot.repositories.users import get_or_create_user, lock_user_by_id
from bot.services.flows import get_next_paid_flow
from bot.services.memberships import compute_grace_end, evaluate_pay_later
from bot.services.payments import (
    calculate_price_rub,
    confirm_payment,
    resolve_flow_for_payment,
)
from bot.services.promos import is_promo_valid
from bot.services.settings import (
    get_effective_settings,
    get_shop_free_label,
    get_shop_prices,
)
from bot.services.texts import get_text
from bot.ui.formatters import format_flow_period, format_local_date, format_price_rub
from bot.ui.keyboards import access_links_kb, back_home_kb, main_menu_kb
from bot.ui.navigation import (
    ScreenResponder,
    edit_saved_screen,
    edit_screen,
    send_clean_screen,
)
from config import settings

router = Router()
logger = logging.getLogger(__name__)


class PromoCodeState(StatesGroup):
    waiting_code = State()


def _cancel_input_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="❌ Отменить", callback_data="user:input:cancel"
                )
            ]
        ]
    )


@router.message(Command("cancel"))
@router.callback_query(lambda c: c.data == "user:input:cancel")
async def cancel_input_handler(
    event: types.Message | types.CallbackQuery, state: FSMContext
) -> None:
    await state.clear()
    if isinstance(event, types.CallbackQuery):
        await edit_screen(
            event.message,
            "🌿 Главное меню\n\nВыберите нужный раздел:",
            reply_markup=main_menu_kb(),
        )
        await event.answer()
    else:
        await send_clean_screen(
            event,
            "🌿 Главное меню\n\nВвод отменён. Выберите нужный раздел:",
            reply_markup=main_menu_kb(),
        )


@router.callback_query(lambda c: c.data == "nav:home")
async def home_navigation_handler(
    callback: types.CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    await state.clear()
    text = await get_text(session, "start_welcome")
    await edit_screen(callback.message, text, reply_markup=main_menu_kb())
    await callback.answer()


def _payment_validation_error(remote: dict, payment: Payment) -> str | None:
    return validate_remote_payment(
        remote,
        external_id=payment.external_id or "",
        internal_payment_id=payment.id,
        user_id=payment.user_id,
        amount_rub=payment.amount_rub,
        currency=payment.currency,
    )


async def _find_paid_payment_with_active_flow(
    session: AsyncSession, user_id: int, now: datetime
) -> Payment | None:
    result = await session.execute(
        select(Payment)
        .join(Flow, Payment.flow_id == Flow.id)
        .where(Payment.user_id == user_id)
        .where(Payment.status == PaymentStatus.PAID)
        .where(Flow.is_free.is_(False))
        .where(Flow.end_at >= now)
        .order_by(Payment.paid_at.desc(), Payment.id.desc())
        .limit(1)
    )
    return result.scalars().first()


def _renewal_period_open(flow: Flow, now: datetime) -> bool:
    """
    Разрешаем продление с календарной даты «за 7 дней до старта» (как рассылки -7)
    до конца окна продаж — без привязки к часу sales_open_at в UTC.

    Иначе сообщение «платите» и кнопка «Моя оплата» могут расходиться с полем
    sales_open/sales_close, посчитанным от utc-старта пока есть сдвиг по дню.
    """
    tz = ZoneInfo(settings.scheduler_timezone)
    today_local = now.astimezone(tz).date()
    start_local = flow.start_at.astimezone(tz).date()
    close_local = flow.sales_close_at.astimezone(tz).date()
    from_date = start_local - timedelta(days=7)
    return from_date <= today_local <= close_local


async def _should_offer_renewal_checkout(
    session: AsyncSession, user_id: int, now: datetime
) -> bool:
    """
    True — показать оплату продления: есть следующий платный поток, набор открыт,
    по нему ещё нет подтверждённой оплаты.
    """
    next_paid = await flow_repo.get_next_paid_flow(session, now)
    if next_paid is None:
        return False
    paid_next = await session.execute(
        select(Payment.id)
        .where(Payment.user_id == user_id)
        .where(Payment.flow_id == next_paid.id)
        .where(Payment.status == PaymentStatus.PAID)
        .limit(1)
    )
    if paid_next.scalar_one_or_none() is not None:
        return False
    return _renewal_period_open(next_paid, now)


async def _send_paid_access_links(
    session: AsyncSession, responder: ScreenResponder, tg_id: int
) -> None:
    links = await grant_access(responder.bot, tg_id)
    kb = access_links_kb(links.channel_link, links.group_link)
    if kb is None:
        await responder.answer("Оплата уже подтверждена. Доступ активирован.")
        return
    await responder.answer(
        "Оплата уже подтверждена. Доступ активирован.\n"
        "Нажмите кнопки ниже и отправьте заявку на вступление.",
        reply_markup=kb,
    )


async def _resolve_free_access_flow(
    session: AsyncSession, user_id: int, now: datetime
) -> int | None:
    active_membership = await membership_repo.get_active_membership(session, user_id)

    # Продлевающие: приоритет на следующий платный, затем текущий платный.
    if active_membership is not None:
        next_paid = await flow_repo.get_next_paid_flow(session, now)
        if next_paid:
            return next_paid.id
        active_paid = await flow_repo.get_active_paid_flow(session, now)
        if active_paid:
            return active_paid.id
        return active_membership.flow_id

    # Новые участницы: сначала платный поток, затем бесплатный (если включён).
    next_paid = await flow_repo.get_next_paid_flow(session, now)
    if next_paid:
        return next_paid.id
    active_paid = await flow_repo.get_active_paid_flow(session, now)
    if active_paid:
        return active_paid.id
    if settings.free_flows_enabled:
        next_free = await flow_repo.get_next_free_flow(session, now)
        if next_free:
            return next_free.id
        active_free = await flow_repo.get_active_free_flow(session, now)
        if active_free:
            return active_free.id
    return None


async def _send_personal_payment_link(
    session: AsyncSession, tg_user: types.User, responder: ScreenResponder
) -> None:
    now = datetime.now(timezone.utc)
    user = await get_or_create_user(
        session=session,
        tg_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
        is_admin=tg_user.id in settings.admin_tg_ids,
    )
    await session.flush()
    # Serialize payment decisions per user. Rapid double taps must not create
    # two YooKassa orders before either request sees the other's PENDING row.
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())

    # Оплата за текущий ещё действующий поток: обычно только повторяем ссылки.
    # Если открыт следующий поток и за него не платили, выставляем продление.
    if await _find_paid_payment_with_active_flow(session, user.id, now) is not None:
        if not await _should_offer_renewal_checkout(session, user.id, now):
            await session.commit()
            await _send_paid_access_links(session, responder, tg_user.id)
            return

    latest_membership = await membership_repo.get_latest_membership(session, user.id)
    if (
        latest_membership is not None
        and latest_membership.status != MembershipStatus.ACTIVE
    ):
        last_flow = await flow_repo.get_flow_by_id(session, latest_membership.flow_id)
        if (
            last_flow is not None
            and last_flow.is_free
            and latest_membership.pay_later_used_at is None
        ):
            await responder.answer(
                "Бесплатный поток уже завершен, а отсрочка не была оформлена вовремя.\n"
                "Сейчас доступ можно получить только после оплаты полной стоимости."
            )

    price = await calculate_price_rub(session, user_id=user.id, paid_at=now)
    if price <= 0:
        flow_id = await _resolve_free_access_flow(session, user.id, now)
        if flow_id is None:
            await responder.answer(await get_text(session, "payment_needs_review"))
            return
        payment = Payment(
            user_id=user.id,
            provider="promo",
            status=PaymentStatus.PENDING,
            amount_rub=0,
            currency="RUB",
            flow_id=flow_id,
        )
        session.add(payment)
        await session.flush()
        links = await confirm_payment(
            session, responder.bot, payment, paid_at=now, notify_user=False
        )
        await session.commit()
        kb = access_links_kb(
            links.channel_link if links else None,
            links.group_link if links else None,
        )
        text = await get_text(
            session, "payment_success" if kb else "payment_success_no_links"
        )
        await responder.answer(text, reply_markup=kb or back_home_kb())
        return

    existing_pending = (
        await session.execute(
            select(Payment)
            .where(Payment.user_id == user.id)
            .where(Payment.status == PaymentStatus.PENDING)
            .where(Payment.external_id.is_not(None))
            .where(Payment.amount_rub == price)
            .order_by(Payment.created_at.desc())
            .with_for_update()
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing_pending is not None:
        adapter = YooKassaAdapter()
        try:
            remote = await adapter.get_payment(existing_pending.external_id)
            if _payment_validation_error(remote, existing_pending):
                await responder.answer(
                    "Не удалось безопасно подтвердить принадлежность счёта. "
                    "Создаю новый платёж."
                )
                existing_pending.status = PaymentStatus.FAILED
                await session.flush()
            else:
                remote_status = remote.get("status")
                if remote_status == "succeeded":
                    links = await confirm_payment(
                        session,
                        responder.bot,
                        existing_pending,
                        paid_at=now,
                        notify_user=False,
                    )
                    await session.commit()
                    kb = access_links_kb(
                        links.channel_link if links else None,
                        links.group_link if links else None,
                    )
                    text = await get_text(
                        session,
                        "payment_success" if kb else "payment_success_no_links",
                    )
                    await responder.answer(text, reply_markup=kb or back_home_kb())
                    return
                if remote_status in ("canceled", "expired"):
                    existing_pending.status = PaymentStatus.FAILED
                    await session.flush()
                elif remote_status == "pending":
                    conf = remote.get("confirmation", {})
                    url = conf.get("confirmation_url")
                    if url:
                        keyboard = types.InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    types.InlineKeyboardButton(
                                        text="💳 Перейти к оплате", url=url
                                    )
                                ],
                                [
                                    types.InlineKeyboardButton(
                                        text="🔄 Проверить оплату",
                                        callback_data="payment:refresh",
                                    )
                                ],
                                [
                                    types.InlineKeyboardButton(
                                        text="← Главное меню", callback_data="nav:home"
                                    )
                                ],
                            ]
                        )
                        await responder.answer(
                            "💳 У вас уже есть активный счёт\n\n"
                            f"Сумма: {format_price_rub(price)} ₽\n"
                            "Если вы уже оплатили, проверка обычно занимает до минуты.",
                            reply_markup=keyboard,
                        )
                        return
                    await responder.answer(
                        "Платёжный сервис не вернул ссылку на действующий счёт. "
                        "Не создавайте повторную оплату и попробуйте позже.",
                        reply_markup=back_home_kb(),
                    )
                    return
                else:
                    await responder.answer(
                        "Платёжный сервис вернул неизвестный статус. "
                        "Не создавайте повторную оплату и попробуйте позже.",
                        reply_markup=back_home_kb(),
                    )
                    return
        except Exception:
            await session.rollback()
            logger.exception("Failed to reuse existing YooKassa payment")
            await responder.answer(
                "Не удалось связаться с платёжным сервисом. Существующий счёт "
                "не изменён — попробуйте снова через минуту.",
                reply_markup=back_home_kb(),
            )
            return

    target_flow_id = await resolve_flow_for_payment(session, now)
    if target_flow_id is None:
        await responder.answer(
            "Сейчас набор закрыт, поэтому новый счёт не создаётся. "
            "Дата следующего набора появится в расписании.",
            reply_markup=back_home_kb(),
        )
        return

    payment = Payment(
        user_id=user.id,
        provider="yookassa",
        status=PaymentStatus.PENDING,
        amount_rub=price,
        currency="RUB",
        flow_id=target_flow_id,
        expires_at=now + timedelta(hours=1),
    )
    session.add(payment)
    await session.flush()

    adapter = YooKassaAdapter()
    description = "Оплата участия в Клубе Пробуждение"
    try:
        payment_id, confirmation_url = await adapter.create_payment(
            amount_rub=price,
            description=description,
            metadata={"user_id": user.id, "internal_payment_id": payment.id},
            internal_payment_id=payment.id,
        )
        payment.external_id = payment_id
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Failed to create YooKassa payment")
        await responder.answer(
            "Платёжный сервис временно недоступен. Счёт не создан и списания "
            "не будет — попробуйте снова позже.",
            reply_markup=back_home_kb(),
        )
        return

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="💳 Перейти к оплате", url=confirmation_url
                )
            ],
            [
                types.InlineKeyboardButton(
                    text="🔄 Проверить оплату",
                    callback_data="payment:refresh",
                )
            ],
            [
                types.InlineKeyboardButton(
                    text="← Главное меню", callback_data="nav:home"
                )
            ],
        ]
    )
    await responder.answer(
        "💳 Счёт готов\n\n"
        f"Ваша стоимость: {format_price_rub(price)} ₽\n"
        "После оплаты доступ появится автоматически. Если банк задержит статус, "
        "используйте кнопку проверки.",
        reply_markup=keyboard,
    )


@router.message(lambda m: m.text == "💳 Моя оплата")
async def pay_handler(message: types.Message, session: AsyncSession) -> None:
    await _send_personal_payment_link(
        session, message.from_user, ScreenResponder(message, edit_existing=False)
    )


@router.callback_query(lambda c: c.data == "nav:payment")
async def payment_navigation_handler(callback: types.CallbackQuery) -> None:
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="Продолжить к проверке и оплате",
                    callback_data="payment:start",
                )
            ],
            [
                types.InlineKeyboardButton(
                    text="← Главное меню", callback_data="nav:home"
                )
            ],
        ]
    )
    await edit_screen(
        callback.message,
        "💳 Оплата\n\n"
        "Бот сначала проверит активный доступ, доступную цену и незавершённый "
        "счёт. Новый платёж создаётся только после подтверждения ниже.",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "payment:start")
async def payment_start_handler(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    await _send_personal_payment_link(
        session,
        callback.from_user,
        ScreenResponder(callback.message, edit_existing=True),
    )
    await callback.answer()


def _shop_menu_kb(
    free_label: str, *, include_free_offer: bool
) -> types.InlineKeyboardMarkup:
    rows: list[list[types.InlineKeyboardButton]] = [
        [
            types.InlineKeyboardButton(
                text="💳 Узнать мою цену и оплатить",
                callback_data="shop:order:personal",
            )
        ]
    ]
    if include_free_offer:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=f"🎁 Бесплатный поток {free_label}",
                    callback_data="shop:free",
                )
            ]
        )
    rows.append(
        [types.InlineKeyboardButton(text="← Главное меню", callback_data="nav:home")]
    )
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _shop_order_kb(order_key: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="✅ Оформить заказ", callback_data=f"shop:order:{order_key}"
                )
            ],
            [types.InlineKeyboardButton(text="← Назад", callback_data="nav:shop")],
        ]
    )


async def _shop_content(
    session: AsyncSession,
) -> tuple[str, types.InlineKeyboardMarkup]:
    prices = await get_shop_prices(session)
    free_label = await get_shop_free_label(session)
    title = await get_text(session, "shop_title")
    free_desc = await get_text(session, "shop_free_desc")
    lines = [
        title,
        "",
        f"✨ Первое участие — {format_price_rub(prices['intro'])} ₽",
        f"🔁 Непрерывное продление — {format_price_rub(prices['renewal'])} ₽",
    ]
    if settings.free_flows_enabled:
        lines.append(f"🎁 {free_desc} — {free_label}")
    lines.extend(
        [
            "",
            "Персональная сумма рассчитывается автоматически с учётом истории "
            "участия и промокода. Счёт появится только после подтверждения.",
        ]
    )
    return "\n".join(lines), _shop_menu_kb(
        free_label, include_free_offer=settings.free_flows_enabled
    )


@router.message(lambda m: m.text == "🛍 Тарифы")
async def shop_handler(message: types.Message, session: AsyncSession) -> None:
    text, keyboard = await _shop_content(session)
    await send_clean_screen(message, text, reply_markup=keyboard)


@router.callback_query(lambda c: c.data == "nav:shop")
async def shop_navigation_handler(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    text, keyboard = await _shop_content(session)
    await edit_screen(callback.message, text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(lambda c: c.data == "shop:pay:intro")
async def shop_intro_detail(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    prices = await get_shop_prices(session)
    intro_desc = await get_text(session, "shop_intro_desc")
    flow = await get_next_paid_flow(session, datetime.now(timezone.utc))
    flow_info = (
        f"\nБлижайший поток: {format_flow_period(flow.start_at, flow.end_at)}"
        if flow
        else ""
    )
    await edit_screen(
        callback.message,
        f"{intro_desc} — базовая цена {format_price_rub(prices['intro'])} ₽\n"
        "Доступ: канал + группа\n"
        "Длительность: 5 недель"
        f"{flow_info}\n"
        "Перед оплатой бот рассчитает вашу персональную цену.",
        reply_markup=_shop_order_kb("personal"),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "shop:pay:renewal")
async def shop_renewal_detail(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    prices = await get_shop_prices(session)
    renewal_desc = await get_text(session, "shop_renewal_desc")
    flow = await get_next_paid_flow(session, datetime.now(timezone.utc))
    flow_info = (
        f"\nБлижайший поток: {format_flow_period(flow.start_at, flow.end_at)}"
        if flow
        else ""
    )
    await edit_screen(
        callback.message,
        f"{renewal_desc} — базовая цена {format_price_rub(prices['renewal'])} ₽\n"
        "Доступ: канал + группа\n"
        "Длительность: 5 недель"
        f"{flow_info}\n"
        "Перед оплатой бот проверит право на цену продления.",
        reply_markup=_shop_order_kb("personal"),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "shop:free")
async def shop_free_detail(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    if not settings.free_flows_enabled:
        await edit_screen(
            callback.message,
            await get_text(session, "free_access_disabled"),
            reply_markup=back_home_kb(),
        )
        await callback.answer()
        return
    free_label = await get_shop_free_label(session)
    free_desc = await get_text(session, "shop_free_desc")
    await edit_screen(
        callback.message,
        f"{free_desc} — {free_label}\n"
        "Бесплатный вход открывается только в объявленные даты. "
        "Участие в основном потоке доступно в разделе «✨ Участие».",
        reply_markup=back_home_kb(),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "shop:checkout:intro")
async def shop_checkout_intro(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    prices = await get_shop_prices(session)
    order_text = await get_text(session, "shop_order_text")
    await edit_screen(
        callback.message,
        f"{order_text}\nБазовая цена: {format_price_rub(prices['intro'])} ₽\n"
        "Точная сумма будет рассчитана перед созданием счёта.",
        reply_markup=_shop_order_kb("personal"),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "shop:checkout:renewal")
async def shop_checkout_renewal(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    prices = await get_shop_prices(session)
    order_text = await get_text(session, "shop_order_text")
    await edit_screen(
        callback.message,
        f"{order_text}\nЦена продления: {format_price_rub(prices['renewal'])} ₽\n"
        "Бот сначала проверит, доступна ли она вашему аккаунту.",
        reply_markup=_shop_order_kb("personal"),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "shop:order:intro")
async def shop_order_intro(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    await _send_personal_payment_link(
        session,
        callback.from_user,
        ScreenResponder(callback.message, edit_existing=True),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "shop:order:renewal")
async def shop_order_renewal(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    await _send_personal_payment_link(
        session,
        callback.from_user,
        ScreenResponder(callback.message, edit_existing=True),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "shop:order:personal")
async def shop_order_personal(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    await _send_personal_payment_link(
        session,
        callback.from_user,
        ScreenResponder(callback.message, edit_existing=True),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "payment:refresh")
async def payment_refresh_handler(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    responder = ScreenResponder(callback.message, edit_existing=True)
    now = datetime.now(timezone.utc)
    user = await get_or_create_user(
        session=session,
        tg_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
        is_admin=callback.from_user.id in settings.admin_tg_ids,
    )
    await session.commit()
    user = await lock_user_by_id(session, user.id)
    if user is None:
        await responder.answer(
            "Профиль не найден. Вернитесь в главное меню и повторите попытку.",
            reply_markup=back_home_kb(),
        )
        await callback.answer()
        return

    pending_payment = (
        await session.execute(
            select(Payment)
            .where(Payment.user_id == user.id)
            .where(Payment.status == PaymentStatus.PENDING)
            .where(Payment.external_id.is_not(None))
            .order_by(Payment.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if pending_payment is None:
        if await _find_paid_payment_with_active_flow(session, user.id, now) is not None:
            if await _should_offer_renewal_checkout(session, user.id, now):
                await _send_personal_payment_link(
                    session,
                    callback.from_user,
                    ScreenResponder(callback.message, edit_existing=True),
                )
                await callback.answer()
                return
            await session.commit()
            await _send_paid_access_links(
                session,
                ScreenResponder(callback.message, edit_existing=True),
                callback.from_user.id,
            )
            await callback.answer("Оплата уже подтверждена")
            return
        await responder.answer(
            "Активных счетов нет. Вернитесь в раздел оплаты, чтобы проверить "
            "условия и создать новый.",
            reply_markup=back_home_kb(),
        )
        await callback.answer()
        return

    adapter = YooKassaAdapter()
    try:
        remote = await adapter.get_payment(pending_payment.external_id)
    except Exception:
        logger.exception(
            "Failed to refresh YooKassa payment",
            extra={"payment_id": pending_payment.id},
        )
        await responder.answer(
            "Не удалось получить статус от платёжного сервиса. "
            "Повторите проверку через минуту.",
            reply_markup=back_home_kb(),
        )
        await callback.answer()
        return

    remote_status = remote.get("status")
    if _payment_validation_error(remote, pending_payment):
        pending_payment.status = PaymentStatus.FAILED
        await session.commit()
        await responder.answer(
            "Не удалось безопасно сопоставить счёт с вашим профилем. "
            "Старый счёт закрыт; новый создастся только после подтверждения.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text="Создать новый счёт", callback_data="payment:start"
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text="← Главное меню", callback_data="nav:home"
                        )
                    ],
                ]
            ),
        )
        await callback.answer()
        return

    if remote_status == "succeeded":
        links = await confirm_payment(
            session,
            callback.message.bot,
            pending_payment,
            paid_at=now,
            notify_user=False,
        )
        await session.commit()
        kb = access_links_kb(
            links.channel_link if links else None,
            links.group_link if links else None,
        )
        text = await get_text(
            session, "payment_success" if kb else "payment_success_no_links"
        )
        await responder.answer(text, reply_markup=kb or back_home_kb())
        await callback.answer("Оплата подтверждена")
        return

    if remote_status in ("canceled", "expired"):
        pending_payment.status = PaymentStatus.FAILED
        await session.commit()
        await responder.answer(
            "Этот счёт отменён платёжным сервисом. Новый счёт создастся только "
            "после вашего подтверждения.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text="Создать новый счёт", callback_data="payment:start"
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text="← Главное меню", callback_data="nav:home"
                        )
                    ],
                ]
            ),
        )
        await callback.answer()
        return

    conf = remote.get("confirmation", {})
    url = conf.get("confirmation_url")
    if remote_status == "pending" and url:
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text="💳 Перейти к оплате", url=url)],
                [
                    types.InlineKeyboardButton(
                        text="🔄 Проверить оплату",
                        callback_data="payment:refresh",
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text="← Главное меню", callback_data="nav:home"
                    )
                ],
            ]
        )
        await session.commit()
        await responder.answer(
            "⏳ Оплата пока не подтверждена банком.\n"
            "Если вы уже оплатили, повторите проверку через 30–60 секунд.",
            reply_markup=keyboard,
        )
        await callback.answer()
        return

    await session.commit()
    await responder.answer(
        "Платёжный сервис вернул неизвестный статус. Не создавайте повторный "
        "платёж и попробуйте проверку позже.",
        reply_markup=back_home_kb(),
    )
    await callback.answer()


@router.message(lambda m: m.text == "🎟 Получить доступ")
async def access_handler(message: types.Message, session: AsyncSession) -> None:
    responder = ScreenResponder(message, edit_existing=False)
    if not settings.free_flows_enabled:
        await responder.answer(
            await get_text(session, "free_access_disabled"),
            reply_markup=back_home_kb(),
        )
        return
    now = datetime.now(timezone.utc)
    user = await get_or_create_user(
        session=session,
        tg_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        is_admin=message.from_user.id in settings.admin_tg_ids,
    )
    await session.commit()

    flow = await flow_repo.get_active_free_flow(session, now)
    if flow is None:
        flow = await flow_repo.get_next_free_flow(session, now)
    if flow is None:
        await responder.answer(
            await get_text(session, "sales_closed"), reply_markup=back_home_kb()
        )
        return
    if now < flow.sales_open_at:
        await responder.answer(
            await get_text(session, "sales_not_open"), reply_markup=back_home_kb()
        )
        return
    if now > flow.sales_close_at:
        await responder.answer(
            await get_text(session, "sales_closed"), reply_markup=back_home_kb()
        )
        return

    existing = await membership_repo.get_membership_by_flow(
        session, user_id=user.id, flow_id=flow.id
    )
    if (
        existing
        and existing.status == MembershipStatus.ACTIVE
        and existing.access_end_at >= now
    ):
        await responder.answer(
            await get_text(session, "access_already_in"),
            reply_markup=back_home_kb(),
        )
        return

    effective = await get_effective_settings(session)
    if existing:
        membership = existing
        membership.status = MembershipStatus.ACTIVE
        membership.access_start_at = flow.start_at
        membership.access_end_at = flow.end_at
        membership.grace_end_at = compute_grace_end(flow.end_at, effective.grace_days)
        membership.pay_later_used_at = None
        membership.pay_later_deadline_at = None
    else:
        membership = Membership(
            user_id=user.id,
            flow_id=flow.id,
            status=MembershipStatus.ACTIVE,
            access_start_at=flow.start_at,
            access_end_at=flow.end_at,
            grace_end_at=compute_grace_end(flow.end_at, effective.grace_days),
        )
        session.add(membership)
    await session.commit()
    links = await grant_access(message.bot, message.from_user.id)
    text = await get_text(session, "access_granted_free")
    kb = access_links_kb(links.channel_link, links.group_link)
    if kb is None:
        await responder.answer(text, reply_markup=back_home_kb())
        return
    await responder.answer(
        f"{text}\n\nНажмите кнопки ниже и отправьте заявку на вступление.",
        reply_markup=kb,
    )


@router.message(lambda m: m.text == "⏳ Оплачу позже")
async def pay_later_menu_handler(message: types.Message, session: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    user = await get_or_create_user(
        session=session,
        tg_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        is_admin=message.from_user.id in settings.admin_tg_ids,
    )
    await session.commit()

    eligibility = await evaluate_pay_later(session, user.id, now)
    if eligibility.eligible and eligibility.deadline:
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text="Подтвердить отсрочку", callback_data="pay_later"
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text="← Главное меню", callback_data="nav:home"
                    )
                ],
            ]
        )
        text = (
            "⏳ Оплатить позже\n\n"
            f"Доступ сохранится до {format_local_date(eligibility.deadline)}.\n"
            "После этой даты при отсутствии оплаты бот исключит вас из канала "
            "и группы. Действие включится только после подтверждения."
        )
    else:
        reason = eligibility.message.replace("Опция недоступна: ", "")
        text = f"⏳ Отсрочка сейчас недоступна\n\n{reason}"
        keyboard = back_home_kb()
    await send_clean_screen(message, text, reply_markup=keyboard)


@router.callback_query(lambda c: c.data == "nav:pay_later")
async def pay_later_navigation_handler(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    now = datetime.now(timezone.utc)
    user = await get_or_create_user(
        session=session,
        tg_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
        is_admin=callback.from_user.id in settings.admin_tg_ids,
    )
    await session.commit()
    eligibility = await evaluate_pay_later(session, user.id, now)
    if eligibility.eligible and eligibility.deadline:
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text="Подтвердить отсрочку", callback_data="pay_later"
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text="← Главное меню", callback_data="nav:home"
                    )
                ],
            ]
        )
        text = (
            "⏳ Оплатить позже\n\n"
            f"Доступ сохранится до {format_local_date(eligibility.deadline)}.\n"
            "После этой даты при отсутствии оплаты доступ будет отключён. "
            "Подтвердите действие ниже."
        )
    else:
        reason = eligibility.message.replace("Опция недоступна: ", "")
        text = f"⏳ Отсрочка сейчас недоступна\n\n{reason}"
        keyboard = back_home_kb()
    await edit_screen(callback.message, text, reply_markup=keyboard)
    await callback.answer()


async def _schedule_content(session: AsyncSession) -> str:
    now = datetime.now(timezone.utc)
    if settings.free_flows_enabled:
        flow = await flow_repo.get_active_free_flow(session, now)
        if flow is None:
            flow = await flow_repo.get_active_paid_flow(session, now)
        if flow is None:
            flow = await flow_repo.get_next_free_flow(session, now)
        if flow is None:
            flow = await get_next_paid_flow(session, now)
    else:
        flow = await flow_repo.get_active_paid_flow(session, now)
        if flow is None:
            flow = await get_next_paid_flow(session, now)
    if flow is None:
        return "🗓 Расписание\n\nНовый поток пока не запланирован."

    kind = "Бесплатный" if flow.is_free else "Основной"
    sales_status = (
        "Набор открыт"
        if flow.sales_open_at <= now <= flow.sales_close_at
        else "Набор закрыт"
    )
    template = await get_text(session, "schedule_text")
    try:
        return template.format(
            kind=kind,
            start=format_local_date(flow.start_at),
            end=format_local_date(flow.end_at),
            sales_status=sales_status,
        )
    except (KeyError, ValueError):
        return (
            f"🗓 {kind} поток\n\n"
            f"Старт: {format_local_date(flow.start_at)}\n"
            f"Окончание: {format_local_date(flow.end_at)}\n"
            f"{sales_status}"
        )


@router.message(lambda m: m.text == "📅 Расписание")
async def schedule_handler(message: types.Message, session: AsyncSession) -> None:
    await send_clean_screen(
        message, await _schedule_content(session), reply_markup=back_home_kb()
    )


@router.callback_query(lambda c: c.data == "nav:schedule")
async def schedule_navigation_handler(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    await edit_screen(
        callback.message,
        await _schedule_content(session),
        reply_markup=back_home_kb(),
    )
    await callback.answer()


@router.message(lambda m: m.text == "ℹ️ Помощь")
async def help_handler(message: types.Message, session: AsyncSession) -> None:
    await send_clean_screen(
        message, await get_text(session, "help_text"), reply_markup=back_home_kb()
    )


@router.callback_query(lambda c: c.data == "nav:help")
async def help_navigation_handler(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    await edit_screen(
        callback.message,
        await get_text(session, "help_text"),
        reply_markup=back_home_kb(),
    )
    await callback.answer()


@router.message(lambda m: m.text == "🏷 Промокод")
async def promo_code_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    user = await get_or_create_user(
        session=session,
        tg_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        is_admin=message.from_user.id in settings.admin_tg_ids,
    )
    await session.commit()
    await state.set_state(PromoCodeState.waiting_code)
    screen = await send_clean_screen(
        message, "Введите промокод одним сообщением.", reply_markup=_cancel_input_kb()
    )
    await state.update_data(user_id=user.id, screen_message_id=screen.message_id)


@router.callback_query(lambda c: c.data == "nav:promo")
async def promo_code_navigation_handler(
    callback: types.CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    user = await get_or_create_user(
        session=session,
        tg_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
        is_admin=callback.from_user.id in settings.admin_tg_ids,
    )
    await session.commit()
    await state.set_state(PromoCodeState.waiting_code)
    screen = await edit_screen(
        callback.message,
        "🏷 Промокод\n\nОтправьте код одним сообщением.",
        reply_markup=_cancel_input_kb(),
    )
    await state.update_data(user_id=user.id, screen_message_id=screen.message_id)
    await callback.answer()


@router.message(PromoCodeState.waiting_code)
async def promo_code_apply_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    code = (message.text or "").strip().upper()
    data = await state.get_data()
    screen_message_id = data.get("screen_message_id")
    if not code:
        await edit_saved_screen(
            message,
            screen_message_id,
            "Введите непустой промокод.",
            reply_markup=_cancel_input_kb(),
        )
        return

    user_id = data.get("user_id")
    if not user_id:
        await state.clear()
        await edit_saved_screen(
            message,
            screen_message_id,
            "Не удалось определить пользователя. Откройте меню заново.",
            reply_markup=back_home_kb(),
        )
        return

    promo = await promo_repo.get_promo_by_code(session, code)
    if not promo:
        await state.clear()
        await edit_saved_screen(
            message,
            screen_message_id,
            "Промокод не найден или уже неактивен.",
            reply_markup=back_home_kb(),
        )
        return
    now = datetime.now(timezone.utc)
    if not is_promo_valid(promo, now):
        await state.clear()
        await edit_saved_screen(
            message,
            screen_message_id,
            "Промокод не найден или уже неактивен.",
            reply_markup=back_home_kb(),
        )
        return

    existing = await promo_repo.get_user_promo(session, user_id, code)
    if existing:
        await state.clear()
        await edit_saved_screen(
            message,
            screen_message_id,
            "✅ Этот промокод уже применён к вашему профилю.",
            reply_markup=back_home_kb(),
        )
        return

    latest = await promo_repo.get_latest_user_promo(session, user_id)
    applied = await promo_repo.add_user_promo(session, user_id, code)
    if not applied:
        await session.rollback()
        await state.clear()
        await edit_saved_screen(
            message,
            screen_message_id,
            "Лимит промокода исчерпан или он больше не активен.",
            reply_markup=back_home_kb(),
        )
        return
    await session.commit()
    await state.clear()
    prefix = "Предыдущий промокод заменён.\n\n" if latest else ""
    screen = await edit_saved_screen(
        message,
        screen_message_id,
        f"✅ {prefix}Промокод применён. Рассчитываю стоимость…",
        reply_markup=back_home_kb(),
    )
    await _send_personal_payment_link(
        session, message.from_user, ScreenResponder(screen, edit_existing=True)
    )
