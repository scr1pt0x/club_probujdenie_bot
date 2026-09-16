import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage

from bot.services import delivery

METHOD = SendMessage(chat_id=1, text="QA")


@pytest.mark.parametrize(
    "error,status",
    [
        (TelegramForbiddenError(METHOD, "blocked"), "blocked"),
        (TelegramNetworkError(METHOD, "timeout"), "unknown"),
        (TimeoutError(), "unknown"),
    ],
)
def test_ambiguous_or_blocked_delivery_is_not_retried(error, status):
    call = AsyncMock(side_effect=error)
    assert asyncio.run(delivery.deliver(call)) == status
    assert call.await_count == 1


def test_rate_limit_is_retried_once_after_telegram_delay(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(delivery.asyncio, "sleep", sleep)
    call = AsyncMock(side_effect=[TelegramRetryAfter(METHOD, "retry", 2), True])
    assert asyncio.run(delivery.deliver(call)) == "sent"
    sleep.assert_awaited_once_with(3)
    assert call.await_count == 2
