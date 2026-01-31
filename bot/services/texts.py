from sqlalchemy.ext.asyncio import AsyncSession

from bot.admin.templates import DEFAULT_TEMPLATES
from bot.repositories.message_templates import get_template_by_key


async def get_text(session: AsyncSession, key: str) -> str:
    template = await get_template_by_key(session, key)
    if template:
        return template.text
    text = DEFAULT_TEMPLATES.get(key, "")
    if text:
        return text
    return f"⚠️ Шаблон не найден: {key}"
