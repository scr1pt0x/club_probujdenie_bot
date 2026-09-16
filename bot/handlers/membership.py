from datetime import datetime, timezone

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.access_control.service import grant_access
from bot.repositories import memberships as membership_repo
from bot.repositories.users import (
    get_or_create_user,
    lock_user_by_id,
    lock_user_by_tg_id,
)
from bot.services.entitlements import has_valid_access
from bot.services.memberships import apply_pay_later, evaluate_pay_later
from bot.ui.formatters import format_local_date
from bot.ui.keyboards import access_links_kb, back_home_kb
from bot.ui.navigation import edit_screen, send_clean_screen
from config import settings

router = Router()


def _with_links(keyboard):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔗 Ссылки на канал и группу", callback_data="nav:access_links"
                )
            ],
            *keyboard.inline_keyboard,
        ]
    )


def _pay_later_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить отсрочку", callback_data="pay_later"
                )
            ],
            [InlineKeyboardButton(text="← Главное меню", callback_data="nav:home")],
        ]
    )


async def _status_content(
    session: AsyncSession, tg_user: types.User
) -> tuple[str, InlineKeyboardMarkup]:
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
    if user.access_exempt:
        return (
            "👤 Мой доступ\n\n🛡 Льготный доступ активен. Оплата не требуется.",
            _with_links(back_home_kb()),
        )
    membership = await membership_repo.get_active_membership(session, user_id=user.id)
    text = (
        "👤 Мой доступ\n\n"
        "Сейчас активного участия нет.\n"
        "Откройте «✨ Участие», чтобы посмотреть условия."
    )
    keyboard = back_home_kb()
    if membership:
        if membership.access_end_at >= now:
            text = (
                "👤 Мой доступ\n\n"
                "✅ Активен\n"
                f"До {format_local_date(membership.access_end_at)}"
            )
        elif membership.grace_end_at >= now:
            text = (
                "👤 Мой доступ\n\n"
                "🕓 Основной период завершён\n"
                "Льготное продление доступно до "
                f"{format_local_date(membership.grace_end_at)}"
            )

        pay_later = await evaluate_pay_later(session, user.id, now)
        if pay_later.eligible:
            keyboard = _pay_later_keyboard()
            text += "\n\nМожно оформить отсрочку оплаты."
        elif (
            membership.pay_later_deadline_at and membership.pay_later_deadline_at > now
        ):
            text += (
                "\n⏳ Отсрочка до "
                f"{format_local_date(membership.pay_later_deadline_at)}"
            )
    if await has_valid_access(session, user.id, now):
        keyboard = _with_links(keyboard)
        if not membership:
            text = "👤 Мой доступ\n\n✅ Оплата подтверждена, доступ активен."
    return text, keyboard


@router.callback_query(lambda c: c.data == "nav:access_links")
async def access_links_handler(callback: types.CallbackQuery, session: AsyncSession):
    user = await lock_user_by_tg_id(session, callback.from_user.id)
    if user is None or not await has_valid_access(
        session, user.id, datetime.now(timezone.utc)
    ):
        await callback.answer(
            "Действующего доступа нет. Откройте раздел оплаты.", show_alert=True
        )
        return
    links = await grant_access(callback.bot, user.tg_id)
    await session.commit()
    keyboard = access_links_kb(links.channel_link, links.group_link)
    await edit_screen(
        callback.message,
        "Нажмите ссылки и отправьте заявки на вступление."
        if links.successful
        else (
            "Доступ подтверждён, но не все ссылки получены. "
            "Попробуйте позже или обратитесь к администратору."
        ),
        reply_markup=keyboard or back_home_kb(),
    )
    await callback.answer()


@router.message(Command("status"))
@router.message(lambda m: m.text == "👤 Мой статус")
async def status_handler(message: types.Message, session: AsyncSession) -> None:
    text, keyboard = await _status_content(session, message.from_user)
    await send_clean_screen(message, text, reply_markup=keyboard)


@router.callback_query(lambda c: c.data == "nav:status")
async def status_navigation_handler(
    callback: types.CallbackQuery, session: AsyncSession
) -> None:
    text, keyboard = await _status_content(session, callback.from_user)
    await edit_screen(callback.message, text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(lambda c: c.data == "pay_later")
async def pay_later_handler(
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
    user = await lock_user_by_id(session, user.id)
    if user is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    membership = await membership_repo.get_active_membership(session, user_id=user.id)
    if not membership:
        await callback.answer("Нет активной подписки", show_alert=True)
        return

    ok, text = await apply_pay_later(session, user_id=user.id, now=now)
    if not ok:
        await callback.answer(text, show_alert=True)
        return
    await session.commit()
    await edit_screen(
        callback.message,
        f"✅ {text}\n\nНе забудьте оплатить участие до этой даты.",
        reply_markup=back_home_kb(),
    )
    await callback.answer()
