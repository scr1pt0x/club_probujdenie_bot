from aiogram import Router, types
from aiogram.filters import Command
from sqlalchemy.ext.asyncio import AsyncSession

from bot.repositories.users import get_or_create_user
from bot.services.texts import get_text
from bot.ui.keyboards import main_menu_kb
from config import settings


router = Router()


@router.message(Command("start"))
async def start_handler(message: types.Message, session: AsyncSession) -> None:

    is_admin = message.from_user.id in settings.admin_tg_ids
    await get_or_create_user(
        session=session,
        tg_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        is_admin=is_admin,
    )
    await session.commit()

    text = await get_text(session, "start_welcome")
    await message.answer(text, reply_markup=main_menu_kb())
