from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import AppSetting


async def get_setting(session: AsyncSession, key: str) -> str | None:
    result = await session.execute(select(AppSetting).where(AppSetting.key == key))
    entry = result.scalar_one_or_none()
    return entry.value if entry else None


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    result = await session.execute(select(AppSetting).where(AppSetting.key == key))
    entry = result.scalar_one_or_none()
    if entry:
        entry.value = value
        return
    session.add(AppSetting(key=key, value=value))
