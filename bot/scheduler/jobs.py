import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.access_control.service import revoke_access
from bot.db.models import Membership, MembershipStatus, PaymentStatus
from bot.repositories import flows as flow_repo
from bot.repositories import memberships as membership_repo
from bot.repositories import payments as payment_repo
from bot.repositories import users as user_repo
from bot.services.mailings import send_auto_end_mailings, send_flow_mailings
from bot.services.payments import confirm_payment, notify_payment_status
from bot.services.settings import get_mailings_enabled
from bot.payments.adapter import PaymentAdapter
from config import settings

logger = logging.getLogger(__name__)


async def expire_memberships(session: AsyncSession, bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    memberships = await membership_repo.list_memberships_to_expire(session, now)
    for membership in memberships:
        membership.status = MembershipStatus.EXPIRED
        user = await user_repo.get_user_by_id(session, membership.user_id)
        if user:
            await revoke_access(bot, user.tg_id)
    await session.commit()


async def enforce_pay_later_deadlines(session: AsyncSession, bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(Membership)
        .where(Membership.status == MembershipStatus.ACTIVE)
        .where(Membership.pay_later_deadline_at.is_not(None))
        .where(Membership.pay_later_deadline_at <= now)
    )
    for membership in result.scalars().all():
        membership.status = MembershipStatus.EXPIRED
        user = await user_repo.get_user_by_id(session, membership.user_id)
        if user:
            await revoke_access(bot, user.tg_id)
    await session.commit()


async def remove_non_renewed_on_flow_start(
    session: AsyncSession, bot: Bot, flow_id: int
) -> None:
    now = datetime.now(timezone.utc).date()
    flow = await flow_repo.get_flow_by_id(session, flow_id)
    if flow is None or flow.start_at.date() != now:
        return

    result = await session.execute(
        select(Membership)
        .where(Membership.status == MembershipStatus.ACTIVE)
        .where(Membership.access_end_at < flow.start_at)
        .where(Membership.pay_later_used_at.is_(None))
    )
    for membership in result.scalars().all():
        # Критично: если не было "оплатить позже" и оплаты, удаляем в первый день потока.
        membership.status = MembershipStatus.EXPIRED
        user = await user_repo.get_user_by_id(session, membership.user_id)
        if user:
            await revoke_access(bot, user.tg_id)
    await session.commit()


async def remove_non_renewed_on_paid_flows(
    session: AsyncSession, bot: Bot
) -> None:
    flows = await flow_repo.list_flows(session)
    for flow in flows:
        if flow.is_free:
            continue
        await remove_non_renewed_on_flow_start(session, bot, flow.id)


async def check_pending_payments(
    session: AsyncSession, bot: Bot, adapter: PaymentAdapter
) -> None:
    now = datetime.now(timezone.utc)
    pending = await payment_repo.list_pending_payments(session, now)
    for payment in pending:
        status = await adapter.get_payment_status(payment.external_id)
        if status == PaymentStatus.PAID:
            await confirm_payment(session, bot, payment, paid_at=now)
        elif status == PaymentStatus.FAILED:
            payment.status = PaymentStatus.FAILED
            await notify_payment_status(
                session, bot, payment.user_id, "payment_failed"
            )
        elif status == PaymentStatus.EXPIRED:
            payment.status = PaymentStatus.EXPIRED
            await notify_payment_status(
                session, bot, payment.user_id, "payment_expired"
            )
    await session.commit()


async def send_scheduled_mailings(session: AsyncSession, bot: Bot) -> None:
    now_utc = datetime.now(timezone.utc)
    tz = ZoneInfo(settings.scheduler_timezone)
    now_local_date = now_utc.astimezone(tz).date()
    target_dates = {
        now_local_date + timedelta(days=7),
        now_local_date + timedelta(days=3),
    }

    enabled = await get_mailings_enabled(session)
    flows = await flow_repo.list_flows(session)
    matched_flows = []
    for flow in flows:
        flow_start_local_date = flow.start_at.astimezone(tz).date()
        if flow_start_local_date not in target_dates:
            continue

        matched_flows.append(flow)
    matched_flows_meta = [
        {
            "id": f.id,
            "start_at": f.start_at.isoformat(),
            "end_at": f.end_at.isoformat(),
            "is_free": f.is_free,
        }
        for f in matched_flows
    ]

    logger.info(
        "Scheduled start mailings tick",
        extra={
            "enabled": enabled,
            "tz": settings.scheduler_timezone,
            "now_local_date": str(now_local_date),
            "target_dates_local": [str(d) for d in target_dates],
            "matched_flows": matched_flows_meta,
        },
    )

    for flow in matched_flows:
        await send_flow_mailings(session, bot, flow.id, flow.start_at)
    await session.commit()


async def auto_mailings(bot: Bot, sessionmaker) -> None:
    async with sessionmaker() as session:
        enabled = await get_mailings_enabled(session)
        if not enabled:
            return
        now = datetime.now(timezone.utc)
        tz = ZoneInfo(settings.scheduler_timezone)
        now_local_date = now.astimezone(tz).date()
        logger.info(
            "Auto end mailings tick",
            extra={
                "enabled": enabled,
                "tz": settings.scheduler_timezone,
                "now_local_date": str(now_local_date),
                "now_utc": now.isoformat(),
            },
        )
        await send_auto_end_mailings(session, bot, now)
        await session.commit()
