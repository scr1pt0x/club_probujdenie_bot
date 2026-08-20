import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccessChangeResult:
    channel_ok: bool
    group_ok: bool
    channel_link: str | None = None
    group_link: str | None = None
    protected: bool = False

    @property
    def successful(self) -> bool:
        return self.channel_ok and self.group_ok


async def _safe_ban(bot: Bot, chat_id: int, tg_id: int) -> bool:
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=tg_id, revoke_messages=False)
        return True
    except TelegramAPIError:
        logger.exception(
            "Failed to ban member from chat",
            extra={"chat_id": chat_id, "tg_id": tg_id},
        )
        return False


async def _safe_unban(bot: Bot, chat_id: int, tg_id: int) -> bool:
    try:
        # Without only_if_banned Telegram may remove an existing member. Granting
        # or refreshing links must never kick somebody who already has access.
        await bot.unban_chat_member(chat_id=chat_id, user_id=tg_id, only_if_banned=True)
        return True
    except TelegramAPIError:
        logger.exception(
            "Failed to unban member in chat",
            extra={"chat_id": chat_id, "tg_id": tg_id},
        )
        return False


async def _safe_invite_link(bot: Bot, chat_id: int, tg_id: int) -> str | None:
    try:
        link = await bot.create_chat_invite_link(
            chat_id=chat_id,
            creates_join_request=True,
            name=f"access-{tg_id}",
            expire_date=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        return link.invite_link
    except TelegramAPIError:
        logger.exception(
            "Failed to create invite link",
            extra={"chat_id": chat_id, "tg_id": tg_id},
        )
        return None


async def grant_access(bot: Bot, tg_id: int) -> AccessChangeResult:
    channel_unbanned = await _safe_unban(bot, settings.primary_channel_id, tg_id)
    group_unbanned = await _safe_unban(bot, settings.secondary_discussion_id, tg_id)
    channel_link = (
        await _safe_invite_link(bot, settings.primary_channel_id, tg_id)
        if channel_unbanned
        else None
    )
    group_link = (
        await _safe_invite_link(bot, settings.secondary_discussion_id, tg_id)
        if group_unbanned
        else None
    )
    return AccessChangeResult(
        channel_ok=channel_unbanned and channel_link is not None,
        group_ok=group_unbanned and group_link is not None,
        channel_link=channel_link,
        group_link=group_link,
    )


async def revoke_access(bot: Bot, tg_id: int) -> AccessChangeResult:
    if tg_id in settings.admin_tg_ids:
        logger.warning("Protected administrator revoke skipped", extra={"tg_id": tg_id})
        return AccessChangeResult(channel_ok=True, group_ok=True, protected=True)
    channel_ok = await _safe_ban(bot, settings.primary_channel_id, tg_id)
    group_ok = await _safe_ban(bot, settings.secondary_discussion_id, tg_id)
    return AccessChangeResult(channel_ok=channel_ok, group_ok=group_ok)
