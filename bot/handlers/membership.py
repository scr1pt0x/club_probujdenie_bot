from datetime import datetime, timedelta, timezone

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.repositories.users import get_or_create_user
from bot.repositories import memberships as membership_repo
from bot.services.flows import get_next_paid_flow
from bot.services.memberships import apply_pay_later
from config import settings


router = Router()


def _pay_later_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить позже", callback_data="pay_later")]
        ]
    )


@router.message(Command("status"))
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
    next_flow = await get_next_paid_flow(session, now)

    text = "Статус: нет активной подписки."
    keyboard = None
    if membership:
        text = (
            "Статус: активная подписка.\n"
            f"Доступ до: {membership.access_end_at.date()}\n"
        )
        if next_flow and now < next_flow.start_at:
            keyboard = _pay_later_keyboard()

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
