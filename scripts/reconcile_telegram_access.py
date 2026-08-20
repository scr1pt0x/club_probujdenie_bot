"""Find and optionally remove inactive users who still have Telegram access."""

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot
from sqlalchemy import exists, select

from bot.access_control.service import revoke_access
from bot.db.models import (
    Flow,
    Membership,
    MembershipStatus,
    Payment,
    PaymentStatus,
    User,
)
from bot.db.session import AsyncSessionLocal
from bot.repositories.audit_log import add_audit_log
from bot.repositories.users import lock_user_by_tg_id
from bot.services.entitlements import has_valid_access
from config import settings

PRESENT_STATUSES = {"member", "restricted"}
PROTECTED_STATUSES = {"administrator", "creator"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true", help="Apply removals; default is dry-run"
    )
    parser.add_argument("--limit", type=int, default=30)
    return parser.parse_args()


async def _inactive_tg_ids() -> list[int]:
    now = datetime.now(timezone.utc)
    active_membership = exists(
        select(Membership.id).where(
            Membership.user_id == User.id,
            Membership.status == MembershipStatus.ACTIVE,
            Membership.grace_end_at >= now,
        )
    ).correlate(User)
    future_payment = exists(
        select(Payment.id)
        .join(Flow, Payment.flow_id == Flow.id)
        .where(
            Payment.user_id == User.id,
            Payment.status == PaymentStatus.PAID,
            Flow.end_at >= now,
        )
    ).correlate(User)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User.tg_id)
            .where(~active_membership, ~future_payment)
            .order_by(User.id)
        )
        return list(result.scalars().all())


async def main() -> None:
    args = parse_args()
    if args.limit < 1:
        raise SystemExit("--limit must be positive")

    bot = Bot(settings.bot_token)
    candidates: list[int] = []
    skipped_errors = 0
    skipped_protected = 0
    try:
        for tg_id in await _inactive_tg_ids():
            if tg_id in settings.admin_tg_ids:
                skipped_protected += 1
                continue
            try:
                channel = await bot.get_chat_member(settings.primary_channel_id, tg_id)
                group = await bot.get_chat_member(
                    settings.secondary_discussion_id, tg_id
                )
            except Exception:
                skipped_errors += 1
                continue

            statuses = {str(channel.status), str(group.status)}
            if statuses & PROTECTED_STATUSES:
                skipped_protected += 1
            elif statuses & PRESENT_STATUSES:
                candidates.append(tg_id)
            await asyncio.sleep(0.05)

        print(
            f"candidates={len(candidates)} protected={skipped_protected} "
            f"errors={skipped_errors} mode={'apply' if args.apply else 'dry-run'}"
        )
        if not args.apply:
            return
        if len(candidates) > args.limit:
            raise SystemExit(
                f"Safety limit exceeded: {len(candidates)} candidates > {args.limit}"
            )

        removed = 0
        failed = 0
        preserved = 0
        for tg_id in candidates:
            async with AsyncSessionLocal() as session:
                user = await lock_user_by_tg_id(session, tg_id)
                if user is None or await has_valid_access(
                    session, user.id, datetime.now(timezone.utc)
                ):
                    preserved += 1
                    await session.commit()
                    continue

                result = await revoke_access(bot, tg_id)
                if result.successful:
                    removed += 1
                    if not result.protected:
                        await add_audit_log(
                            session,
                            action="reconciliation_access_revoke",
                            payload={"user_id": user.id, "tg_id": tg_id},
                        )
                else:
                    failed += 1
                await session.commit()
        print(f"removed={removed} failed={failed} preserved={preserved}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
