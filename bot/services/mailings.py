import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.admin.templates import DEFAULT_TEMPLATES
from bot.db.models import Flow, Membership, MembershipStatus, Payment, PaymentStatus, User
from bot.repositories import flows as flow_repo
from bot.repositories.audit_log import add_audit_log, has_action_with_key
from bot.repositories.message_templates import get_template_by_key
from bot.services.texts import get_text
from config import settings
from bot.services.settings import get_mailings_enabled


logger = logging.getLogger(__name__)


async def _get_active_user_ids(session: AsyncSession, now: datetime) -> list[int]:
    result = await session.execute(
        select(distinct(Membership.user_id))
        .where(Membership.status == MembershipStatus.ACTIVE)
        .where(Membership.access_end_at >= now)
    )
    return [row[0] for row in result.all()]


async def _get_active_flow_user_ids(
    session: AsyncSession, flow_id: int, now: datetime
) -> list[int]:
    result = await session.execute(
        select(distinct(Membership.user_id))
        .where(Membership.status == MembershipStatus.ACTIVE)
        .where(Membership.flow_id == flow_id)
        .where(Membership.access_end_at >= now)
    )
    return [row[0] for row in result.all()]


async def _get_former_user_ids(session: AsyncSession) -> list[int]:
    latest_subq = (
        select(
            Membership.user_id,
            func.max(Membership.created_at).label("max_created"),
        )
        .group_by(Membership.user_id)
        .subquery()
    )
    result = await session.execute(
        select(Membership.user_id)
        .join(
            latest_subq,
            (Membership.user_id == latest_subq.c.user_id)
            & (Membership.created_at == latest_subq.c.max_created),
        )
        .where(Membership.status != MembershipStatus.ACTIVE)
    )
    return [row[0] for row in result.all()]


async def _get_flow_participant_user_ids(
    session: AsyncSession, flow_id: int
) -> set[int]:
    result = await session.execute(
        select(distinct(Membership.user_id))
        .where(Membership.flow_id == flow_id)
        .where(Membership.status == MembershipStatus.ACTIVE)
    )
    return {row[0] for row in result.all()}


async def _send_bulk(
    session: AsyncSession,
    bot: Bot,
    user_ids: list[int],
    text: str,
    mailing_key: str | None,
    delay_seconds: float = 0.5,
    idempotent: bool = True,
) -> int:
    if idempotent and mailing_key:
        if await has_action_with_key(session, "mailing_sent", mailing_key):
            return 0

    sent = 0
    for user_id in user_ids:
        result = await session.execute(select(User.tg_id).where(User.id == user_id))
        row = result.first()
        if not row:
            continue
        tg_id = row[0]
        try:
            await bot.send_message(tg_id, text)
            sent += 1
        except Exception:
            # Ошибки Telegram API не должны останавливать рассылку
            pass
        await asyncio.sleep(delay_seconds)

    if idempotent and mailing_key:
        await add_audit_log(session, "mailing_sent", {"key": mailing_key})
    return sent


async def _get_template_text(session: AsyncSession, key: str) -> str:
    template = await get_template_by_key(session, key)
    if template:
        return template.text
    return DEFAULT_TEMPLATES.get(key, "")


async def send_flow_mailings(
    session: AsyncSession, bot: Bot, flow_id: int, flow_start: datetime
) -> tuple[int, int]:
    tz = ZoneInfo(settings.scheduler_timezone)
    now_utc = datetime.now(timezone.utc)
    enabled = await get_mailings_enabled(session)
    now_local_date = now_utc.astimezone(tz).date()
    flow_start_local_date = flow_start.astimezone(tz).date()
    days_before = (flow_start_local_date - now_local_date).days

    if days_before not in (7, 3):
        return 0, 0

    flow = await flow_repo.get_flow_by_id(session, flow_id)
    already_in_target_flow = await _get_flow_participant_user_ids(session, flow_id)
    active_ids = await _get_active_user_ids(session, now_utc)
    former_ids = await _get_former_user_ids(session)
    active_ids = [uid for uid in active_ids if uid not in already_in_target_flow]
    former_ids = [uid for uid in former_ids if uid not in already_in_target_flow]

    active_key = f"flow:{flow_id}:active:{days_before}"
    former_key = f"flow:{flow_id}:former:{days_before}"

    active_text = await _get_template_text(session, f"mailing_active_{days_before}")
    former_text = await _get_template_text(session, f"mailing_former_{days_before}")

    # Критично: рассылки должны быть идемпотентными и с анти-спам ограничением.
    sent_active = await _send_bulk(session, bot, active_ids, active_text, active_key)
    sent_former = await _send_bulk(session, bot, former_ids, former_text, former_key)
    logger.info(
        "Flow start mailings sent",
        extra={
            "enabled": enabled,
            "tz": settings.scheduler_timezone,
            "now_local_date": str(now_local_date),
            "flow_start_local_date": str(flow_start_local_date),
            "flow_id": flow_id,
            "days_before": days_before,
            "flow_start_at": flow_start.isoformat(),
            "flow_end_at": flow.end_at.isoformat() if flow else None,
            "active_users_count": len(active_ids),
            "former_users_count": len(former_ids),
            "excluded_already_in_target_flow": len(already_in_target_flow),
            "sent_active": sent_active,
            "sent_former": sent_former,
        },
    )
    return sent_active, sent_former


async def send_custom_broadcast(
    session: AsyncSession, bot: Bot, audience: str, text: str
) -> int:
    now = datetime.now(timezone.utc)
    if audience == "active":
        user_ids = await _get_active_user_ids(session, now)
    elif audience == "former":
        user_ids = await _get_former_user_ids(session)
    elif audience == "current_unpaid":
        user_ids = await _get_current_unpaid_transition_user_ids(session, now)
    elif audience == "all":
        active_ids = await _get_active_user_ids(session, now)
        former_ids = await _get_former_user_ids(session)
        user_ids = list({*active_ids, *former_ids})
    else:
        return 0
    return await _send_bulk(
        session, bot, user_ids, text, mailing_key=None, idempotent=False
    )


async def _get_current_unpaid_transition_user_ids(
    session: AsyncSession, now: datetime
) -> list[int]:
    current_flow = await flow_repo.get_active_free_flow(session, now)
    if current_flow is None:
        current_flow = await flow_repo.get_active_paid_flow(session, now)
    if current_flow is None:
        return []

    next_paid_flow = await flow_repo.get_next_paid_flow(session, now)
    if next_paid_flow is None:
        return []

    current_user_ids = await _get_active_flow_user_ids(session, current_flow.id, now)
    if not current_user_ids:
        return []

    paid_result = await session.execute(
        select(distinct(Payment.user_id))
        .where(Payment.flow_id == next_paid_flow.id)
        .where(Payment.status == PaymentStatus.PAID)
        .where(Payment.user_id.in_(current_user_ids))
    )
    paid_user_ids = {row[0] for row in paid_result.all()}

    result = [uid for uid in current_user_ids if uid not in paid_user_ids]
    logger.info(
        "Current unpaid transition audience selected",
        extra={
            "current_flow_id": current_flow.id,
            "next_paid_flow_id": next_paid_flow.id,
            "current_participants_count": len(current_user_ids),
            "already_paid_count": len(paid_user_ids),
            "unpaid_count": len(result),
        },
    )
    return result


async def send_auto_end_mailings(
    session: AsyncSession, bot: Bot, now: datetime
) -> int:
    tz = ZoneInfo(settings.scheduler_timezone)
    now_utc = now.astimezone(timezone.utc)
    enabled = await get_mailings_enabled(session)
    today_local = now_utc.astimezone(tz).date()
    window_start = datetime.combine(
        today_local - timedelta(days=7), time.min, tz
    )
    window_end = datetime.combine(today_local + timedelta(days=1), time.max, tz)
    result = await session.execute(
        select(Flow).where(Flow.end_at >= window_start, Flow.end_at <= window_end)
    )
    flows = list(result.scalars().all())
    total_sent = 0
    sent_flows = 0
    for flow in flows:
        end_date_local = flow.end_at.astimezone(tz).date()
        template_key: str | None = None
        if flow.is_free:
            if today_local == end_date_local - timedelta(days=7):
                template_key = "free_end_minus_7"
            elif today_local == end_date_local - timedelta(days=3):
                template_key = "free_end_minus_3"
        else:
            if today_local == end_date_local - timedelta(days=3):
                template_key = "paid_end_minus_3"
            elif today_local == end_date_local - timedelta(days=1):
                template_key = "paid_end_minus_1"

        if not template_key:
            continue

        key = f"auto:{template_key}:{flow.id}:{today_local}"
        if await has_action_with_key(session, "mailing_sent", key):
            continue

        user_ids = await _get_active_flow_user_ids(session, flow.id, now_utc)
        next_paid_flow = await flow_repo.get_next_paid_flow(session, flow.end_at)
        already_in_next_paid_flow: set[int] = set()
        if next_paid_flow is not None:
            already_in_next_paid_flow = await _get_flow_participant_user_ids(
                session, next_paid_flow.id
            )
            user_ids = [
                uid for uid in user_ids if uid not in already_in_next_paid_flow
            ]
        if not user_ids:
            await add_audit_log(
                session, action="mailing_sent", payload={"key": key, "count": 0}
            )
            continue

        text = await _get_template_text(session, template_key)
        sent = await _send_bulk(
            session,
            bot,
            user_ids,
            text,
            mailing_key=key,
            idempotent=True,
        )
        total_sent += sent
        sent_flows += 1
        logger.info(
            "Auto end mailing sent",
            extra={
                "enabled": enabled,
                "tz": settings.scheduler_timezone,
                "today_local": str(today_local),
                "template_key": template_key,
                "flow_id": flow.id,
                "flow_start_at": flow.start_at.isoformat(),
                "flow_end_at": flow.end_at.isoformat(),
                "end_date_local": str(end_date_local),
                "next_paid_flow_id": next_paid_flow.id if next_paid_flow else None,
                "excluded_already_in_next_paid": len(already_in_next_paid_flow),
                "recipients_count": len(user_ids),
                "sent": sent,
            },
        )
    logger.info(
        "Auto end mailings run",
        extra={
            "enabled": enabled,
            "tz": settings.scheduler_timezone,
            "today_local": str(today_local),
            "window_start_local": str(window_start),
            "window_end_local": str(window_end),
            "flows_considered": len(flows),
            "sent_flows": sent_flows,
            "total_sent": total_sent,
        },
    )
    return total_sent


async def send_pay_later_deadline_reminders(
    session: AsyncSession, bot: Bot, now: datetime
) -> int:
    tz = ZoneInfo(settings.scheduler_timezone)
    now_utc = now.astimezone(timezone.utc)
    today_local = now_utc.astimezone(tz).date()

    result = await session.execute(
        select(Membership)
        .where(Membership.status == MembershipStatus.ACTIVE)
        .where(Membership.pay_later_deadline_at.is_not(None))
    )
    memberships = list(result.scalars().all())

    sent = 0
    for membership in memberships:
        deadline = membership.pay_later_deadline_at
        if deadline is None:
            continue
        deadline_local = deadline.astimezone(tz).date()
        template_key: str | None = None
        if today_local == deadline_local - timedelta(days=1):
            template_key = "pay_later_deadline_minus_1"
        elif today_local == deadline_local:
            template_key = "pay_later_deadline_today"

        if template_key is None:
            continue

        key = f"auto:{template_key}:membership:{membership.id}:{today_local}"
        if await has_action_with_key(session, "mailing_sent", key):
            continue

        user = await session.get(User, membership.user_id)
        if user is None:
            await add_audit_log(session, "mailing_sent", {"key": key, "count": 0})
            continue

        text = await _get_template_text(session, template_key)
        try:
            await bot.send_message(user.tg_id, text)
            sent += 1
            await add_audit_log(session, "mailing_sent", {"key": key, "count": 1})
        except Exception:
            await add_audit_log(session, "mailing_sent", {"key": key, "count": 0})

    logger.info(
        "Pay-later reminders run",
        extra={
            "tz": settings.scheduler_timezone,
            "today_local": str(today_local),
            "memberships_checked": len(memberships),
            "sent": sent,
        },
    )
    return sent
