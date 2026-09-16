from collections.abc import Collection
from datetime import datetime

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    Flow,
    Membership,
    MembershipStatus,
    Payment,
    PaymentStatus,
    User,
)


def valid_access_predicate(now: datetime, *, exclude_membership_ids=()):
    """Shared policy for admission, expiry protection and mailing audiences."""
    membership = select(Membership.id).where(
        Membership.user_id == User.id,
        Membership.status == MembershipStatus.ACTIVE,
        or_(Membership.grace_end_at >= now, Membership.pay_later_deadline_at > now),
    )
    if exclude_membership_ids:
        membership = membership.where(Membership.id.notin_(exclude_membership_ids))
    paid = (
        select(Payment.id)
        .join(Flow, Payment.flow_id == Flow.id)
        .where(
            Payment.user_id == User.id,
            Payment.status == PaymentStatus.PAID,
            Flow.end_at > now,
        )
    )
    return or_(
        User.access_exempt.is_(True),
        and_(User.access_suspended.is_(False), or_(exists(membership), exists(paid))),
    )


async def has_unresolved_payment(session: AsyncSession, user_id: int) -> bool:
    """Hold automatic removal, but do NOT grant access, until money is reconciled."""
    result = await session.execute(
        select(Payment.id)
        .where(
            Payment.user_id == user_id,
            Payment.status.in_([PaymentStatus.PENDING, PaymentStatus.NEEDS_REVIEW]),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def has_valid_access(
    session: AsyncSession,
    user_id: int,
    now: datetime,
    *,
    exclude_membership_ids: Collection[int] = (),
) -> bool:
    """Return whether revoking Telegram access would be unsafe for this user."""
    result = await session.execute(
        select(User.id).where(
            User.id == user_id,
            valid_access_predicate(now, exclude_membership_ids=exclude_membership_ids),
        )
    )
    return result.scalar_one_or_none() is not None
