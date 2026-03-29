import argparse
import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, types
from dotenv import load_dotenv
from sqlalchemy import distinct, select

load_dotenv()

from bot.access_control.service import grant_access
from bot.db.models import Membership, MembershipStatus, User
from bot.db.session import AsyncSessionLocal
from config import settings


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("recover_mass_access")


def _links_kb(
    channel_link: str | None, group_link: str | None
) -> types.InlineKeyboardMarkup | None:
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


async def _load_target_users(limit: int | None) -> list[User]:
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as session:
        query = (
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.status == MembershipStatus.ACTIVE)
            .where(Membership.access_end_at >= now)
            .order_by(User.id.asc())
            .distinct(User.id)
        )
        if limit is not None:
            query = query.limit(limit)
        result = await session.execute(query)
        return list(result.scalars().all())


async def recover_mass_access(
    apply_changes: bool,
    limit: int | None,
    message_text: str,
) -> None:
    users = await _load_target_users(limit)
    logger.info("Users with active access in DB: %s", len(users))
    if not apply_changes:
        logger.info("Dry-run mode. No Telegram actions executed.")
        return

    bot = Bot(token=settings.bot_token)
    processed = 0
    restored = 0
    messaged = 0
    failed = 0

    try:
        for user in users:
            processed += 1
            try:
                links = await grant_access(bot, user.tg_id)
                restored += 1
                kb = _links_kb(links.get("channel_link"), links.get("group_link"))
                await bot.send_message(user.tg_id, message_text, reply_markup=kb)
                messaged += 1
                if processed % 25 == 0:
                    logger.info(
                        "Progress %s/%s (restored=%s, messaged=%s, failed=%s)",
                        processed,
                        len(users),
                        restored,
                        messaged,
                        failed,
                    )
            except Exception:
                failed += 1
                logger.exception(
                    "Failed to restore access for user",
                    extra={"user_id": user.id, "tg_id": user.tg_id},
                )
            await asyncio.sleep(0.05)
    finally:
        await bot.session.close()

    logger.info(
        "Done. processed=%s restored=%s messaged=%s failed=%s",
        processed,
        restored,
        messaged,
        failed,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover Telegram access in bulk")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute recovery actions (default is dry-run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit for number of users",
    )
    parser.add_argument(
        "--message",
        type=str,
        default=(
            "Мы восстановили доступ после технического сбоя.\n"
            "Нажмите кнопки ниже и отправьте заявку на вступление."
        ),
        help="Message text sent to each participant",
    )
    args = parser.parse_args()
    asyncio.run(recover_mass_access(args.apply, args.limit, args.message))


if __name__ == "__main__":
    main()
