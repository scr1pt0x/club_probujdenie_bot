from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import PromoCode, UserPromo


async def get_promo_by_code(session: AsyncSession, code: str) -> PromoCode | None:
    result = await session.execute(
        select(PromoCode).where(PromoCode.code == code.upper())
    )
    return result.scalar_one_or_none()


async def create_promo_code(
    session: AsyncSession,
    code: str,
    kind: str,
    value_int: int,
    max_uses: int | None,
    starts_at: datetime | None,
    ends_at: datetime | None,
) -> PromoCode:
    promo = PromoCode(
        code=code.upper(),
        kind=kind,
        value_int=value_int,
        max_uses=max_uses,
        starts_at=starts_at,
        ends_at=ends_at,
        active=True,
    )
    session.add(promo)
    return promo


async def list_recent_promos(session: AsyncSession, limit: int = 10) -> list[PromoCode]:
    result = await session.execute(
        select(PromoCode).order_by(PromoCode.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def disable_promo(session: AsyncSession, code: str) -> bool:
    promo = await get_promo_by_code(session, code)
    if not promo:
        return False
    promo.active = False
    return True


async def add_user_promo(
    session: AsyncSession, user_id: int, code: str
) -> None:
    existing = await get_user_promo(session, user_id, code)
    if existing:
        existing.applied_at = datetime.now(timezone.utc)
        return
    session.add(UserPromo(user_id=user_id, code=code.upper()))


async def get_latest_user_promo(session: AsyncSession, user_id: int) -> UserPromo | None:
    result = await session.execute(
        select(UserPromo)
        .where(UserPromo.user_id == user_id)
        .order_by(UserPromo.applied_at.desc())
    )
    return result.scalar_one_or_none()


async def get_user_promo(
    session: AsyncSession, user_id: int, code: str
) -> UserPromo | None:
    result = await session.execute(
        select(UserPromo)
        .where(UserPromo.user_id == user_id, UserPromo.code == code.upper())
    )
    return result.scalar_one_or_none()


async def delete_user_promos(session: AsyncSession, user_id: int) -> None:
    await session.execute(delete(UserPromo).where(UserPromo.user_id == user_id))
