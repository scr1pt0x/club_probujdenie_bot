import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.access_control.service import revoke_access
from bot.db.models import Membership, MembershipStatus, Payment, PaymentStatus
from bot.payments.adapter import PaymentAdapter
from bot.payments.verification import validate_remote_payment
from bot.repositories import flows as flow_repo
from bot.repositories import memberships as membership_repo
from bot.repositories import payments as payment_repo
from bot.repositories import users as user_repo
from bot.repositories.audit_log import add_audit_log
from bot.services.entitlements import has_valid_access
from bot.services.mailings import (
    send_auto_end_mailings,
    send_flow_mailings,
    send_pay_later_deadline_reminders,
)
from bot.services.payments import confirm_payment, notify_payment_status
from bot.services.settings import get_mailings_enabled
from bot.services.texts import get_text
from config import settings

logger = logging.getLogger(__name__)


def _pending_payment_deadline(payment: Payment) -> datetime:
    if payment.expires_at is not None:
        return payment.expires_at
    # Legacy payments did not have expires_at. Provider status is checked before
    # this fallback is used, so a succeeded legacy payment is still confirmed.
    return payment.created_at + timedelta(hours=24)


def _expiration_notice_is_timely(payment: Payment, now: datetime) -> bool:
    return _pending_payment_deadline(payment) >= now - timedelta(days=1)


def _is_revoke_jobs_enabled() -> bool:
    return settings.revoke_jobs_enabled


def _is_mass_revoke_blocked(job_name: str, candidates_count: int) -> bool:
    if candidates_count <= settings.max_revoke_per_run:
        return False
    logger.error(
        "Mass revoke blocked by safety limit",
        extra={
            "job": job_name,
            "candidates_count": candidates_count,
            "max_revoke_per_run": settings.max_revoke_per_run,
        },
    )
    return True


def _group_memberships_by_user(
    memberships: list[Membership],
) -> dict[int, list[Membership]]:
    grouped: dict[int, list[Membership]] = defaultdict(list)
    for membership in memberships:
        grouped[membership.user_id].append(membership)
    return dict(grouped)


async def _record_automatic_revoke(
    session: AsyncSession,
    *,
    job: str,
    user_id: int,
    tg_id: int,
    memberships: list[Membership],
) -> None:
    await add_audit_log(
        session,
        action="automatic_access_revoke",
        payload={
            "job": job,
            "user_id": user_id,
            "tg_id": tg_id,
            "membership_ids": [membership.id for membership in memberships],
        },
    )


async def expire_memberships(session: AsyncSession, bot: Bot) -> None:
    if not _is_revoke_jobs_enabled():
        logger.warning("Revoke jobs disabled: expire_memberships skipped")
        return
    now = datetime.now(timezone.utc)
    memberships = await membership_repo.list_memberships_to_expire(session, now)
    grouped = _group_memberships_by_user(memberships)
    revoke_user_ids = {
        user_id
        for user_id, stale in grouped.items()
        if not await has_valid_access(
            session,
            user_id,
            now,
            exclude_membership_ids={membership.id for membership in stale},
        )
    }

    if _is_mass_revoke_blocked("expire_memberships", len(revoke_user_ids)):
        return

    for user_id, stale in grouped.items():
        user = await user_repo.lock_user_by_id(session, user_id)
        excluded_ids = {membership.id for membership in stale}
        # Recheck only after taking the same lock used by payment confirmation.
        # A payment committed while the job was building its candidate list must
        # protect the participant from a stale Telegram ban.
        keep_access = await has_valid_access(
            session, user_id, now, exclude_membership_ids=excluded_ids
        )
        if keep_access or user is None:
            for membership in stale:
                membership.status = MembershipStatus.EXPIRED
            await session.commit()
            continue

        result = await revoke_access(bot, user.tg_id)
        if result.successful:
            for membership in stale:
                membership.status = MembershipStatus.EXPIRED
            if not result.protected:
                await _record_automatic_revoke(
                    session,
                    job="expire_memberships",
                    user_id=user_id,
                    tg_id=user.tg_id,
                    memberships=stale,
                )
        await session.commit()


async def enforce_pay_later_deadlines(session: AsyncSession, bot: Bot) -> None:
    if not _is_revoke_jobs_enabled():
        logger.warning("Revoke jobs disabled: enforce_pay_later_deadlines skipped")
        return
    now = datetime.now(timezone.utc)
    revoke_text = await get_text(session, "pay_later_access_revoked")
    result = await session.execute(
        select(Membership)
        .where(Membership.status == MembershipStatus.ACTIVE)
        .where(Membership.pay_later_deadline_at.is_not(None))
        .where(Membership.pay_later_deadline_at <= now)
    )
    memberships = list(result.scalars().all())
    grouped = _group_memberships_by_user(memberships)
    revoke_candidate_ids = {
        user_id
        for user_id, overdue in grouped.items()
        if not await has_valid_access(
            session,
            user_id,
            now,
            exclude_membership_ids={membership.id for membership in overdue},
        )
    }
    if _is_mass_revoke_blocked(
        "enforce_pay_later_deadlines", len(revoke_candidate_ids)
    ):
        return
    for user_id, overdue in grouped.items():
        user = await user_repo.lock_user_by_id(session, user_id)
        excluded_ids = {membership.id for membership in overdue}
        keep_access = await has_valid_access(
            session, user_id, now, exclude_membership_ids=excluded_ids
        )
        if keep_access or user is None:
            for membership in overdue:
                membership.status = MembershipStatus.EXPIRED
            await session.commit()
            continue

        result = await revoke_access(bot, user.tg_id)
        if not result.successful:
            # Keep all rows active so the next run retries instead of hiding a
            # partial Telegram failure.
            await session.commit()
            continue

        for membership in overdue:
            membership.status = MembershipStatus.EXPIRED
        if not result.protected:
            await _record_automatic_revoke(
                session,
                job="enforce_pay_later_deadlines",
                user_id=user_id,
                tg_id=user.tg_id,
                memberships=overdue,
            )
            try:
                await bot.send_message(user.tg_id, revoke_text)
            except Exception:
                logger.exception(
                    "Failed to notify user on pay-later expiry",
                    extra={"user_id": user_id},
                )
        await session.commit()


async def check_pending_payments(
    session: AsyncSession, bot: Bot, adapter: PaymentAdapter
) -> None:
    now = datetime.now(timezone.utc)
    pending = await payment_repo.list_pending_payments(session)
    for listed_payment in pending:
        try:
            payment = await payment_repo.get_payment_by_external_id(
                session, listed_payment.external_id
            )
            if payment is None or payment.status != PaymentStatus.PENDING:
                await session.commit()
                continue
            if await user_repo.lock_user_by_id(session, payment.user_id) is None:
                await session.commit()
                continue
            payment = await payment_repo.get_payment_by_external_id(
                session, listed_payment.external_id
            )
            if payment is None or payment.status != PaymentStatus.PENDING:
                await session.commit()
                continue
            remote = await adapter.get_payment(payment.external_id)
            validation_error = validate_remote_payment(
                remote,
                external_id=payment.external_id,
                internal_payment_id=payment.id,
                user_id=payment.user_id,
                amount_rub=payment.amount_rub,
                currency=payment.currency,
            )
            if validation_error:
                payment.status = PaymentStatus.NEEDS_REVIEW
                logger.error(
                    "Pending payment verification mismatch",
                    extra={
                        "payment_id": payment.id,
                        "external_id": payment.external_id,
                        "reason": validation_error,
                    },
                )
                await notify_payment_status(
                    session,
                    bot,
                    payment.user_id,
                    "payment_needs_review",
                    dedupe_key=f"payment:{payment.id}:payment_needs_review",
                )
                await session.commit()
                continue

            remote_status = remote.get("status")
            status = {
                "succeeded": PaymentStatus.PAID,
                "canceled": PaymentStatus.FAILED,
            }.get(remote_status, PaymentStatus.PENDING)
            if status == PaymentStatus.PAID:
                await confirm_payment(session, bot, payment, paid_at=now)
            elif status == PaymentStatus.FAILED:
                payment.status = PaymentStatus.FAILED
                if _expiration_notice_is_timely(payment, now):
                    await notify_payment_status(
                        session,
                        bot,
                        payment.user_id,
                        "payment_failed",
                        dedupe_key=f"payment:{payment.id}:payment_failed",
                    )
            elif status == PaymentStatus.EXPIRED:
                payment.status = PaymentStatus.EXPIRED
                if _expiration_notice_is_timely(payment, now):
                    await notify_payment_status(
                        session,
                        bot,
                        payment.user_id,
                        "payment_expired",
                        dedupe_key=f"payment:{payment.id}:payment_expired",
                    )
            else:
                # Release the per-user row lock even when YooKassa is still
                # pending and no database values changed.
                await session.commit()
                continue

            # Commit each processed payment independently so one failure
            # does not keep previously handled payments in PENDING state.
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception(
                "Failed to process pending payment",
                extra={"payment_id": payment.id, "external_id": payment.external_id},
            )


async def send_scheduled_mailings(session: AsyncSession, bot: Bot) -> None:
    now_utc = datetime.now(timezone.utc)
    tz = ZoneInfo(settings.scheduler_timezone)
    now_local_date = now_utc.astimezone(tz).date()
    target_dates = {
        now_local_date + timedelta(days=7),
        now_local_date + timedelta(days=3),
    }

    enabled = await get_mailings_enabled(session)
    if not enabled:
        logger.info("Scheduled mailings disabled, skipping")
        return
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
        await send_pay_later_deadline_reminders(session, bot, now)
        await session.commit()
