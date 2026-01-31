from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bot.repositories.promos import get_latest_user_promo, get_promo_by_code


def is_promo_valid(promo, now: datetime) -> bool:
    if not promo.active:
        return False
    if promo.starts_at and now < promo.starts_at:
        return False
    if promo.ends_at and now > promo.ends_at:
        return False
    if promo.max_uses is not None and promo.used_count >= promo.max_uses:
        return False
    return True


async def apply_promo_to_price(
    session: AsyncSession, user_id: int, base_price: int
) -> int:
    user_promo = await get_latest_user_promo(session, user_id)
    if not user_promo:
        return base_price
    promo = await get_promo_by_code(session, user_promo.code)
    if not promo:
        return base_price
    now = datetime.now(timezone.utc)
    if not is_promo_valid(promo, now):
        return base_price

    if promo.kind == "free":
        return 0
    if promo.kind == "percent":
        return max(0, int(base_price * (100 - promo.value_int) / 100))
    if promo.kind == "fixed":
        return max(0, base_price - promo.value_int)
    return base_price
