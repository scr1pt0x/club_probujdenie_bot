import asyncio
import logging

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher

from bot.admin.router import router as admin_router
from bot.handlers.membership import router as membership_router
from bot.handlers.menu import router as menu_router
from bot.handlers.start import router as start_router
from bot.db.session import AsyncSessionLocal
from bot.payments.dummy_adapter import DummyPaymentAdapter
from bot.scheduler.setup import setup_scheduler
from bot.services.flows import ensure_seed_flows
from bot.utils.db_middleware import DbSessionMiddleware
from config import settings


async def on_startup() -> None:
    async with AsyncSessionLocal() as session:
        await ensure_seed_flows(session)
        await session.commit()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp.message.middleware(DbSessionMiddleware())
    dp.callback_query.middleware(DbSessionMiddleware())

    dp.include_router(start_router)
    dp.include_router(membership_router)
    dp.include_router(menu_router)
    dp.include_router(admin_router)

    await on_startup()

    payment_adapter = DummyPaymentAdapter()
    scheduler = setup_scheduler(bot, payment_adapter=payment_adapter)
    scheduler.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
