from collections.abc import Collection
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    Flow,
    Membership,
    MembershipStatus,
    Payment,
    PaymentStatus,
    User,
)


async def has_valid_access(
    session: AsyncSession,
    user_id: int,
    now: datetime,
    *,
    exclude_membership_ids: Collection[int] = (),
) -> bool:
    """Return whether revoking Telegram access would be unsafe for this user."""
    access_exempt = await session.execute(
        select(User.access_exempt).where(User.id == user_id)
    )
    if access_exempt.scalar_one_or_none() is True:
        return True

    membership_query = (
        select(Membership.id)
        .where(Membership.user_id == user_id)
        .where(Membership.status == MembershipStatus.ACTIVE)
        .where(Membership.grace_end_at >= now)
        .limit(1)
    )
    if exclude_membership_ids:
        membership_query = membership_query.where(
            Membership.id.notin_(exclude_membership_ids)
        )
    if (await session.execute(membership_query)).scalar_one_or_none() is not None:
        return True

    paid_query = (
        select(Payment.id)
        .join(Flow, Payment.flow_id == Flow.id)
        .where(Payment.user_id == user_id)
        .where(Payment.status == PaymentStatus.PAID)
        .where(Flow.end_at > now)
        .limit(1)
    )
    return (await session.execute(paid_query)).scalar_one_or_none() is not None
