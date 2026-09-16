from datetime import datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Membership, MembershipStatus


async def get_active_membership(
    session: AsyncSession, user_id: int
) -> Membership | None:
    result = await session.execute(
        select(Membership)
        .where(Membership.user_id == user_id)
        .where(Membership.status == MembershipStatus.ACTIVE)
        .order_by(Membership.access_end_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def get_membership_by_flow(
    session: AsyncSession, user_id: int, flow_id: int
) -> Membership | None:
    result = await session.execute(
        select(Membership).where(
            Membership.user_id == user_id, Membership.flow_id == flow_id
        )
    )
    return result.scalar_one_or_none()


async def list_memberships_to_expire(
    session: AsyncSession, now: datetime
) -> list[Membership]:
    result = await session.execute(
        select(Membership)
        .where(Membership.status == MembershipStatus.ACTIVE)
        .where(Membership.grace_end_at < now)
        .where(
            or_(
                Membership.pay_later_deadline_at.is_(None),
                Membership.pay_later_deadline_at <= now,
            )
        )
    )
    return list(result.scalars().all())


async def get_latest_membership(
    session: AsyncSession, user_id: int
) -> Membership | None:
    result = await session.execute(
        select(Membership)
        .where(Membership.user_id == user_id)
        .order_by(Membership.created_at.desc(), Membership.id.desc())
        .limit(1)
    )
    return result.scalars().first()


async def recheck_expiring_memberships(
    session: AsyncSession,
    user_id: int,
    membership_ids: set[int],
    now: datetime,
    *,
    pay_later: bool = False,
) -> list[Membership]:
    """Called under the user lock; discard stale decisions and refresh ORM rows."""
    query = select(Membership).where(
        Membership.user_id == user_id,
        Membership.id.in_(membership_ids),
        Membership.status == MembershipStatus.ACTIVE,
    )
    if pay_later:
        query = query.where(Membership.pay_later_deadline_at <= now)
    else:
        query = query.where(
            Membership.grace_end_at < now,
            or_(
                Membership.pay_later_deadline_at.is_(None),
                Membership.pay_later_deadline_at <= now,
            ),
        )
    return list(
        (await session.execute(query.execution_options(populate_existing=True)))
        .scalars()
        .all()
    )


async def expire_all_active_memberships(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(
        update(Membership)
        .where(Membership.user_id == user_id)
        .where(Membership.status == MembershipStatus.ACTIVE)
        .values(status=MembershipStatus.EXPIRED)
    )
    return int(result.rowcount or 0)


async def count_pay_later_used(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Membership)
        .where(Membership.pay_later_used_at.is_not(None))
    )
    return int(result.scalar_one() or 0)


async def count_pay_later_active(session: AsyncSession, now: datetime) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Membership)
        .where(Membership.status == MembershipStatus.ACTIVE)
        .where(Membership.pay_later_deadline_at.is_not(None))
        .where(Membership.pay_later_deadline_at > now)
    )
    return int(result.scalar_one() or 0)


async def count_pay_later_overdue(session: AsyncSession, now: datetime) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Membership)
        .where(Membership.status == MembershipStatus.ACTIVE)
        .where(Membership.pay_later_deadline_at.is_not(None))
        .where(Membership.pay_later_deadline_at <= now)
    )
    return int(result.scalar_one() or 0)
