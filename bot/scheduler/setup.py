from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.db.session import AsyncSessionLocal
from bot.scheduler import jobs
from config import settings


def setup_scheduler(bot, payment_adapter=None) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)

    async def _with_session(coro):
        async with AsyncSessionLocal() as session:
            await coro(session)

    async def _expire_memberships_job():
        await _with_session(lambda s: jobs.expire_memberships(s, bot))

    async def _enforce_pay_later_deadlines_job():
        await _with_session(lambda s: jobs.enforce_pay_later_deadlines(s, bot))

    async def _send_scheduled_mailings_job():
        await _with_session(lambda s: jobs.send_scheduled_mailings(s, bot))

    async def _auto_mailings_job():
        await jobs.auto_mailings(bot, AsyncSessionLocal)

    async def _remove_non_renewed_job():
        await _with_session(lambda s: jobs.remove_non_renewed_on_paid_flows(s, bot))

    async def _check_payments_job():
        await _with_session(
            lambda s: jobs.check_pending_payments(s, bot, payment_adapter)
        )

    scheduler.add_job(
        _expire_memberships_job,
        "interval",
        minutes=30,
        id="expire_memberships",
        replace_existing=True,
    )
    scheduler.add_job(
        _enforce_pay_later_deadlines_job,
        "interval",
        minutes=30,
        id="enforce_pay_later_deadlines",
        replace_existing=True,
    )
    scheduler.add_job(
        _send_scheduled_mailings_job,
        "interval",
        hours=12,
        id="send_mailings",
        replace_existing=True,
    )
    scheduler.add_job(
        _auto_mailings_job,
        "cron",
        hour=10,
        minute=0,
        id="auto_mailings",
        replace_existing=True,
    )
    scheduler.add_job(
        _remove_non_renewed_job,
        "interval",
        hours=12,
        id="remove_non_renewed",
        replace_existing=True,
    )

    if payment_adapter is not None:
        scheduler.add_job(
            _check_payments_job,
            "interval",
            minutes=10,
            id="check_payments",
            replace_existing=True,
        )

    return scheduler
