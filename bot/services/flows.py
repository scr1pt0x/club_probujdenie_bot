from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Flow
from bot.repositories import flows as flow_repo
from config import settings


def parse_utc_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def sales_window_for_start(start_at: datetime) -> tuple[datetime, datetime]:
    # Фиксировано по согласованию с заказчиком: окно продаж = старт -7 / +7 дней (UTC).
    return start_at - timedelta(days=7), start_at + timedelta(days=7)


async def ensure_seed_flows(session: AsyncSession) -> None:
    free_start = parse_utc_date(settings.free_flow_start)
    free_end = parse_utc_date(settings.free_flow_end)
    open_at, close_at = sales_window_for_start(free_start)

    result = await flow_repo.get_flow_by_start(session, free_start, True)
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


    # Фиксировано по согласованию с заказчиком: старт платного потока 2026-03-30 (UTC).
    paid_start = parse_utc_date("2026-03-30")
    paid_end = paid_start + timedelta(weeks=5)
    paid_open, paid_close = sales_window_for_start(paid_start)

    result = await flow_repo.get_flow_by_start(session, paid_start, False)
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
    )
    return result.scalar_one_or_none()
