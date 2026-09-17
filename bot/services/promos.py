from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import FreePromoUse
from bot.repositories.flows import get_paid_flow_in_sales_window
from bot.repositories.promos import get_latest_user_promo, get_promo_by_code


def is_promo_valid(promo, now: datetime, *, check_capacity: bool = True) -> bool:
    if not promo.active:
        return False
    if promo.starts_at and now < promo.starts_at:
        return False
    if promo.ends_at and now > promo.ends_at:
        return False
    if (
        check_capacity
        and promo.max_uses is not None
        and promo.used_count >= promo.max_uses
    ):
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
    # The slot was already reserved when this user's code was accepted.
    if not is_promo_valid(promo, now, check_capacity=False):
        return base_price

    if promo.kind == "free":
        flow = await get_paid_flow_in_sales_window(session, now)
        use = await session.get(FreePromoUse, (user_id, promo.code))
        if flow is None or (use is not None and use.flow_id != flow.id):
            return base_price
        return 0
    if promo.kind == "percent":
        return max(0, int(base_price * (100 - promo.value_int) / 100))
    if promo.kind == "fixed":
        return max(0, base_price - promo.value_int)
    return base_price


async def record_free_promo_use(session, payment) -> bool:
    """Under the user lock, bind a zero-price order; caller rolls back on False."""
    selected = await get_latest_user_promo(session, payment.user_id)
    promo = await get_promo_by_code(session, selected.code) if selected else None
    if promo is None or promo.kind != "free":
        return True  # Percentage/fixed discounts retain their existing policy.
    now = datetime.now(timezone.utc)
    if not is_promo_valid(promo, now, check_capacity=False):
        return False
    await session.execute(
        insert(FreePromoUse)
        .values(
            user_id=payment.user_id,
            code=promo.code,
            flow_id=payment.flow_id,
            payment_id=payment.id,
            used_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[FreePromoUse.user_id, FreePromoUse.code]
        )
    )
    use = await session.get(
        FreePromoUse, (payment.user_id, promo.code), populate_existing=True
    )
    return use.flow_id == payment.flow_id
