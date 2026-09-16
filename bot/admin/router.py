import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from aiogram import Router, types
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.access_control.service import grant_access, revoke_access
from bot.admin.keyboards import (
    back_menu_kb,
    flows_edit_select_kb,
    flows_menu_kb,
    mailings_menu_kb,
    prices_menu_kb,
    promo_kind_kb,
    promos_menu_kb,
    shop_menu_kb,
    shop_texts_kb,
    template_card_kb,
    templates_list_kb,
    user_card_kb,
    users_search_kb,
)
from bot.admin.payment_reviews import payment_reviews_screen
from bot.admin.templates import DEFAULT_TEMPLATES
from bot.db.models import Flow, Membership, MembershipStatus
from bot.repositories import flows as flow_repo
from bot.repositories import memberships as membership_repo
from bot.repositories import promos as promo_repo
from bot.repositories.app_settings import get_setting, set_setting
from bot.repositories.audit_log import (
    add_audit_log,
    get_action_payload,
    list_audit_logs,
    recent_campaigns,
)
from bot.repositories.message_templates import get_template_by_key, upsert_template
from bot.repositories.promos import delete_user_promos
from bot.repositories.users import (
    get_or_create_user,
    get_user_by_id,
    get_user_by_tg_id,
    get_user_by_username,
    lock_user_by_id,
)
from bot.services.delivery import claim_attempt
from bot.services.entitlements import has_valid_access
from bot.services.flows import extend_memberships_for_flow, sales_window_for_start
from bot.services.mailings import custom_audience_ids, send_custom_broadcast
from bot.services.memberships import compute_grace_end
from bot.services.settings import (
    get_effective_settings,
    get_mailings_enabled,
    get_shop_free_label,
    get_shop_prices,
)
from bot.services.texts import get_text
from bot.ui.messages import split_message
from bot.ui.navigation import edit_screen, send_clean_screen
from config import settings

router = Router()
logger = logging.getLogger(__name__)


def _extend_membership_seven_days(membership, now, grace_days):
    membership.status = MembershipStatus.ACTIVE
    membership.access_end_at = max(membership.access_end_at, now) + timedelta(days=7)
    membership.grace_end_at = max(
        membership.grace_end_at,
        compute_grace_end(membership.access_end_at, grace_days),
    )
    if membership.pay_later_deadline_at:
        membership.pay_later_deadline_at = max(
            membership.pay_later_deadline_at, membership.access_end_at
        )


def _next_paid_start_after_flow_end(flow_end_at: datetime) -> datetime:
    """Вернуть 00:00 UTC в календарный день после окончания потока."""
    end_day = flow_end_at.astimezone(timezone.utc).date()
    next_day = end_day + timedelta(days=1)
    return datetime(next_day.year, next_day.month, next_day.day, tzinfo=timezone.utc)


class TemplateEditState(StatesGroup):
    waiting_text = State()


class FlowEditState(StatesGroup):
    waiting_start = State()
    waiting_end = State()


class PriceEditState(StatesGroup):
    waiting_value = State()


class UserSearchState(StatesGroup):
    waiting_query = State()


class PromoCreateState(StatesGroup):
    waiting_code = State()
    waiting_kind = State()
    waiting_value = State()
    waiting_limit = State()
    waiting_starts = State()
    waiting_ends = State()


class PromoDisableState(StatesGroup):
    waiting_code = State()


class ShopPriceEditState(StatesGroup):
    waiting_value = State()


class CustomMailingState(StatesGroup):
    waiting_text = State()
    confirming = State()


def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📅 Потоки", callback_data="admin:flows"),
                InlineKeyboardButton(text="💳 Цены", callback_data="admin:prices"),
            ],
            [
                InlineKeyboardButton(text="🛍 Витрина", callback_data="admin:shop"),
                InlineKeyboardButton(text="🏷 Промокоды", callback_data="admin:promos"),
            ],
            [
                InlineKeyboardButton(text="👥 Участницы", callback_data="admin:users"),
                InlineKeyboardButton(
                    text="📣 Рассылки", callback_data="admin:mailings"
                ),
            ],
            [
                InlineKeyboardButton(text="📝 Тексты", callback_data="admin:texts"),
                InlineKeyboardButton(text="🧾 Журнал", callback_data="admin:audit"),
            ],
            [
                InlineKeyboardButton(
                    text="💳 Проверка платежей", callback_data="admin:payments"
                )
            ],
        ]
    )


@router.message(Command("admin", ignore_case=True))
async def admin_menu(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        await message.answer("Доступ запрещен")
        return
    await state.clear()
    await add_audit_log(
        session,
        action="admin_menu_opened",
        payload={"tg_id": message.from_user.id},
    )
    await session.commit()
    await send_clean_screen(
        message,
        "⚙️ Управление клубом\n\nВыберите раздел:",
        reply_markup=_admin_keyboard(),
    )


async def _get_template_text(session: AsyncSession, key: str) -> str:
    template = await get_template_by_key(session, key)
    if template:
        return template.text
    return DEFAULT_TEMPLATES[key]


async def _show_template_card(
    callback: types.CallbackQuery, session: AsyncSession, key: str
) -> None:
    text = await _get_template_text(session, key)
    chunks = split_message(f"Ключ: {key}\n\nТекст:\n{text}")
    await edit_screen(callback.message, chunks[0], reply_markup=template_card_kb(key))
    for chunk in chunks[1:-1]:
        await callback.message.answer(chunk)
    if len(chunks) > 1:
        await callback.message.answer(chunks[-1], reply_markup=template_card_kb(key))
    await callback.answer()


def _format_flow_block(title: str, flow, now: datetime) -> str:
    if flow is None:
        return f"{title}: нет данных"
    kind = "Бесплатный" if flow.is_free else "Платный"
    sales_status = (
        "Набор открыт"
        if flow.sales_open_at <= now <= flow.sales_close_at
        else "Набор закрыт"
    )
    return (
        f"{title}: {kind}\n"
        f"Старт: {flow.start_at.date()}\n"
        f"Окончание: {flow.end_at.date()}\n"
        f"{sales_status}"
    )


async def _get_current_flow(session: AsyncSession, now: datetime):
    if settings.free_flows_enabled:
        flow = await flow_repo.get_active_free_flow(session, now)
        if flow is None:
            flow = await flow_repo.get_active_paid_flow(session, now)
        return flow
    return await flow_repo.get_active_paid_flow(session, now)


async def _get_next_flow(session: AsyncSession, now: datetime):
    if not settings.free_flows_enabled:
        return await flow_repo.get_next_paid_flow(session, now)
    next_free = await flow_repo.get_next_free_flow(session, now)
    next_paid = await flow_repo.get_next_paid_flow(session, now)
    if next_free and next_paid:
        return next_free if next_free.start_at <= next_paid.start_at else next_paid
    return next_free or next_paid


async def _resolve_next_paid_start_at(session: AsyncSession) -> datetime | None:
    """Рассчитать старт платного потока после последнего подходящего потока."""
    now = datetime.now(timezone.utc)
    if settings.free_flows_enabled:
        free_flow = await flow_repo.get_active_free_flow(session, now)
        if free_flow is None:
            free_flow = await flow_repo.get_next_free_flow(session, now)
        if free_flow is not None:
            return _next_paid_start_after_flow_end(free_flow.end_at)
    latest_paid = await flow_repo.get_latest_paid_flow(session)
    if latest_paid is None:
        return None
    return _next_paid_start_after_flow_end(latest_paid.end_at)


async def _show_flows_screen(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    now = datetime.now(timezone.utc)
    current_flow = await _get_current_flow(session, now)
    next_flow = await _get_next_flow(session, now)

    can_create_paid = False
    next_paid_start = await _resolve_next_paid_start_at(session)
    if next_paid_start is not None:
        existing = await flow_repo.get_next_paid_flow(session, next_paid_start)
        can_create_paid = existing is None

    text = "\n\n".join(
        [
            _format_flow_block("Текущий поток", current_flow, now),
            _format_flow_block("Следующий поток", next_flow, now),
        ]
    )
    await edit_screen(
        callback.message, text, reply_markup=flows_menu_kb(can_create_paid)
    )
    await callback.answer()


async def _show_prices_screen_message(
    message: types.Message, session: AsyncSession
) -> None:
    effective = await get_effective_settings(session)
    text = (
        "Цены и правила:\n"
        f"Вступительная: {effective.intro_price_rub}\n"
        f"Продление: {effective.renewal_price_rub}\n"
        f"Grace: {effective.grace_days} дней\n"
        f"Оплачу позже: {effective.pay_later_max_days} дней"
    )
    await message.answer(text, reply_markup=prices_menu_kb())


async def _show_prices_screen(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    effective = await get_effective_settings(session)
    text = (
        "Цены и правила:\n"
        f"Вступительная: {effective.intro_price_rub}\n"
        f"Продление: {effective.renewal_price_rub}\n"
        f"Grace: {effective.grace_days} дней\n"
        f"Оплачу позже: {effective.pay_later_max_days} дней"
    )
    await edit_screen(callback.message, text, reply_markup=prices_menu_kb())
    await callback.answer()


async def _show_mailings_screen(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    enabled = await get_mailings_enabled(session)
    override = await get_setting(session, "mailings_enabled_override")
    now = datetime.now(timezone.utc)
    pay_later_total = await membership_repo.count_pay_later_used(session)
    pay_later_active = await membership_repo.count_pay_later_active(session, now)
    pay_later_overdue = await membership_repo.count_pay_later_overdue(session, now)
    status = "включено" if enabled else "выключено"
    source = "override" if override is not None else "env"
    text = (
        f"Рассылки: {status} ({source})\n\n"
        "Оплачу позже:\n"
        f"- Использовали: {pay_later_total}\n"
        f"- Активная отсрочка: {pay_later_active}\n"
        f"- Просрочено (до отключения): {pay_later_overdue}"
    )
    await edit_screen(callback.message, text, reply_markup=mailings_menu_kb(enabled))
    await callback.answer()


def _mailings_custom_audience_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="👥 Всем", callback_data="admin:mailings:custom:all"
            ),
            InlineKeyboardButton(
                text="✅ Текущим", callback_data="admin:mailings:custom:active"
            ),
            InlineKeyboardButton(
                text="🕓 Бывшим", callback_data="admin:mailings:custom:former"
            ),
        ],
        [
            InlineKeyboardButton(
                text="💳 Не оплатившим",
                callback_data="admin:mailings:custom:current_unpaid",
            )
        ],
    ]
    rows.extend(back_menu_kb("admin:mailings").inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_users_search(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserSearchState.waiting_query)
    await edit_screen(
        callback.message,
        "👤 Управление пользователями\n\n"
        "Введите @username или tg_id участницы.\n\n"
        "После поиска вы сможете:\n"
        "• выдать или забрать доступ\n"
        "• продлить участие\n"
        "• сбросить «оплачу позже»\n"
        "• сбросить промокод",
        reply_markup=users_search_kb(),
    )
    await callback.answer()


async def _show_promos_screen(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    await edit_screen(callback.message, "Промокоды:", reply_markup=promos_menu_kb())
    await callback.answer()


async def _show_shop_screen(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    prices = await get_shop_prices(session)
    free_label = await get_shop_free_label(session)
    text = (
        "Витрина:\n"
        f"Вступление: {prices['intro']} ₽\n"
        f"Продление: {prices['renewal']} ₽\n"
        f"Бесплатный: {free_label}"
    )
    await edit_screen(callback.message, text, reply_markup=shop_menu_kb())
    await callback.answer()


async def _show_shop_screen_message(
    message: types.Message, session: AsyncSession
) -> None:
    prices = await get_shop_prices(session)
    free_label = await get_shop_free_label(session)
    text = (
        "Витрина:\n"
        f"Вступление: {prices['intro']} ₽\n"
        f"Продление: {prices['renewal']} ₽\n"
        f"Бесплатный: {free_label}"
    )
    await message.answer(text, reply_markup=shop_menu_kb())


async def _send_shop_preview(message: types.Message, session: AsyncSession) -> None:
    prices = await get_shop_prices(session)
    free_label = await get_shop_free_label(session)
    title = await get_text(session, "shop_title")
    intro_desc = await get_text(session, "shop_intro_desc")
    renewal_desc = await get_text(session, "shop_renewal_desc")
    free_desc = await get_text(session, "shop_free_desc")
    await message.answer(
        f"{title}\n"
        f"- {intro_desc} — {prices['intro']} ₽\n"
        f"- {renewal_desc} — {prices['renewal']} ₽\n"
        f"- {free_desc} — {free_label}"
    )


def _audit_action_label(action: str) -> str:
    mapping = {
        "admin_user_action": "Действие администратора",
        "automatic_access_revoke": "Автоматическое исключение",
        "mailing_sent": "Рассылка отправлена",
        "reconciliation_access_revoke": "Исключение при сверке",
    }
    return mapping.get(action, action)


def _payload_action_label(action: str) -> str:
    mapping = {
        "grant_access": "Выдать доступ",
        "revoke_access": "Забрать доступ",
        "extend_7_days": "Продлить на 7 дней",
        "reset_pay_later": "Сбросить «оплачу позже»",
        "reset_promo": "Сбросить промокод",
    }
    return mapping.get(action, action)


def _format_audit_log(entry) -> str:
    lines: list[str] = ["--------------------------------"]
    created_at = entry.created_at
    if created_at is not None:
        created_at = created_at.astimezone(timezone.utc)
        lines.append(f"🕒 {created_at.strftime('%Y-%m-%d %H:%M')} (UTC)")
    lines.append(f"📌 Тип: {_audit_action_label(entry.action)}")
    payload = entry.payload or {}
    actor_tg_id = payload.get("actor_tg_id")
    if actor_tg_id:
        lines.append(f"👤 Кто: tg_id {actor_tg_id}")
    payload_action = payload.get("action")
    if payload_action:
        lines.append(f"🧾 Что: {_payload_action_label(payload_action)}")
    target_tg_id = payload.get("tg_id")
    if target_tg_id:
        lines.append(f"🎯 Кому: tg_id {target_tg_id}")
    details = {
        k: v for k, v in payload.items() if k not in {"actor_tg_id", "action", "tg_id"}
    }
    if details:
        lines.append("ℹ️ Детали:")
        for key, value in details.items():
            lines.append(f"- {key}: {value}")
    lines.append("--------------------------------")
    return "\n".join(lines)


async def _get_current_or_next_flow(session: AsyncSession, now: datetime):
    flow = await _get_current_flow(session, now)
    if flow is None:
        flow = await _get_next_flow(session, now)
    return flow


@router.callback_query(lambda c: c.data and c.data.startswith("admin:"))
async def admin_section(
    callback: types.CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    if callback.from_user.id not in settings.admin_tg_ids:
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    section = callback.data.split(":", 1)[1]
    if section == "payments" or section.startswith("payments:"):
        await state.clear()
        await payment_reviews_screen(callback, session, section)
        return
    if section in {
        "menu",
        "flows",
        "prices",
        "texts",
        "promos",
        "shop",
        "users",
        "mailings",
        "audit",
    }:
        await state.clear()
    text: str | None = None
    if section == "flows":
        await _show_flows_screen(callback, session)
        return
    elif section == "prices":
        await _show_prices_screen(callback, session)
        return
    elif section == "texts":
        await edit_screen(
            callback.message, "Выберите шаблон:", reply_markup=templates_list_kb()
        )
        await callback.answer()
        return
    elif section == "promos":
        await _show_promos_screen(callback, session)
        return
    elif section == "shop":
        await _show_shop_screen(callback, session)
        return
    elif section == "users":
        await _show_users_search(callback, state)
        return
    elif section == "mailings":
        await _show_mailings_screen(callback, session)
        return
    elif section == "audit":
        logs = await list_audit_logs(session, limit=50)
        if not logs:
            await edit_screen(
                callback.message, "Лог пуст.", reply_markup=back_menu_kb("admin:menu")
            )
            await callback.answer()
            return
        blocks = [_format_audit_log(entry) for entry in logs]
        chunks = split_message("\n".join(blocks))
        await edit_screen(
            callback.message,
            chunks[0],
            reply_markup=back_menu_kb("admin:menu") if len(chunks) == 1 else None,
        )
        for chunk in chunks[1:-1]:
            await callback.message.answer(chunk)
        if len(chunks) > 1:
            await callback.message.answer(
                chunks[-1], reply_markup=back_menu_kb("admin:menu")
            )
        await callback.answer()
        return
    elif section == "menu":
        await edit_screen(
            callback.message,
            "⚙️ Управление клубом\n\nВыберите раздел:",
            reply_markup=_admin_keyboard(),
        )
        await callback.answer()
        return
    elif section.startswith("prices:"):
        parts = section.split(":")
        if len(parts) == 3 and parts[1] == "edit":
            key = parts[2]
            if key not in ("intro", "renewal", "grace", "pay_later"):
                await callback.answer("Неизвестная настройка", show_alert=True)
                return
            await state.set_state(PriceEditState.waiting_value)
            await state.update_data(setting_key=key)
            await edit_screen(
                callback.message,
                "Введите новое значение числом.",
                reply_markup=back_menu_kb("admin:prices"),
            )
            await callback.answer()
            return
    elif section.startswith("mailings:"):
        parts = section.split(":")
        if len(parts) == 2 and parts[1] == "history":
            await show_mailing_history(callback, session)
            return
        if len(parts) == 3 and parts[1] == "resume":
            await resume_custom_mailing(callback, session, parts[2])
            return
        if len(parts) == 3 and parts[1] == "send":
            await confirm_custom_mailing(callback, session, state, parts[2])
            return
        if len(parts) == 2 and parts[1] == "toggle":
            enabled = await get_mailings_enabled(session)
            await set_setting(
                session,
                "mailings_enabled_override",
                "false" if enabled else "true",
            )
            await session.commit()
            await _show_mailings_screen(callback, session)
            return
        if len(parts) == 2 and parts[1] == "custom":
            await state.clear()
            await edit_screen(
                callback.message,
                "Выберите аудиторию:",
                reply_markup=_mailings_custom_audience_kb(),
            )
            await callback.answer()
            return
        if len(parts) == 3 and parts[1] == "custom":
            audience = parts[2]
            if audience not in ("all", "active", "former", "current_unpaid"):
                await callback.answer("Неизвестная аудитория", show_alert=True)
                return
            await state.set_state(CustomMailingState.waiting_text)
            await state.set_data({"audience": audience})
            await edit_screen(
                callback.message,
                "Пришлите текст, фото или видео с подписью одним сообщением. "
                "Сначала покажу предпросмотр; отправка — только после подтверждения.",
                reply_markup=back_menu_kb("admin:mailings"),
            )
            await callback.answer()
            return
    elif section.startswith("shop:"):
        parts = section.split(":")
        if len(parts) == 2 and parts[1] == "texts":
            await edit_screen(
                callback.message, "Тексты витрины:", reply_markup=shop_texts_kb()
            )
            await callback.answer()
            return
        if len(parts) == 2 and parts[1] == "test":
            await _send_shop_preview(callback.message, session)
            await callback.answer()
            return
        if len(parts) == 3 and parts[1] == "edit":
            key = parts[2]
            if key not in ("intro", "renewal", "free_label"):
                await callback.answer("Неизвестная настройка", show_alert=True)
                return
            await state.set_state(ShopPriceEditState.waiting_value)
            await state.update_data(setting_key=key)
            prompt = (
                "Введите надпись (например: Бесплатно)."
                if key == "free_label"
                else "Введите новое значение числом."
            )
            await callback.message.answer(
                prompt,
                reply_markup=back_menu_kb("admin:shop"),
            )
            await callback.answer()
            return
    elif section.startswith("promos:"):
        parts = section.split(":")
        if len(parts) == 2 and parts[1] == "create":
            await state.set_state(PromoCreateState.waiting_code)
            await callback.message.answer(
                "Введите код промокода.", reply_markup=back_menu_kb("admin:promos")
            )
            await callback.answer()
            return
        if len(parts) == 2 and parts[1] == "list":
            promos = await promo_repo.list_recent_promos(session, limit=10)
            if not promos:
                await callback.message.answer(
                    "Промокоды не найдены.", reply_markup=back_menu_kb("admin:promos")
                )
                await callback.answer()
                return
            lines = []
            for promo in promos:
                limit = promo.max_uses if promo.max_uses is not None else "∞"
                starts = promo.starts_at.date() if promo.starts_at else "-"
                ends = promo.ends_at.date() if promo.ends_at else "-"
                lines.append(
                    f"{promo.code} | {promo.kind} | {promo.value_int} | "
                    f"{promo.used_count}/{limit} | "
                    f"{'active' if promo.active else 'off'} | {starts}→{ends}"
                )
            await callback.message.answer(
                "Последние промокоды:\n" + "\n".join(lines),
                reply_markup=back_menu_kb("admin:promos"),
            )
            await callback.answer()
            return
        if len(parts) == 2 and parts[1] == "disable":
            await state.set_state(PromoDisableState.waiting_code)
            await callback.message.answer(
                "Введите код промокода для отключения.",
                reply_markup=back_menu_kb("admin:promos"),
            )
            await callback.answer()
            return
        if len(parts) == 3 and parts[1] == "kind":
            kind = parts[2]
            if kind not in ("percent", "fixed", "free"):
                await callback.answer("Неизвестный тип", show_alert=True)
                return
            await state.update_data(kind=kind)
            if kind == "free":
                await state.update_data(value_int=0)
                await state.set_state(PromoCreateState.waiting_limit)
                await callback.message.answer("Введите лимит (0 = безлимит).")
                await callback.answer()
                return
            await state.set_state(PromoCreateState.waiting_value)
            await callback.message.answer("Введите значение числами.")
            await callback.answer()
            return
    elif section.startswith("users:"):
        parts = section.split(":")
        if len(parts) != 3 or parts[0] != "users":
            await callback.answer("Некорректная команда", show_alert=True)
            return
        action = parts[1]
        user_id = int(parts[2]) if parts[2].isdigit() else None
        if user_id is None:
            await callback.answer("Некорректная команда", show_alert=True)
            return
        user = await get_user_by_id(session, user_id)
        if not user:
            await callback.message.answer("Пользователь не найден.")
            await callback.answer()
            return

        admin_user = await get_or_create_user(
            session=session,
            tg_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
            last_name=callback.from_user.last_name,
            is_admin=True,
        )
        await session.commit()

        # Serialize explicit admin changes with payment confirmation and
        # automatic expiry for this participant.
        user = await lock_user_by_id(session, user.id)
        if user is None:
            await callback.message.answer("Пользователь больше не существует.")
            await callback.answer()
            return

        now = datetime.now(timezone.utc)
        membership = await membership_repo.get_latest_membership(
            session, user_id=user.id
        )
        effective = await get_effective_settings(session)

        if action == "grant":
            flow = await _get_current_or_next_flow(session, now)
            if flow is None:
                await callback.message.answer("Поток не найден.")
                await callback.answer()
                return
            existing = await membership_repo.get_membership_by_flow(
                session, user_id=user.id, flow_id=flow.id
            )
            if existing:
                membership = existing
            else:
                membership = Membership(user_id=user.id, flow_id=flow.id)
                session.add(membership)
            membership.status = MembershipStatus.ACTIVE
            membership.access_start_at = min(
                membership.access_start_at or flow.start_at, flow.start_at
            )
            membership.access_end_at = max(
                membership.access_end_at or flow.end_at,
                flow.end_at,
                membership.pay_later_deadline_at or flow.end_at,
            )
            membership.grace_end_at = max(
                membership.grace_end_at or flow.end_at,
                compute_grace_end(membership.access_end_at, effective.grace_days),
            )
            user.access_suspended = False
            if membership.pay_later_deadline_at:
                membership.pay_later_deadline_at = max(
                    membership.pay_later_deadline_at, membership.access_end_at
                )
            access_result = await grant_access(callback.message.bot, user.tg_id)
            await add_audit_log(
                session,
                action="admin_user_action",
                payload={
                    "tg_id": user.tg_id,
                    "action": "grant_access",
                    "flow_id": flow.id,
                    "actor_tg_id": callback.from_user.id,
                },
                actor_user_id=admin_user.id,
            )
            await session.commit()
            if access_result.successful:
                access_keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="📢 Войти в канал",
                                url=access_result.channel_link,
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="💬 Войти в группу",
                                url=access_result.group_link,
                            )
                        ],
                    ]
                )
                try:
                    await callback.message.bot.send_message(
                        user.tg_id,
                        "✅ Администратор активировал ваш доступ. "
                        "Отправьте заявки по кнопкам ниже.",
                        reply_markup=access_keyboard,
                    )
                except Exception:
                    await callback.message.answer(
                        "⚠️ Доступ выдан, но пользователь не получил сообщение. "
                        "Вероятно, он ещё не запускал бота."
                    )
                await callback.message.answer("✅ Доступ выдан в канал и группу.")
            else:
                await callback.message.answer(
                    "⚠️ Участие сохранено, но Telegram выдал доступ не везде. "
                    "Проверьте права бота и повторите действие."
                )
            await callback.answer()
            return

        if action in {"revoke", "exempt_off"}:
            await session.commit()
            confirm_action = (
                "revoke_confirm" if action == "revoke" else "exempt_off_confirm"
            )
            await callback.message.answer(
                "Вы уверены? Это действие может лишить участницу доступа."
                "\nПодтвердите действие только для выбранной участницы.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Подтвердить",
                                callback_data=f"admin:users:{confirm_action}:{user.id}",
                            ),
                            InlineKeyboardButton(
                                text="Отмена", callback_data="admin:users"
                            ),
                        ]
                    ]
                ),
            )
            await callback.answer()
            return

        if action == "revoke_confirm":
            if user.access_exempt:
                await session.rollback()
                await callback.message.answer(
                    "🛡 У участницы включён льготный доступ. Сначала явно "
                    "снимите льготную защиту в её карточке."
                )
                await callback.answer()
                return
            access_result = await revoke_access(callback.message.bot, user.tg_id)
            if access_result.protected:
                await session.rollback()
                await callback.message.answer(
                    "🛡 Этот пользователь указан как администратор бота. "
                    "Автоматическое исключение запрещено."
                )
                await callback.answer()
                return
            user.access_suspended = True
            if not access_result.successful:
                await add_audit_log(
                    session,
                    "admin_user_action",
                    {
                        "tg_id": user.tg_id,
                        "action": "suspend_access_partial",
                        "actor_tg_id": callback.from_user.id,
                    },
                    actor_user_id=admin_user.id,
                )
                await session.commit()
                await callback.message.answer(
                    "⚠️ Исключение выполнено не во всех чатах. Повторный вход "
                    "запрещён; устраните проблему с правами и повторите исключение."
                )
                await callback.answer()
                return
            expired_count = await membership_repo.expire_all_active_memberships(
                session, user.id
            )
            await add_audit_log(
                session,
                action="admin_user_action",
                payload={
                    "tg_id": user.tg_id,
                    "action": "revoke_access",
                    "expired_memberships": expired_count,
                    "actor_tg_id": callback.from_user.id,
                },
                actor_user_id=admin_user.id,
            )
            await session.commit()
            await callback.message.answer(
                f"⛔ Доступ забран из канала и группы. "
                f"Закрыто активных участий: {expired_count}."
            )
            await callback.answer()
            return

        if action == "exempt":
            await callback.answer(
                "Старая кнопка. Откройте карточку участницы заново.", show_alert=True
            )
            return

        if action in {"exempt_on", "exempt_off_confirm"}:
            user.access_exempt = action == "exempt_on"
            if user.access_exempt:
                user.access_suspended = False
            await add_audit_log(
                session,
                action="admin_user_action",
                payload={
                    "tg_id": user.tg_id,
                    "action": (
                        "enable_access_exempt"
                        if user.access_exempt
                        else "disable_access_exempt"
                    ),
                    "actor_tg_id": callback.from_user.id,
                },
                actor_user_id=admin_user.id,
            )
            await session.commit()
            await callback.message.answer(
                "🛡 Льготный доступ включён: автоматические исключения запрещены."
                if user.access_exempt
                else "🔓 Льготная защита снята."
            )
            await callback.answer()
            return

        if action == "extend7":
            if not membership:
                await callback.message.answer("Нет участия для продления.")
                await callback.answer()
                return
            _extend_membership_seven_days(membership, now, effective.grace_days)
            user.access_suspended = False
            await add_audit_log(
                session,
                action="admin_user_action",
                payload={
                    "tg_id": user.tg_id,
                    "action": "extend_7_days",
                    "actor_tg_id": callback.from_user.id,
                },
                actor_user_id=admin_user.id,
            )
            access_result = await grant_access(callback.message.bot, user.tg_id)
            await session.commit()
            await callback.message.answer(
                "✅ Продлено на 7 дней. Ссылки доступны участнице в «Мой доступ»."
                if access_result.successful
                else "✅ Срок продлён. ⚠️ Telegram не подтвердил доступ в оба чата; "
                "проверьте права бота и повторите выдачу доступа."
            )
            await callback.answer()
            return

        if action == "reset_pay_later":
            if not membership:
                await callback.message.answer("Нет участия для сброса.")
                await callback.answer()
                return
            membership.pay_later_used_at = None
            membership.pay_later_deadline_at = None
            await add_audit_log(
                session,
                action="admin_user_action",
                payload={
                    "tg_id": user.tg_id,
                    "action": "reset_pay_later",
                    "actor_tg_id": callback.from_user.id,
                },
                actor_user_id=admin_user.id,
            )
            await session.commit()
            await callback.message.answer("✅ Сброшено.")
            await callback.answer()
            return
        if action == "reset_promo":
            await delete_user_promos(session, user.id)
            await add_audit_log(
                session,
                action="admin_user_action",
                payload={
                    "tg_id": user.tg_id,
                    "action": "reset_promo",
                    "actor_tg_id": callback.from_user.id,
                },
                actor_user_id=admin_user.id,
            )
            await session.commit()
            await callback.message.answer("✅ Промокод сброшен.")
            await callback.answer()
            return
    elif section.startswith("flows:"):
        parts = section.split(":")
        if len(parts) == 2 and parts[1] == "edit":
            await callback.message.answer(
                "Какой поток редактировать?", reply_markup=flows_edit_select_kb()
            )
            await callback.answer()
            return
        if len(parts) == 3 and parts[1] == "edit":
            now = datetime.now(timezone.utc)
            target = parts[2]
            flow = None
            if target == "current":
                flow = await _get_current_flow(session, now)
            elif target == "next":
                flow = await _get_next_flow(session, now)
            if flow is None:
                await callback.message.answer("Поток не найден.")
                await callback.answer()
                return
            await state.set_state(FlowEditState.waiting_start)
            await state.update_data(flow_id=flow.id)
            await callback.message.answer(
                "Введите дату старта (YYYY-MM-DD).",
                reply_markup=back_menu_kb("admin:flows"),
            )
            await callback.answer()
            return
        if len(parts) == 2 and parts[1] == "create_paid":
            start_at = await _resolve_next_paid_start_at(session)
            if start_at is None:
                await callback.message.answer(
                    "Не удалось вычислить старт следующего платного потока: "
                    "в базе нет платных потоков. Задайте PAID_FLOW_START и "
                    "перезапустите бота (сидер) или добавьте поток вручную."
                )
                await callback.answer()
                return
            existing_paid = await flow_repo.get_next_paid_flow(session, start_at)
            if existing_paid:
                await callback.message.answer("Следующий платный поток уже создан.")
                await callback.answer()
                return
            end_at = start_at + timedelta(weeks=5)
            sales_open_at, sales_close_at = sales_window_for_start(start_at)
            session.add(
                Flow(
                    title="Платный поток",
                    start_at=start_at,
                    end_at=end_at,
                    duration_weeks=5,
                    is_free=False,
                    sales_open_at=sales_open_at,
                    sales_close_at=sales_close_at,
                )
            )
            await session.commit()
            await callback.message.answer(
                "Создан платный поток:\n"
                f"Старт: {start_at.date()}\n"
                f"Окончание: {end_at.date()}"
            )
            await callback.answer()
            return
    elif section.startswith("text:"):
        parts = section.split(":")
        if len(parts) == 2:
            key = parts[1]
            if key not in DEFAULT_TEMPLATES:
                await callback.answer("Неизвестный шаблон", show_alert=True)
                return
            await _show_template_card(callback, session, key)
            return
        if len(parts) == 3 and parts[1] == "edit":
            key = parts[2]
            if key not in DEFAULT_TEMPLATES:
                await callback.answer("Неизвестный шаблон", show_alert=True)
                return
            await state.set_state(TemplateEditState.waiting_text)
            await state.update_data(template_key=key)
            await callback.message.answer(
                "Пришлите новый текст одним сообщением."
                + (
                    "\nДля расписания обязательны {start}, {end}, {sales_status}. "
                    "Даты подставятся автоматически; {kind} — тип потока."
                    if key == "schedule_text"
                    else ""
                )
            )
            await callback.answer()
            return
        if len(parts) == 3 and parts[1] == "test":
            key = parts[2]
            if key not in DEFAULT_TEMPLATES:
                await callback.answer("Неизвестный шаблон", show_alert=True)
                return
            text = await _get_template_text(session, key)
            await callback.message.answer(text)
            await callback.answer("Тест отправлен")
            return
    else:
        text = "Неизвестный раздел."

    await add_audit_log(
        session,
        action="admin_section_opened",
        payload={"section": section, "tg_id": callback.from_user.id},
    )
    await session.commit()

    if text is None:
        text = "Неизвестный раздел."
    await callback.message.answer(text)
    await callback.answer()


@router.message(FlowEditState.waiting_start)
async def flow_edit_start_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        return
    try:
        start_at = datetime.strptime((message.text or "").strip(), "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        await message.answer("Неверный формат даты. Используйте YYYY-MM-DD.")
        return

    data = await state.get_data()
    flow_id = data.get("flow_id")
    if not flow_id:
        await state.clear()
        await message.answer("Поток не найден.")
        return

    await state.update_data(start_at=start_at)
    await state.set_state(FlowEditState.waiting_end)
    await message.answer(
        "Введите дату окончания (YYYY-MM-DD).",
        reply_markup=back_menu_kb("admin:flows"),
    )


@router.message(FlowEditState.waiting_end)
async def flow_edit_end_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        return
    try:
        end_at = datetime.strptime((message.text or "").strip(), "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        await message.answer("Неверный формат даты. Используйте YYYY-MM-DD.")
        return

    data = await state.get_data()
    flow_id = data.get("flow_id")
    start_at = data.get("start_at")
    if not flow_id or not start_at:
        await state.clear()
        await message.answer("Поток не найден.")
        return
    if end_at <= start_at:
        await message.answer("Дата окончания должна быть позже даты старта.")
        return

    flow = await flow_repo.get_flow_by_id(session, flow_id)
    if flow is None:
        await state.clear()
        await message.answer("Поток не найден.")
        return

    duplicate = await flow_repo.get_flow_by_start(session, start_at, flow.is_free)
    if duplicate is not None and duplicate.id != flow.id:
        await message.answer("Поток с такой датой уже существует. Введите другие даты.")
        return
    await extend_memberships_for_flow(session, flow.id, end_at)
    flow.start_at = start_at
    flow.end_at = end_at
    flow.duration_weeks = max(1, (end_at - start_at).days // 7)
    flow.sales_open_at, flow.sales_close_at = sales_window_for_start(start_at)
    await session.commit()
    await state.clear()

    await message.answer("✅ Даты обновлены.")
    await message.answer(
        f"Старт: {flow.start_at.date()}\nОкончание: {flow.end_at.date()}"
    )


@router.message(PriceEditState.waiting_value)
async def price_edit_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        return

    data = await state.get_data()
    key = data.get("setting_key")
    if key not in ("intro", "renewal", "grace", "pay_later"):
        await state.clear()
        await message.answer("Настройка не найдена.")
        return

    try:
        value = int((message.text or "").strip())
    except ValueError:
        await message.answer("Введите целое число.")
        return

    if key in ("intro", "renewal") and not (0 <= value <= 1_000_000):
        await message.answer("Цена должна быть в диапазоне 0..1_000_000.")
        return
    if key == "grace" and not (0 <= value <= 30):
        await message.answer("Grace должен быть в диапазоне 0..30 дней.")
        return
    if key == "pay_later" and not (0 <= value <= 60):
        await message.answer("Оплачу позже должно быть в диапазоне 0..60 дней.")
        return

    mapping = {
        "intro": "intro_price_rub",
        "renewal": "renewal_price_rub",
        "grace": "grace_days",
        "pay_later": "pay_later_max_days",
    }
    await set_setting(session, mapping[key], str(value))
    await session.commit()
    await state.clear()

    await message.answer("✅ Сохранено.")
    await _show_prices_screen_message(message, session)


@router.message(UserSearchState.waiting_query)
async def user_search_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer("Введите @username или числовой Telegram ID.")
        return

    user = None
    if query.isdigit():
        user = await get_user_by_tg_id(session, int(query))
    else:
        if query.startswith("@"):
            query = query[1:]
        if query:
            user = await get_user_by_username(session, query)

    if not user:
        await message.answer("Пользователь не найден.")
        return

    membership = await membership_repo.get_latest_membership(session, user_id=user.id)
    now = datetime.now(timezone.utc)
    has_access = await has_valid_access(session, user.id, now)

    lines = [
        f"tg_id: {user.tg_id}",
        f"username: @{user.username}" if user.username else "username: —",
        f"имя: {user.first_name or ''} {user.last_name or ''}".strip() or "имя: —",
        f"доступ сейчас: {'да' if has_access else 'нет'}",
        f"льготная защита: {'включена' if user.access_exempt else 'нет'}",
        f"ручное ограничение: {'включено' if user.access_suspended else 'нет'}",
    ]

    if membership:
        lines.extend(
            [
                "последнее участие:",
                f"status: {membership.status}",
                f"start: {membership.access_start_at.date()}",
                f"end: {membership.access_end_at.date()}",
                f"grace_end: {membership.grace_end_at.date()}",
            ]
        )
        if membership.pay_later_deadline_at:
            lines.append(
                f"pay_later_deadline: {membership.pay_later_deadline_at.date()}"
            )
    else:
        lines.append("участие: нет")

    await state.clear()
    await message.answer(
        "\n".join(lines),
        reply_markup=user_card_kb(user.id, access_exempt=user.access_exempt),
    )


@router.message(PromoCreateState.waiting_code)
async def promo_create_code_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        return
    code = (message.text or "").strip().upper()
    if not code:
        await message.answer("Введите код промокода.")
        return
    existing = await promo_repo.get_promo_by_code(session, code)
    if existing:
        await message.answer("Промокод уже существует.")
        return
    await state.update_data(code=code)
    await state.set_state(PromoCreateState.waiting_kind)
    await message.answer("Выберите тип промокода:", reply_markup=promo_kind_kb())


@router.message(PromoCreateState.waiting_kind)
async def promo_create_kind_text_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        await state.clear()
        return
    await message.answer(
        "Нажмите одну из кнопок выше для выбора типа.", reply_markup=promo_kind_kb()
    )


@router.message(PromoCreateState.waiting_value)
async def promo_create_value_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        return
    try:
        value = int((message.text or "").strip())
    except ValueError:
        await message.answer("Введите число.")
        return
    if value < 0:
        await message.answer("Значение не может быть отрицательным.")
        return
    await state.update_data(value_int=value)
    await state.set_state(PromoCreateState.waiting_limit)
    await message.answer(
        "Введите лимит (0 = безлимит).", reply_markup=back_menu_kb("admin:promos")
    )


@router.message(PromoCreateState.waiting_limit)
async def promo_create_limit_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        return
    try:
        value = int((message.text or "").strip())
    except ValueError:
        await message.answer("Введите число.")
        return
    if value < 0:
        await message.answer("Лимит не может быть отрицательным.")
        return
    max_uses = None if value == 0 else value
    await state.update_data(max_uses=max_uses)
    await state.set_state(PromoCreateState.waiting_starts)
    await message.answer(
        "Дата начала (YYYY-MM-DD) или '-' чтобы пропустить.",
        reply_markup=back_menu_kb("admin:promos"),
    )


@router.message(PromoCreateState.waiting_starts)
async def promo_create_starts_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        return
    raw = (message.text or "").strip()
    starts_at = None
    if raw and raw not in ("-", "skip"):
        try:
            starts_at = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            await message.answer("Неверный формат даты. Используйте YYYY-MM-DD.")
            return
    await state.update_data(starts_at=starts_at)
    await state.set_state(PromoCreateState.waiting_ends)
    await message.answer(
        "Дата окончания (YYYY-MM-DD) или '-' чтобы пропустить.",
        reply_markup=back_menu_kb("admin:promos"),
    )


@router.message(PromoCreateState.waiting_ends)
async def promo_create_ends_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        return
    raw = (message.text or "").strip()
    ends_at = None
    if raw and raw not in ("-", "skip"):
        try:
            ends_at = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            await message.answer("Неверный формат даты. Используйте YYYY-MM-DD.")
            return

    data = await state.get_data()
    code = data.get("code")
    kind = data.get("kind")
    value_int = data.get("value_int", 0)
    max_uses = data.get("max_uses")
    starts_at = data.get("starts_at")
    if not code or not kind:
        await state.clear()
        await message.answer("Не удалось создать промокод.")
        return
    if starts_at and ends_at and ends_at < starts_at:
        await message.answer("Дата окончания должна быть позже даты начала.")
        return

    await promo_repo.create_promo_code(
        session=session,
        code=code,
        kind=kind,
        value_int=value_int,
        max_uses=max_uses,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    await session.commit()
    await state.clear()
    await message.answer("✅ Промокод создан.")


@router.message(PromoDisableState.waiting_code)
async def promo_disable_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        return
    code = (message.text or "").strip().upper()
    if not code:
        await message.answer("Введите код промокода.")
        return
    ok = await promo_repo.disable_promo(session, code)
    if not ok:
        await message.answer("Промокод не найден.")
        return
    await session.commit()
    await state.clear()
    await message.answer("✅ Промокод отключен.")


@router.message(CustomMailingState.waiting_text)
async def custom_mailing_text_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        return
    data = await state.get_data()
    audience = data.get("audience")
    logger.info(
        "Custom mailing input: type=%s text_length=%s caption_length=%s audience=%s",
        message.content_type,
        len(message.text or ""),
        len(message.caption or ""),
        audience,
    )
    if audience not in ("all", "active", "former", "current_unpaid"):
        await state.clear()
        await message.answer("Аудитория не найдена.")
        return
    if message.media_group_id:
        await message.answer(
            "Для этой рассылки пришлите одно фото или видео, без альбома."
        )
        return
    if not (message.text or message.photo or message.video or message.document):
        await message.answer(
            "Поддерживаются текст, фото, видео и документ. "
            "Пришлите один из этих вариантов или нажмите «Назад»."
        )
        return
    enabled = await get_mailings_enabled(session)
    if not enabled:
        await state.clear()
        await message.answer("⛔ Рассылки выключены. Включите в админке.")
        return
    user_ids = await custom_audience_ids(session, audience)
    await session.commit()
    if not user_ids:
        await message.answer(
            "В выбранной аудитории нет получателей. Выберите другую.",
            reply_markup=back_menu_kb("admin:mailings"),
        )
        return
    # Freeze the content in a bot-owned preview, including formatting and media.
    preview = await message.bot.copy_message(
        chat_id=message.chat.id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
    )
    key = uuid4().hex
    await state.set_state(CustomMailingState.confirming)
    await state.set_data(
        {
            "key": key,
            "audience": audience,
            "user_ids": user_ids,
            "source_chat_id": message.chat.id,
            "source_message_id": preview.message_id,
        }
    )
    labels = {
        "all": "Все пользователи",
        "active": "Активные участницы",
        "former": "Бывшие участницы",
        "current_unpaid": "Не оплатившие продление",
    }
    await message.answer(
        f"Предпросмотр выше.\nАудитория: {labels[audience]}\n"
        f"Получателей: {len(user_ids)}\n"
        "Отправить это сообщение?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Отправить", callback_data=f"admin:mailings:send:{key}"
                    )
                ],
                [InlineKeyboardButton(text="Отмена", callback_data="admin:mailings")],
            ]
        ),
    )


@router.message(CustomMailingState.confirming)
async def custom_mailing_waiting_confirmation(message: types.Message) -> None:
    if message.from_user.id in settings.admin_tg_ids:
        await message.answer(
            "Рассылка ещё не отправлена. Подтвердите предпросмотр "
            "кнопкой «Отправить» или отмените его."
        )


async def confirm_custom_mailing(callback, session, state, key: str) -> None:
    data = await state.get_data()
    if (
        await state.get_state() != CustomMailingState.confirming.state
        or data.get("key") != key
    ):
        await callback.answer(
            "Этот предпросмотр уже использован или устарел.", show_alert=True
        )
        return
    if not await get_mailings_enabled(session):
        await callback.answer("Рассылки выключены.", show_alert=True)
        return
    if not await claim_attempt(
        session,
        "custom_mailing_started",
        key,
        actor_tg_id=callback.from_user.id,
        audience=data["audience"],
        total=len(data["user_ids"]),
        user_ids=data["user_ids"],
        source_chat_id=data["source_chat_id"],
        source_message_id=data["source_message_id"],
    ):
        await state.clear()
        await callback.answer("Эта рассылка уже запущена.", show_alert=True)
        return
    await state.clear()
    await _run_custom_mailing(callback, session, data, key)


async def _mailing_ui(awaitable):
    # Delivery must not depend on acknowledging an old button or editing a menu.
    try:
        await awaitable
    except (TelegramAPIError, TimeoutError, OSError) as exc:
        logger.warning("Mailing UI unavailable: %s", type(exc).__name__)


async def show_mailing_history(callback, session):
    rows = []
    for entry in await recent_campaigns(session):
        data = entry.payload
        if "user_ids" not in data:  # Pre-migration journal cannot resume safely.
            continue
        finished = await get_action_payload(
            session, "custom_mailing_finished", data["key"]
        )
        label = (
            "Результат" if finished and not finished.get("stopped") else "Продолжить"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"{label} · {entry.created_at:%d.%m %H:%M} UTC · "
                        f"{data['total']} чел."
                    ),
                    callback_data=f"admin:mailings:resume:{data['key']}",
                )
            ]
        )
    rows.extend(back_menu_kb("admin:mailings").inline_keyboard)
    await _mailing_ui(
        edit_screen(
            callback.message,
            "Последние рассылки. Продолжение отправляет только тем, для кого ещё "
            "не было попытки. Неопределённые доставки не повторяются.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    )
    await _mailing_ui(callback.answer())


async def resume_custom_mailing(callback, session, key):
    data = await get_action_payload(session, "custom_mailing_started", key)
    if not data or "user_ids" not in data:
        await _mailing_ui(
            callback.answer("Нет данных для продолжения.", show_alert=True)
        )
        return
    finished = await get_action_payload(session, "custom_mailing_finished", key)
    if finished and not finished.get("stopped"):
        await _mailing_ui(
            edit_screen(
                callback.message,
                "Рассылка завершена. Повторная отправка не выполняется.\n"
                f"Последний запуск: доставлено {finished['sent']}, "
                f"пропущено ранее обработанных {finished['skipped']}, "
                f"неопределённых {finished['unknown']}, "
                f"ошибок {finished['failed'] + finished['rate_limited']}, "
                f"блокировок {finished['blocked']}.",
                reply_markup=back_menu_kb("admin:mailings:history"),
            )
        )
        await _mailing_ui(callback.answer())
        return
    if not await get_mailings_enabled(session):
        await _mailing_ui(
            callback.answer("Сначала включите рассылки.", show_alert=True)
        )
        return
    await _run_custom_mailing(callback, session, data, key)


async def _run_custom_mailing(callback, session, data, key):
    await _mailing_ui(callback.answer("Отправка началась"))
    await _mailing_ui(
        edit_screen(
            callback.message,
            f"📨 Отправляю {len(data['user_ids'])} получателям. "
            "Это может занять несколько минут. Результат появится здесь.",
        )
    )
    try:
        result = await send_custom_broadcast(
            session,
            callback.bot,
            user_ids=data["user_ids"],
            source_chat_id=data["source_chat_id"],
            source_message_id=data["source_message_id"],
            key=key,
        )
    except Exception:
        logger.exception("Custom mailing interrupted: key=%s", key)
        await session.rollback()
        await add_audit_log(session, "custom_mailing_interrupted", {"key": key})
        await session.commit()
        await _mailing_ui(
            edit_screen(
                callback.message,
                "⚠️ Рассылка прервана. Часть сообщений могла "
                "уйти. Продолжите через «Последние рассылки»: "
                "ранее начатые доставки не повторятся.",
                reply_markup=back_menu_kb("admin:mailings"),
            )
        )
        return
    await _mailing_ui(
        edit_screen(
            callback.message,
            f"Рассылка {'остановлена' if result.get('stopped') else 'завершена'}.\n"
            "Результат этого запуска:\n"
            f"Доставлено: {result['sent']}\nЗаблокировали бота: {result['blocked']}\n"
            f"Ошибки: {result['failed'] + result['rate_limited']}\n"
            f"Доставка не подтверждена: {result['unknown']}\n"
            f"Пропущено: {result['skipped']}",
            reply_markup=back_menu_kb("admin:mailings"),
        )
    )


@router.message(ShopPriceEditState.waiting_value)
async def shop_price_edit_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        return
    data = await state.get_data()
    key = data.get("setting_key")
    if key not in ("intro", "renewal", "free_label"):
        await state.clear()
        await message.answer("Настройка не найдена.")
        return
    if key == "free_label":
        value = (message.text or "").strip()
        if not value:
            await message.answer("Введите непустое значение.")
            return
        await set_setting(session, "shop_free_label", value)
    else:
        try:
            value = int((message.text or "").strip())
        except ValueError:
            await message.answer("Введите целое число.")
            return
        if not (0 <= value <= 1_000_000):
            await message.answer("Цена должна быть в диапазоне 0..1_000_000.")
            return
        mapping = {
            "intro": "intro_price_rub",
            "renewal": "renewal_price_rub",
        }
        await set_setting(session, mapping[key], str(value))
    await session.commit()
    await state.clear()
    await message.answer("✅ Сохранено.")
    await _show_shop_screen_message(message, session)


@router.message(TemplateEditState.waiting_text)
async def template_text_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        await state.clear()
        return
    data = await state.get_data()
    key = data.get("template_key")
    if not key or key not in DEFAULT_TEMPLATES:
        await state.clear()
        await message.answer("Шаблон не найден.")
        return

    text = message.text or ""
    if not text.strip():
        await message.answer("Отправьте непустой текст шаблона.")
        return
    if key == "schedule_text":
        try:
            if not all(
                field in text for field in ("{start}", "{end}", "{sales_status}")
            ):
                raise ValueError("Missing schedule fields")
            text.format(start="дата", end="дата", sales_status="статус", kind="тип")
        except (KeyError, ValueError, IndexError, AttributeError):
            await message.answer(
                "Используйте {start}, {end}, {sales_status} и при желании {kind}. "
                "Не задавайте даты вручную — иначе расписание устареет."
            )
            return
    await upsert_template(session, key, text)
    await session.commit()
    await state.clear()

    await message.answer("✅ Сохранено.")
    chunks = split_message(f"Ключ: {key}\n\nТекст:\n{text}")
    for chunk in chunks[:-1]:
        await message.answer(chunk)
    await message.answer(chunks[-1], reply_markup=template_card_kb(key))
