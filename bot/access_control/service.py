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


async def grant_access(bot: Bot, tg_id: int) -> None:
    await _safe_unban(bot, settings.primary_channel_id, tg_id)
    await _safe_unban(bot, settings.secondary_discussion_id, tg_id)


async def revoke_access(bot: Bot, tg_id: int) -> None:
    await _safe_ban(bot, settings.primary_channel_id, tg_id)
    await _safe_ban(bot, settings.secondary_discussion_id, tg_id)
