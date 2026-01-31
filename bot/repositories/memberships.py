from datetime import datetime

from sqlalchemy import select
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
    )
    return result.scalar_one_or_none()


async def get_membership_by_flow(
    session: AsyncSession, user_id: int, flow_id: int
) -> Membership | None:
    result = await session.execute(
        select(Membership)
        .where(Membership.user_id == user_id, Membership.flow_id == flow_id)
    )
    return result.scalar_one_or_none()


async def list_memberships_to_expire(
    session: AsyncSession, now: datetime
) -> list[Membership]:
    result = await session.execute(
        select(Membership)
        .where(Membership.status == MembershipStatus.ACTIVE)
        .where(Membership.access_end_at < now)
    )
    return list(result.scalars().all())


async def get_latest_membership(
    session: AsyncSession, user_id: int
) -> Membership | None:
    result = await session.execute(
        select(Membership)
        .where(Membership.user_id == user_id)
        .order_by(Membership.created_at.desc(), Membership.id.desc())
    )
    return result.scalar_one_or_none()
