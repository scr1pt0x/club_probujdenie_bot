from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from bot.repositories.app_settings import get_setting
from config import settings


@dataclass(frozen=True)
class EffectiveSettings:
    intro_price_rub: int
    renewal_price_rub: int
    grace_days: int
    pay_later_max_days: int


async def get_effective_settings(session: AsyncSession) -> EffectiveSettings:
    intro = await get_setting(session, "intro_price_rub")
    renewal = await get_setting(session, "renewal_price_rub")
    grace = await get_setting(session, "grace_days")
    pay_later = await get_setting(session, "pay_later_max_days")

    return EffectiveSettings(
        intro_price_rub=int(intro) if intro is not None else settings.intro_price_rub,
        renewal_price_rub=int(renewal)
        if renewal is not None
        else settings.renewal_price_rub,
        grace_days=int(grace) if grace is not None else settings.grace_days,
        pay_later_max_days=int(pay_later)
        if pay_later is not None
        else settings.pay_later_max_days,
    )


async def get_mailings_enabled(session: AsyncSession) -> bool:
    override = await get_setting(session, "mailings_enabled_override")
    if override is None:
        return settings.mailings_enabled
    return override.lower() == "true"
