"""Read-only entitlement audit. Never sends messages or changes Telegram access."""

import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot
from sqlalchemy import select, text

from bot.db.models import Flow, Membership, Payment, User
from bot.db.session import AsyncSessionLocal
from bot.services.entitlements import has_unresolved_payment, has_valid_access
from config import settings


async def main():
    now = datetime.now(timezone.utc)
    report = {
        "at": now.isoformat(),
        "automatic_revoke_enabled": settings.revoke_jobs_enabled,
    }
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        users = list((await session.scalars(select(User))).all())
        memberships = list((await session.scalars(select(Membership))).all())
        payments = list((await session.scalars(select(Payment))).all())
        report["users"] = len(users)
        report["exempt"] = sum(u.access_exempt for u in users)
        report["payment_statuses"] = dict(Counter(p.status for p in payments))
        report["valid_access"] = sum(
            [await has_valid_access(session, u.id, now) for u in users]
        )
        for job in ("expiry", "pay_later"):
            grouped = defaultdict(set)
            for m in memberships:
                if m.status != "active":
                    continue
                if job == "expiry":
                    due = m.grace_end_at < now and (
                        m.pay_later_deadline_at is None
                        or m.pay_later_deadline_at <= now
                    )
                else:
                    due = (
                        m.pay_later_deadline_at is not None
                        and m.pay_later_deadline_at <= now
                    )
                if due:
                    grouped[m.user_id].add(m.id)
            counts = Counter()
            for uid, stale_ids in grouped.items():
                if await has_valid_access(
                    session, uid, now, exclude_membership_ids=stale_ids
                ):
                    counts["preserved_access"] += 1
                elif await has_unresolved_payment(session, uid):
                    counts["held_payment"] += 1
                else:
                    counts["without_access"] += 1
            report[job] = dict(counts)
        future = await session.scalars(
            select(Flow).where(Flow.end_at >= now).order_by(Flow.start_at)
        )
        report["flows"] = [
            dict(
                id=f.id,
                start=f.start_at.isoformat(),
                end=f.end_at.isoformat(),
                sales_open=f.sales_open_at.isoformat(),
                sales_close=f.sales_close_at.isoformat(),
            )
            for f in future
        ]
        exempt_ids = [u.tg_id for u in users if u.access_exempt]
        await session.rollback()
    bot = Bot(settings.bot_token)
    try:
        me = await bot.get_me()
        webhook = await bot.get_webhook_info()
        report["telegram"] = {
            "bot": me.username,
            "webhook_configured": bool(webhook.url),
            "pending_updates": webhook.pending_update_count,
        }
        for label, chat_id in (
            ("channel", settings.primary_channel_id),
            ("group", settings.secondary_discussion_id),
        ):
            member = await bot.get_chat_member(chat_id, me.id)
            counts = Counter()
            for tg_id in exempt_ids:
                status = await bot.get_chat_member(chat_id, tg_id)
                counts[str(status.status)] += 1
            report[label] = dict(
                bot_status=str(member.status),
                can_invite=bool(getattr(member, "can_invite_users", False)),
                can_restrict=bool(getattr(member, "can_restrict_members", False)),
                exempt_members=dict(counts),
            )
    finally:
        await bot.session.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
