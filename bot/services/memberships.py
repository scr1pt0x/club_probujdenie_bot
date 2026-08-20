from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Membership, MembershipStatus, Payment
from bot.repositories import memberships as membership_repo
from bot.services.flows import get_next_paid_flow
from bot.services.settings import get_effective_settings


@dataclass(frozen=True)
class PayLaterEligibility:
    eligible: bool
    message: str
    membership: Membership | None = None
    deadline: datetime | None = None


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


async def apply_pay_later(
    session: AsyncSession, user_id: int, now: datetime
) -> tuple[bool, str]:
    eligibility = await evaluate_pay_later(session, user_id, now)
    if not eligibility.eligible:
        return False, eligibility.message

    membership = eligibility.membership
    deadline = eligibility.deadline
    if membership is None or deadline is None:
        return False, "Опция недоступна: не удалось рассчитать отсрочку."

    effective = await get_effective_settings(session)
    membership.pay_later_used_at = now
    membership.pay_later_deadline_at = deadline
    membership.access_end_at = max(membership.access_end_at, deadline)
    membership.grace_end_at = deadline + timedelta(days=effective.grace_days)

    return True, f"Отсрочка активна до {deadline.strftime('%d.%m.%Y')}."


async def evaluate_pay_later(
    session: AsyncSession, user_id: int, now: datetime
) -> PayLaterEligibility:
    membership = await membership_repo.get_active_membership(session, user_id=user_id)
    if not membership:
        return PayLaterEligibility(False, "Опция недоступна: нет активного участия.")

    if membership.pay_later_deadline_at and membership.pay_later_deadline_at > now:
        deadline_text = membership.pay_later_deadline_at.strftime("%d.%m.%Y")
        return PayLaterEligibility(
            False, f"Отсрочка уже активна до {deadline_text}.", membership
        )

    if membership.grace_end_at < now:
        return PayLaterEligibility(
            False, "Опция недоступна: предыдущее участие уже завершено.", membership
        )

    next_flow = await get_next_paid_flow(session, now)
    if not next_flow:
        return PayLaterEligibility(
            False, "Опция недоступна: нет ближайшего потока.", membership
        )
    if now >= next_flow.start_at:
        return PayLaterEligibility(
            False, "Опция недоступна: поток уже начался.", membership
        )
    if membership.access_end_at >= next_flow.start_at:
        return PayLaterEligibility(False, "Продление пока не требуется.", membership)

    effective = await get_effective_settings(session)
    deadline = next_flow.start_at + timedelta(days=effective.pay_later_max_days)
    return PayLaterEligibility(True, "Отсрочка доступна.", membership, deadline)
