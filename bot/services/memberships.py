from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Membership, MembershipStatus, Payment
from bot.repositories import memberships as membership_repo
from bot.services.flows import get_next_paid_flow
from bot.services.settings import get_effective_settings
from config import settings


def compute_grace_end(access_end: datetime, grace_days: int) -> datetime:
    return access_end + timedelta(days=grace_days)


def is_within_grace(
    active_membership: Membership, paid_at: datetime, grace_days: int
) -> bool:
    return paid_at <= compute_grace_end(active_membership.access_end_at, grace_days)


async def upsert_membership_for_flow(
    session: AsyncSession,
    user_id: int,
    flow_id: int,
    access_start_at: datetime,
    access_end_at: datetime,
    payment: Payment,
) -> Membership:
    effective = await get_effective_settings(session)
    membership = await membership_repo.get_membership_by_flow(session, user_id, flow_id)
    if membership is None:
        membership = Membership(
            user_id=user_id,
            flow_id=flow_id,
            status=MembershipStatus.ACTIVE,
            access_start_at=access_start_at,
            access_end_at=access_end_at,
            grace_end_at=compute_grace_end(access_end_at, effective.grace_days),
            last_payment_id=payment.id,
        )
        session.add(membership)
        return membership

    membership.status = MembershipStatus.ACTIVE
    membership.access_start_at = access_start_at
    membership.access_end_at = access_end_at
    membership.grace_end_at = compute_grace_end(access_end_at, effective.grace_days)
    membership.last_payment_id = payment.id
    return membership


def mark_membership_expired(membership: Membership) -> None:
    membership.status = MembershipStatus.EXPIRED


async def apply_pay_later(
    session: AsyncSession, user_id: int, now: datetime
) -> tuple[bool, str]:
    membership = await membership_repo.get_latest_membership(session, user_id=user_id)
    if not membership:
        return False, "Опция недоступна: нет участия."

    next_flow = await get_next_paid_flow(session, now)
    if not next_flow:
        return False, "Опция недоступна: нет ближайшего потока."
    if now >= next_flow.start_at:
        return False, "Опция недоступна: поток уже начался."
    if membership.access_end_at >= next_flow.start_at:
        return False, "Продление не требуется."

    effective = await get_effective_settings(session)
    deadline = next_flow.start_at + timedelta(days=effective.pay_later_max_days)

    # Критично: "оплатить позже" сохраняет доступ до дедлайна start + N дней (UTC).
    membership.pay_later_used_at = now
    membership.pay_later_deadline_at = deadline
    membership.access_end_at = max(membership.access_end_at, deadline)
    membership.grace_end_at = deadline + timedelta(days=effective.grace_days)

    return True, f"Отсрочка активна до {deadline.date()}."
