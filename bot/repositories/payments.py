from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Payment, PaymentStatus


async def get_payment_by_external_id(
    session: AsyncSession, external_id: str
) -> Payment | None:
    result = await session.execute(
        select(Payment).where(Payment.external_id == external_id)
    )
    return result.scalar_one_or_none()


async def list_pending_payments(
    session: AsyncSession, now: datetime
) -> list[Payment]:
    result = await session.execute(
        select(Payment)
        .where(Payment.status == PaymentStatus.PENDING)
        .where((Payment.expires_at.is_(None)) | (Payment.expires_at >= now))
    )
    return list(result.scalars().all())
