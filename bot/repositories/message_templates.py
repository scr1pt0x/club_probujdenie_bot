from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import MessageTemplate


async def get_template_by_key(
    session: AsyncSession, key: str
) -> MessageTemplate | None:
    result = await session.execute(
        select(MessageTemplate).where(MessageTemplate.key == key)
    )
    return result.scalar_one_or_none()


async def upsert_template(
    session: AsyncSession, key: str, text: str
) -> MessageTemplate:
    template = await get_template_by_key(session, key)
    if template:
        template.text = text
        return template
    template = MessageTemplate(key=key, text=text)
    session.add(template)
    return template
