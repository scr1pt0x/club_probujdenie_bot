from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Flow, Membership, MembershipStatus
from bot.repositories.users import lock_user_by_id
from bot.services.settings import get_effective_settings
from config import settings


def parse_utc_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def sales_window_for_start(start_at: datetime) -> tuple[datetime, datetime]:
    # Фиксировано по согласованию с заказчиком: окно продаж = старт -7 / +7 дней (UTC).
    return start_at - timedelta(days=7), start_at + timedelta(days=7)


async def ensure_seed_flows(session: AsyncSession) -> None:
    if settings.free_flows_enabled:
        free_start = parse_utc_date(settings.free_flow_start)
        free_end = parse_utc_date(settings.free_flow_end)
        open_at, close_at = sales_window_for_start(free_start)

        result = (
            await session.execute(select(Flow).where(Flow.is_free.is_(True)).limit(1))
        ).scalar_one_or_none()
        if result is None:
            session.add(
                Flow(
                    title="Бесплатный поток",
                    start_at=free_start,
                    end_at=free_end,
                    duration_weeks=4,
                    is_free=True,
                    sales_open_at=open_at,
                    sales_close_at=close_at,
                )
            )

    paid_start = parse_utc_date(settings.paid_flow_start)
    paid_end = paid_start + timedelta(weeks=5)
    paid_open, paid_close = sales_window_for_start(paid_start)

    # Initial seeds are not recreated after an administrator edits their dates.
    result = (
        await session.execute(select(Flow).where(Flow.is_free.is_(False)).limit(1))
    ).scalar_one_or_none()
    if result is None:
        session.add(
            Flow(
                title="Платный поток",
                start_at=paid_start,
                end_at=paid_end,
                duration_weeks=5,
                is_free=False,
                sales_open_at=paid_open,
                sales_close_at=paid_close,
            )
        )


async def get_next_paid_flow(session: AsyncSession, now: datetime) -> Flow | None:
    result = await session.execute(
        select(Flow)
        .where(Flow.is_free.is_(False))
        .where(Flow.start_at >= now)
        .order_by(Flow.start_at.asc())
        .limit(1)
    )
    return result.scalars().first()


async def extend_memberships_for_flow(
    session: AsyncSession, flow_id: int, end_at: datetime
) -> None:
    """Extending a flow must extend existing access; never shorten purchased access."""
    user_ids = list(
        (
            await session.scalars(
                select(Membership.user_id)
                .where(
                    Membership.flow_id == flow_id,
                    Membership.status == MembershipStatus.ACTIVE,
                )
                .order_by(Membership.user_id)
            )
        ).all()
    )
    effective = await get_effective_settings(session)
    for user_id in user_ids:
        await lock_user_by_id(session, user_id)
        member = (
            await session.execute(
                select(Membership)
                .where(
                    Membership.flow_id == flow_id,
                    Membership.user_id == user_id,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if member.status != MembershipStatus.ACTIVE:
            continue
        if member.access_end_at < end_at:
            member.access_end_at = end_at
            member.grace_end_at = max(
                member.grace_end_at, end_at + timedelta(days=effective.grace_days)
            )
            if member.pay_later_deadline_at:
                member.pay_later_deadline_at = max(member.pay_later_deadline_at, end_at)
