from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.db.session import AsyncSessionLocal
from bot.scheduler import jobs
from config import settings


def setup_scheduler(bot, payment_adapter=None) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)

    async def _with_session(coro):
        async with AsyncSessionLocal() as session:
            await coro(session)

    scheduler.add_job(
        lambda: _with_session(lambda s: jobs.expire_memberships(s, bot)),
        "interval",
        minutes=30,
        id="expire_memberships",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: _with_session(lambda s: jobs.enforce_pay_later_deadlines(s, bot)),
        "interval",
        minutes=30,
        id="enforce_pay_later_deadlines",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: _with_session(lambda s: jobs.send_scheduled_mailings(s, bot)),
        "interval",
        hours=12,
        id="send_mailings",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: jobs.auto_mailings(bot, AsyncSessionLocal),
        "cron",
        hour=10,
        minute=0,
        id="auto_mailings",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: _with_session(lambda s: jobs.remove_non_renewed_on_paid_flows(s, bot)),
        "interval",
        hours=12,
        id="remove_non_renewed",
        replace_existing=True,
    )

    if payment_adapter is not None:
        scheduler.add_job(
            lambda: _with_session(
                lambda s: jobs.check_pending_payments(s, bot, payment_adapter)
            ),
            "interval",
            minutes=10,
            id="check_payments",
            replace_existing=True,
        )

    return scheduler
