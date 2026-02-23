from datetime import datetime, timezone

from aiogram import Router, types
from sqlalchemy.ext.asyncio import AsyncSession

from bot.repositories.users import get_user_by_tg_id
from bot.repositories.memberships import get_active_membership


router = Router()


@router.chat_join_request()
async def approve_join_request(
    join_request: types.ChatJoinRequest, session: AsyncSession
) -> None:
    user = await get_user_by_tg_id(session, join_request.from_user.id)
    if user is None:
        await join_request.bot.decline_chat_join_request(
            chat_id=join_request.chat.id, user_id=join_request.from_user.id
        )
        return

    membership = await get_active_membership(session, user.id)
    now = datetime.now(timezone.utc)
    if membership is None or membership.access_end_at < now:
        await join_request.bot.decline_chat_join_request(
            chat_id=join_request.chat.id, user_id=join_request.from_user.id
        )
        return

    await join_request.bot.approve_chat_join_request(
        chat_id=join_request.chat.id, user_id=join_request.from_user.id
    )
