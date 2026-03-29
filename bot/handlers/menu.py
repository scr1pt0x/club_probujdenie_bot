from datetime import datetime, timedelta, timezone

from aiogram import Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from bot.repositories import flows as flow_repo
from bot.repositories import memberships as membership_repo
from bot.repositories import promos as promo_repo
from bot.repositories.users import get_or_create_user
from bot.services.flows import get_next_paid_flow
from bot.services.memberships import compute_grace_end
from bot.services.memberships import apply_pay_later
from bot.services.payments import calculate_price_rub, confirm_payment
from bot.payments.yookassa_adapter import YooKassaAdapter
from bot.services.promos import is_promo_valid
from bot.services.settings import (
    get_effective_settings,
    get_shop_free_label,
    get_shop_prices,
)
from bot.services.texts import get_text
from bot.access_control.service import grant_access
from bot.db.models import Membership, MembershipStatus, Payment, PaymentStatus
from config import settings



router = Router()


class PromoCodeState(StatesGroup):
    waiting_code = State()


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

    # Новые участницы: сначала платный поток, если его нет — бесплатный.
    next_paid = await flow_repo.get_next_paid_flow(session, now)
    if next_paid:
        return next_paid.id
    active_paid = await flow_repo.get_active_paid_flow(session, now)
    if active_paid:
        return active_paid.id
    next_free = await flow_repo.get_next_free_flow(session, now)
    if next_free:
        return next_free.id
    active_free = await flow_repo.get_active_free_flow(session, now)
    if active_free:
        return active_free.id
    return None


async def _send_personal_payment_link(
    session: AsyncSession, tg_user: types.User, responder: types.Message
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
    await session.commit()

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
        await confirm_payment(session, responder.bot, payment, paid_at=now)
        await session.commit()
        return

    existing_pending = (
        await session.execute(
            select(Payment)
            .where(Payment.user_id == user.id)
            .where(Payment.status == PaymentStatus.PENDING)
            .where(Payment.external_id.is_not(None))
            .where(Payment.amount_rub == price)
            .order_by(Payment.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing_pending is not None:
        await confirm_payment(session, responder.bot, existing_pending, paid_at=now)
        await session.commit()
        return

    payment = Payment(
        user_id=user.id,
        provider="yookassa",
        status=PaymentStatus.PENDING,
        amount_rub=price,
        currency="RUB",
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
        payment.status = PaymentStatus.FAILED
        await session.commit()
        await responder.answer(await get_text(session, "payment_failed"))
        return

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="🔗 Оплатить", url=confirmation_url)]
        ]
    )
    await responder.answer(
        f"Ваша персональная стоимость участия: {price} ₽",
        reply_markup=keyboard,
    )


@router.message(lambda m: m.text == "💳 Моя оплата")
async def pay_handler(message: types.Message, session: AsyncSession) -> None:
    await _send_personal_payment_link(session, message.from_user, message)


def _format_price(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _shop_menu_kb(
    prices: dict[str, int], free_label: str
) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=f"💳 Оплатить {_format_price(prices['intro'])} ₽",
                    callback_data="shop:pay:intro",
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=f"💳 Оплатить {_format_price(prices['renewal'])} ₽",
                    callback_data="shop:pay:renewal",
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=f"🎁 Бесплатный поток {free_label}",
                    callback_data="shop:free",
                )
            ],
        ]
    )


def _shop_checkout_kb(key: str, price_text: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=f"💳 Оплатить {price_text}",
                    callback_data=f"shop:checkout:{key}",
                )
            ]
        ]
    )


def _shop_order_kb(order_key: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="✅ Оформить заказ", callback_data=f"shop:order:{order_key}"
                )
            ]
        ]
    )


def _access_links_kb(channel_link: str | None, group_link: str | None) -> types.InlineKeyboardMarkup | None:
    rows: list[list[types.InlineKeyboardButton]] = []
    if channel_link:
        rows.append(
            [types.InlineKeyboardButton(text="📢 Войти в канал", url=channel_link)]
        )
    if group_link:
        rows.append(
            [types.InlineKeyboardButton(text="💬 Войти в группу", url=group_link)]
        )
    if not rows:
        return None
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(lambda m: m.text == "🛍 Тарифы")
async def shop_handler(message: types.Message, session: AsyncSession) -> None:
    prices = await get_shop_prices(session)
    free_label = await get_shop_free_label(session)
    title = await get_text(session, "shop_title")
    intro_desc = await get_text(session, "shop_intro_desc")
    renewal_desc = await get_text(session, "shop_renewal_desc")
    free_desc = await get_text(session, "shop_free_desc")
    await message.answer(
        f"{title}\n"
        f"- {intro_desc} — {_format_price(prices['intro'])} ₽\n"
        f"- {renewal_desc} — {_format_price(prices['renewal'])} ₽\n"
        f"- {free_desc} — {free_label}",
        reply_markup=_shop_menu_kb(prices, free_label),
    )


@router.callback_query(lambda c: c.data == "shop:pay:intro")
async def shop_intro_detail(callback: types.CallbackQuery, session: AsyncSession) -> None:
    prices = await get_shop_prices(session)
    intro_desc = await get_text(session, "shop_intro_desc")
    flow = await get_next_paid_flow(session, datetime.now(timezone.utc))
    flow_info = (
        f"\nБлижайший поток: {flow.start_at.date()} → {flow.end_at.date()}"
        if flow
        else ""
    )
    await callback.message.answer(
        f"{intro_desc} — {_format_price(prices['intro'])} ₽\n"
        "Доступ: канал + группа\n"
        "Длительность: 5 недель"
        f"{flow_info}\n"
        "Далее нажмите оплатить",
        reply_markup=_shop_checkout_kb("intro", f"{_format_price(prices['intro'])} ₽"),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "shop:pay:renewal")
async def shop_renewal_detail(callback: types.CallbackQuery, session: AsyncSession) -> None:
    prices = await get_shop_prices(session)
    renewal_desc = await get_text(session, "shop_renewal_desc")
    flow = await get_next_paid_flow(session, datetime.now(timezone.utc))
    flow_info = (
        f"\nБлижайший поток: {flow.start_at.date()} → {flow.end_at.date()}"
        if flow
        else ""
    )
    await callback.message.answer(
        f"{renewal_desc} — {_format_price(prices['renewal'])} ₽\n"
        "Доступ: канал + группа\n"
        "Длительность: 5 недель"
        f"{flow_info}\n"
        "Далее нажмите оплатить",
        reply_markup=_shop_checkout_kb(
            "renewal", f"{_format_price(prices['renewal'])} ₽"
        ),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "shop:free")
async def shop_free_detail(callback: types.CallbackQuery, session: AsyncSession) -> None:
    free_label = await get_shop_free_label(session)
    free_desc = await get_text(session, "shop_free_desc")
    await callback.message.answer(
        f"{free_desc} — {free_label}\n"
        "Для участия используйте кнопку «🎟 Получить доступ»."
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "shop:checkout:intro")
async def shop_checkout_intro(callback: types.CallbackQuery, session: AsyncSession) -> None:
    prices = await get_shop_prices(session)
    order_text = await get_text(session, "shop_order_text")
    await callback.message.answer(
        f"{order_text}\nК оплате: {_format_price(prices['intro'])} ₽",
        reply_markup=_shop_order_kb("intro"),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "shop:checkout:renewal")
async def shop_checkout_renewal(callback: types.CallbackQuery, session: AsyncSession) -> None:
    prices = await get_shop_prices(session)
    order_text = await get_text(session, "shop_order_text")
    await callback.message.answer(
        f"{order_text}\nК оплате: {_format_price(prices['renewal'])} ₽",
        reply_markup=_shop_order_kb("renewal"),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "shop:order:intro")
async def shop_order_intro(callback: types.CallbackQuery, session: AsyncSession) -> None:
    await _send_personal_payment_link(session, callback.from_user, callback.message)
    await callback.answer()


@router.callback_query(lambda c: c.data == "shop:order:renewal")
async def shop_order_renewal(callback: types.CallbackQuery, session: AsyncSession) -> None:
    await _send_personal_payment_link(session, callback.from_user, callback.message)
    await callback.answer()


@router.message(lambda m: m.text == "🎟 Получить доступ")
async def access_handler(message: types.Message, session: AsyncSession) -> None:
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
        await message.answer(await get_text(session, "sales_closed"))
        return
    if now < flow.sales_open_at:
        await message.answer(await get_text(session, "sales_not_open"))
        return
    if now > flow.sales_close_at:
        await message.answer(await get_text(session, "sales_closed"))
        return

    existing = await membership_repo.get_membership_by_flow(
        session, user_id=user.id, flow_id=flow.id
    )
    if (
        existing
        and existing.status == MembershipStatus.ACTIVE
        and existing.access_end_at >= now
    ):
        await message.answer(await get_text(session, "access_already_in"))
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
    kb = _access_links_kb(links.get("channel_link"), links.get("group_link"))
    if kb is None:
        await message.answer(text)
        return
    await message.answer(
        f"{text}\n\nНажмите кнопки ниже и отправьте заявку на вступление.",
        reply_markup=kb,
    )


@router.message(lambda m: m.text == "⏳ Оплачу позже")
async def pay_later_menu_handler(
    message: types.Message, session: AsyncSession
) -> None:
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

    ok, text = await apply_pay_later(session, user_id=user.id, now=now)
    if ok:
        await session.commit()
        await message.answer(text)
        return
    unavailable_text = await get_text(session, "pay_later_unavailable")
    reason = text.replace("Опция недоступна: ", "")
    await message.answer(f"{unavailable_text} ({reason})")


@router.message(lambda m: m.text == "📅 Расписание")
async def schedule_handler(
    message: types.Message, session: AsyncSession
) -> None:
    now = datetime.now(timezone.utc)
    flow = await flow_repo.get_active_free_flow(session, now)
    if flow is None:
        flow = await flow_repo.get_active_paid_flow(session, now)
    if flow is None:
        flow = await flow_repo.get_next_free_flow(session, now)
    if flow is None:
        flow = await get_next_paid_flow(session, now)

    if flow is None:
        await message.answer("Расписание пока недоступно.")
        return

    kind = "Бесплатный" if flow.is_free else "Платный"
    sales_status = (
        "Набор открыт"
        if flow.sales_open_at <= now <= flow.sales_close_at
        else "Набор закрыт"
    )
    template = await get_text(session, "schedule_text")
    try:
        text = template.format(
            kind=kind,
            start=flow.start_at.date(),
            end=flow.end_at.date(),
            sales_status=sales_status,
        )
        await message.answer(text)
    except (KeyError, ValueError):
        await message.answer(
            "⚠️ Ошибка шаблона расписания. Проверьте текст в админке."
        )
        await message.answer(
            f"{kind} поток:\n"
            f"Старт: {flow.start_at.date()}\n"
            f"Окончание: {flow.end_at.date()}\n"
            f"{sales_status}"
        )


@router.message(lambda m: m.text == "ℹ️ Помощь")
async def help_handler(message: types.Message, session: AsyncSession) -> None:
    await message.answer(await get_text(session, "help_text"))


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
    await state.update_data(user_id=user.id)
    await message.answer("Введите промокод.")


@router.message(PromoCodeState.waiting_code)
async def promo_code_apply_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    code = message.text.strip().upper()
    if not code:
        await message.answer("Введите промокод.")
        return

    data = await state.get_data()
    user_id = data.get("user_id")
    if not user_id:
        await state.clear()
        await message.answer("Пользователь не найден.")
        return

    promo = await promo_repo.get_promo_by_code(session, code)
    if not promo:
        await message.answer("Промокод не найден или не активен.")
        await state.clear()
        return
    now = datetime.now(timezone.utc)
    if not is_promo_valid(promo, now):
        await message.answer("Промокод не найден или не активен.")
        await state.clear()
        return

    existing = await promo_repo.get_user_promo(session, user_id, code)
    if existing:
        await message.answer("Промокод уже применён.")
        await state.clear()
        return

    latest = await promo_repo.get_latest_user_promo(session, user_id)
    if latest:
        await message.answer("Предыдущий промокод будет заменён новым.")

    await promo_repo.add_user_promo(session, user_id, code)
    await session.commit()
    await state.clear()
    await message.answer("✅ Промокод применен. Проверяю персональную стоимость...")
    await _send_personal_payment_link(session, message.from_user, message)
