from datetime import datetime, timezone

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.repositories import memberships as membership_repo
from bot.repositories.users import get_or_create_user
from bot.services.memberships import apply_pay_later, evaluate_pay_later
from bot.ui.formatters import format_local_date
from config import settings

router = Router()


def _pay_later_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить позже", callback_data="pay_later")]
        ]
    )


@router.message(Command("status"))
@router.message(lambda m: m.text == "👤 Мой статус")
async def status_handler(message: types.Message, session: AsyncSession) -> None:
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
    membership = await membership_repo.get_active_membership(session, user_id=user.id)
    text = (
        "👤 Статус участия\n\n"
        "Активного доступа сейчас нет.\n"
        "Чтобы присоединиться, откройте «🛍 Тарифы»."
    )
    keyboard = None
    if membership:
        if membership.access_end_at >= now:
            text = (
                "👤 Статус участия\n\n"
                "✅ Доступ активен\n"
                f"Доступ до: {format_local_date(membership.access_end_at)}"
            )
        elif membership.grace_end_at >= now:
            text = (
                "👤 Статус участия\n\n"
                "Доступ завершён. Сейчас ещё действует льготный период продления.\n"
                "Льготная цена доступна до: "
                f"{format_local_date(membership.grace_end_at)}"
            )

        pay_later = await evaluate_pay_later(session, user.id, now)
        if pay_later.eligible:
            keyboard = _pay_later_keyboard()
        elif (
            membership.pay_later_deadline_at and membership.pay_later_deadline_at > now
        ):
            text += (
                "\n⏳ Отсрочка до: "
                f"{format_local_date(membership.pay_later_deadline_at)}"
            )

    await message.answer(text, reply_markup=keyboard)


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
    membership = await membership_repo.get_active_membership(session, user_id=user.id)
    if not membership:
        await callback.answer("Нет активной подписки", show_alert=True)
        return

    ok, text = await apply_pay_later(session, user_id=user.id, now=now)
    if not ok:
        await callback.answer(text, show_alert=True)
        return
    await session.commit()
    await callback.message.answer(text)
    await callback.answer()
