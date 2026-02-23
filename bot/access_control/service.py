import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from config import settings

logger = logging.getLogger(__name__)


async def _safe_ban(bot: Bot, chat_id: int, tg_id: int) -> None:
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=tg_id, revoke_messages=True)
    except TelegramAPIError:
        logger.exception(
            "Failed to ban member from chat",
            extra={"chat_id": chat_id, "tg_id": tg_id},
        )


async def _safe_unban(bot: Bot, chat_id: int, tg_id: int) -> None:
    try:
        await bot.unban_chat_member(chat_id=chat_id, user_id=tg_id)
    except TelegramAPIError:
        logger.exception(
            "Failed to unban member in chat",
            extra={"chat_id": chat_id, "tg_id": tg_id},
        )


async def _safe_invite_link(bot: Bot, chat_id: int, tg_id: int) -> str | None:
    try:
        link = await bot.create_chat_invite_link(
            chat_id=chat_id,
            creates_join_request=True,
            name=f"access-{tg_id}",
        )
        return link.invite_link
    except TelegramAPIError:
        logger.exception(
            "Failed to create invite link",
            extra={"chat_id": chat_id, "tg_id": tg_id},
        )
        return None


async def grant_access(bot: Bot, tg_id: int) -> dict[str, str | None]:
    await _safe_unban(bot, settings.primary_channel_id, tg_id)
    await _safe_unban(bot, settings.secondary_discussion_id, tg_id)
    channel_link = await _safe_invite_link(bot, settings.primary_channel_id, tg_id)
    group_link = await _safe_invite_link(bot, settings.secondary_discussion_id, tg_id)
    return {"channel_link": channel_link, "group_link": group_link}


async def revoke_access(bot: Bot, tg_id: int) -> None:
    await _safe_ban(bot, settings.primary_channel_id, tg_id)
    await _safe_ban(bot, settings.secondary_discussion_id, tg_id)
