from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Flow


async def list_flows(session: AsyncSession) -> list[Flow]:
    result = await session.execute(select(Flow).order_by(Flow.start_at))
    return list(result.scalars().all())


async def get_flow_by_id(session: AsyncSession, flow_id: int) -> Flow | None:
    result = await session.execute(select(Flow).where(Flow.id == flow_id))
    return result.scalar_one_or_none()


async def get_active_paid_flow(session: AsyncSession, now: datetime) -> Flow | None:
    result = await session.execute(
        select(Flow)
        .where(Flow.is_free.is_(False))
        .where(Flow.start_at <= now, Flow.end_at >= now)
        .order_by(Flow.start_at.desc())
    )
    return result.scalar_one_or_none()


async def get_active_free_flow(session: AsyncSession, now: datetime) -> Flow | None:
    result = await session.execute(
        select(Flow)
        .where(Flow.is_free.is_(True))
        .where(Flow.start_at <= now, Flow.end_at >= now)
        .order_by(Flow.start_at.desc())
    )
    return result.scalar_one_or_none()


async def get_next_free_flow(session: AsyncSession, now: datetime) -> Flow | None:
    result = await session.execute(
        select(Flow)
        .where(Flow.is_free.is_(True))
        .where(Flow.start_at >= now)
        .order_by(Flow.start_at.asc())
    )
    return result.scalar_one_or_none()


async def get_next_paid_flow(session: AsyncSession, now: datetime) -> Flow | None:
    result = await session.execute(
        select(Flow)
        .where(Flow.is_free.is_(False))
        .where(Flow.start_at >= now)
        .order_by(Flow.start_at.asc())
    )
    return result.scalar_one_or_none()


async def get_flow_in_sales_window(session: AsyncSession, now: datetime) -> Flow | None:
    result = await session.execute(
        select(Flow)
        .where(Flow.sales_open_at <= now, Flow.sales_close_at >= now)
        .order_by(Flow.start_at.asc())
    )
    return result.scalar_one_or_none()


async def get_flow_by_start(
    session: AsyncSession, start_at: datetime, is_free: bool
) -> Flow | None:
    result = await session.execute(
        select(Flow).where(Flow.start_at == start_at, Flow.is_free == is_free)
    )
    return result.scalar_one_or_none()
