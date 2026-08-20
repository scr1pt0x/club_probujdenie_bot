from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Payment, PaymentStatus


async def get_payment_by_external_id(
    session: AsyncSession, external_id: str
) -> Payment | None:
    result = await session.execute(
        select(Payment).where(Payment.external_id == external_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def list_pending_payments(session: AsyncSession) -> list[Payment]:
    result = await session.execute(
        select(Payment)
        .where(Payment.status == PaymentStatus.PENDING)
        .where(Payment.external_id.is_not(None))
        .where(Payment.external_id != "")
        .order_by(Payment.created_at.asc())
    )
    return list(result.scalars().all())
