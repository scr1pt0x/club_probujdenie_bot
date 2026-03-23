from datetime import datetime, timedelta, timezone

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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
from bot.admin.templates import DEFAULT_TEMPLATES, TEMPLATE_LABELS
from bot.db.models import Flow, Membership, MembershipStatus
from bot.repositories import flows as flow_repo
from bot.repositories import memberships as membership_repo
from bot.repositories.audit_log import add_audit_log, list_audit_logs
from bot.repositories.app_settings import get_setting, set_setting
from bot.repositories.message_templates import get_template_by_key, upsert_template
from bot.repositories.promos import delete_user_promos
from bot.repositories import promos as promo_repo
from bot.repositories.users import get_or_create_user, get_user_by_id, get_user_by_tg_id, get_user_by_username
from bot.services.mailings import send_custom_broadcast, send_manual_mailings
from bot.services.memberships import compute_grace_end
from bot.services.settings import (
    get_effective_settings,
    get_mailings_enabled,
    get_shop_free_label,
    get_shop_prices,
)
from bot.services.texts import get_text
from bot.services.flows import sales_window_for_start
from config import settings


router = Router()


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

def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Потоки", callback_data="admin:flows")],
            [InlineKeyboardButton(text="Цены и grace-период", callback_data="admin:prices")],
            [InlineKeyboardButton(text="📝 Тексты", callback_data="admin:texts")],
            [InlineKeyboardButton(text="Промо / бесплатные потоки", callback_data="admin:promos")],
            [InlineKeyboardButton(text="🛍 Витрина", callback_data="admin:shop")],
            [InlineKeyboardButton(text="Пользователи", callback_data="admin:users")],
            [InlineKeyboardButton(text="📣 Рассылки", callback_data="admin:mailings")],
            [InlineKeyboardButton(text="Лог действий", callback_data="admin:audit")],
        ]
    )


@router.message(Command("admin"))
async def admin_menu(message: types.Message, session: AsyncSession) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        await message.answer("Доступ запрещен")
        return
    await add_audit_log(
        session,
        action="admin_menu_opened",
        payload={"tg_id": message.from_user.id},
    )
    await session.commit()
    await message.answer("Админ-панель:", reply_markup=_admin_keyboard())


async def _get_template_text(session: AsyncSession, key: str) -> str:
    template = await get_template_by_key(session, key)
    if template:
        return template.text
    return DEFAULT_TEMPLATES[key]


async def _show_template_card(
    callback: types.CallbackQuery, session: AsyncSession, key: str
) -> None:
    text = await _get_template_text(session, key)
    await callback.message.answer(
        f"Ключ: {key}\n\nТекст:\n{text}",
        reply_markup=template_card_kb(key),
    )
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
    flow = await flow_repo.get_active_free_flow(session, now)
    if flow is None:
        flow = await flow_repo.get_active_paid_flow(session, now)
    return flow


async def _get_next_flow(session: AsyncSession, now: datetime):
    next_free = await flow_repo.get_next_free_flow(session, now)
    next_paid = await flow_repo.get_next_paid_flow(session, now)
    if next_free and next_paid:
        return next_free if next_free.start_at <= next_paid.start_at else next_paid
    return next_free or next_paid


async def _show_flows_screen(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    now = datetime.now(timezone.utc)
    current_flow = await _get_current_flow(session, now)
    next_flow = await _get_next_flow(session, now)

    free_flow = await flow_repo.get_active_free_flow(session, now)
    if free_flow is None:
        free_flow = await flow_repo.get_next_free_flow(session, now)
    can_create_paid = False
    if free_flow:
        next_paid = await flow_repo.get_next_paid_flow(
            session, free_flow.end_at + timedelta(days=1)
        )
        can_create_paid = next_paid is None

    text = "\n\n".join(
        [
            _format_flow_block("Текущий поток", current_flow, now),
            _format_flow_block("Следующий поток", next_flow, now),
        ]
    )
    await callback.message.answer(text, reply_markup=flows_menu_kb(can_create_paid))
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
    await _show_prices_screen_message(callback.message, session)
    await callback.answer()


async def _show_mailings_screen(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    enabled = await get_mailings_enabled(session)
    override = await get_setting(session, "mailings_enabled_override")
    status = "включено" if enabled else "выключено"
    source = "override" if override is not None else "env"
    text = f"Рассылки: {status} ({source})"
    await callback.message.answer(text, reply_markup=mailings_menu_kb(enabled))
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
    ]
    rows.extend(back_menu_kb("admin:mailings").inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_users_search(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    await state.set_state(UserSearchState.waiting_query)
    await callback.message.answer(
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
    await callback.message.answer("Промокоды:", reply_markup=promos_menu_kb())
    await callback.answer()


async def _show_shop_screen(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    await _show_shop_screen_message(callback.message, session)
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


async def _send_shop_preview(
    message: types.Message, session: AsyncSession
) -> None:
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
        "mailing_sent": "Рассылка отправлена",
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
        k: v
        for k, v in payload.items()
        if k not in {"actor_tg_id", "action", "tg_id"}
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
    if section == "flows":
        await _show_flows_screen(callback, session)
        return
    elif section == "prices":
        await _show_prices_screen(callback, session)
        return
    elif section == "texts":
        await callback.message.answer(
            "Выберите шаблон:", reply_markup=templates_list_kb()
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
            await callback.message.answer(
                "Лог пуст.", reply_markup=back_menu_kb("admin:menu")
            )
            await callback.answer()
            return
        blocks = [_format_audit_log(entry) for entry in logs]
        await callback.message.answer(
            "\n".join(blocks), reply_markup=back_menu_kb("admin:menu")
        )
        await callback.answer()
        return
    elif section == "menu":
        await callback.message.answer("Админ-панель:", reply_markup=_admin_keyboard())
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
            await callback.message.answer(
                "Введите новое значение числом.",
                reply_markup=back_menu_kb("admin:prices"),
            )
            await callback.answer()
            return
    elif section.startswith("mailings:"):
        parts = section.split(":")
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
            await callback.message.answer(
                "Выберите аудиторию:", reply_markup=_mailings_custom_audience_kb()
            )
            await callback.answer()
            return
        if len(parts) == 3 and parts[1] == "custom":
            audience = parts[2]
            if audience not in ("all", "active", "former"):
                await callback.answer("Неизвестная аудитория", show_alert=True)
                return
            await state.set_state(CustomMailingState.waiting_text)
            await state.update_data(audience=audience)
            await callback.message.answer(
                "Введите текст рассылки одним сообщением.",
                reply_markup=back_menu_kb("admin:mailings"),
            )
            await callback.answer()
            return
        if len(parts) == 3 and parts[1] == "run":
            mode = parts[2]
            if mode not in (
                "free_end_minus_7",
                "free_end_minus_3",
                "paid_end_minus_3",
                "paid_end_minus_1",
                    "free_start_minus_7",
                    "free_start_minus_3",
                    "paid_start_minus_7",
                    "paid_start_minus_3",
            ):
                await callback.answer("Неизвестный режим", show_alert=True)
                return
            enabled = await get_mailings_enabled(session)
            if not enabled:
                await callback.message.answer(
                    "⛔ Рассылки выключены. Включите в админке."
                )
                await callback.answer()
                return
            sent_current, sent_former = await send_manual_mailings(
                session, callback.message.bot, mode
            )
            await session.commit()
            await callback.message.answer(
                f"Готово. Отправлено: текущие {sent_current}, бывшие {sent_former}."
            )
            await callback.answer()
            return
    elif section.startswith("shop:"):
        parts = section.split(":")
        if len(parts) == 2 and parts[1] == "texts":
            await callback.message.answer(
                "Тексты витрины:", reply_markup=shop_texts_kb()
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

        now = datetime.now(timezone.utc)
        membership = await membership_repo.get_latest_membership(session, user_id=user.id)
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
            membership.access_start_at = flow.start_at
            membership.access_end_at = flow.end_at
            membership.grace_end_at = compute_grace_end(
                flow.end_at, effective.grace_days
            )
            await grant_access(callback.message.bot, user.tg_id)
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
            await callback.message.answer("✅ Доступ выдан.")
            await callback.answer()
            return

        if action == "revoke":
            await revoke_access(callback.message.bot, user.tg_id)
            if membership:
                membership.status = MembershipStatus.EXPIRED
            await add_audit_log(
                session,
                action="admin_user_action",
                payload={
                    "tg_id": user.tg_id,
                    "action": "revoke_access",
                    "actor_tg_id": callback.from_user.id,
                },
                actor_user_id=admin_user.id,
            )
            await session.commit()
            await callback.message.answer("⛔ Доступ забран.")
            await callback.answer()
            return

        if action == "extend7":
            if not membership:
                await callback.message.answer("Нет участия для продления.")
                await callback.answer()
                return
            membership.access_end_at = membership.access_end_at + timedelta(days=7)
            membership.grace_end_at = compute_grace_end(
                membership.access_end_at, effective.grace_days
            )
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
            await session.commit()
            await callback.message.answer("✅ Продлено на 7 дней.")
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
            now = datetime.now(timezone.utc)
            free_flow = await flow_repo.get_active_free_flow(session, now)
            if free_flow is None:
                free_flow = await flow_repo.get_next_free_flow(session, now)
            if free_flow is None:
                await callback.message.answer("Бесплатный поток не найден.")
                await callback.answer()
                return
            start_at = free_flow.end_at + timedelta(days=1)
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
            await callback.message.answer("Пришлите новый текст одним сообщением.")
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

    await callback.message.answer(text)
    await callback.answer()


@router.message(FlowEditState.waiting_start)
async def flow_edit_start_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        return
    try:
        start_at = datetime.strptime(message.text.strip(), "%Y-%m-%d").replace(
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
        end_at = datetime.strptime(message.text.strip(), "%Y-%m-%d").replace(
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
        value = int(message.text.strip())
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
    query = message.text.strip()
    if not query:
        await message.answer("Введите @username или числовой tg_id.")
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
    has_access = (
        membership is not None
        and membership.status == MembershipStatus.ACTIVE
        and membership.access_end_at >= now
    )

    lines = [
        f"tg_id: {user.tg_id}",
        f"username: @{user.username}" if user.username else "username: —",
        f"имя: {user.first_name or ''} {user.last_name or ''}".strip()
        or "имя: —",
        f"доступ сейчас: {'да' if has_access else 'нет'}",
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
    await message.answer("\n".join(lines), reply_markup=user_card_kb(user.id))


@router.message(PromoCreateState.waiting_code)
async def promo_create_code_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        return
    code = message.text.strip().upper()
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


@router.message(PromoCreateState.waiting_value)
async def promo_create_value_handler(
    message: types.Message, session: AsyncSession, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_tg_ids:
        return
    try:
        value = int(message.text.strip())
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
        value = int(message.text.strip())
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
    raw = message.text.strip()
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
    raw = message.text.strip()
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
    code = message.text.strip().upper()
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
    if audience not in ("all", "active", "former"):
        await state.clear()
        await message.answer("Аудитория не найдена.")
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите текст рассылки одним сообщением.")
        return
    enabled = await get_mailings_enabled(session)
    if not enabled:
        await state.clear()
        await message.answer("⛔ Рассылки выключены. Включите в админке.")
        return
    sent = await send_custom_broadcast(session, message.bot, audience, text)
    await session.commit()
    await state.clear()
    await message.answer(
        f"Готово. Отправлено: {sent}", reply_markup=back_menu_kb("admin:mailings")
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
        value = message.text.strip()
        if not value:
            await message.answer("Введите непустое значение.")
            return
        await set_setting(session, "shop_free_label", value)
    else:
        try:
            value = int(message.text.strip())
        except ValueError:
            await message.answer("Введите целое число.")
            return
        if not (0 <= value <= 1_000_000):
            await message.answer("Цена должна быть в диапазоне 0..1_000_000.")
            return
        mapping = {
            "intro": "shop_intro_price",
            "renewal": "shop_renewal_price",
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
    data = await state.get_data()
    key = data.get("template_key")
    if not key or key not in DEFAULT_TEMPLATES:
        await state.clear()
        await message.answer("Шаблон не найден.")
        return

    await upsert_template(session, key, message.text)
    await session.commit()
    await state.clear()

    await message.answer("✅ Сохранено.")
    await message.answer(
        f"Ключ: {key}\n\nТекст:\n{message.text}",
        reply_markup=template_card_kb(key),
    )
