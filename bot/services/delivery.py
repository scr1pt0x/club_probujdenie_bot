"""Durable, at-most-once attempts: an ambiguous Telegram timeout is not retried."""

import asyncio
import hashlib
import logging

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from sqlalchemy import text

from bot.repositories.audit_log import add_audit_log, has_action_with_key

logger = logging.getLogger(__name__)


async def claim_attempt(session, action: str, key: str, **details) -> bool:
    # Transaction-level lock also protects against a second process/job.
    lock_key = int.from_bytes(
        hashlib.sha256((action + key).encode()).digest()[:8], "big", signed=True
    )
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    if await has_action_with_key(session, action, key):
        await session.commit()
        return False
    await add_audit_log(session, action, {"key": key, **details})
    await session.commit()
    return True


async def deliver(call) -> str:
    for attempt in range(2):
        try:
            await call()
            return "sent"
        except TelegramRetryAfter as exc:
            if attempt or exc.retry_after >= 60:
                return "rate_limited"
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramForbiddenError:
            return "blocked"
        except (TelegramNetworkError, TelegramServerError):
            return "unknown"
        except TelegramAPIError as exc:
            logger.warning("Mailing delivery failed: %s", type(exc).__name__)
            return "failed"
        except (TimeoutError, OSError):
            return "unknown"
    return "failed"
