import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User

from bot.admin import router as admin
from bot.handlers import join_requests, membership, menu, start


def context():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=123, chat_id=42, user_id=42))


def test_non_admin_cannot_resolve_payments_or_resume_broadcasts(monkeypatch):
    async def scenario():
        monkeypatch.setattr(admin, "settings", SimpleNamespace(admin_tg_ids=[42]))
        review = AsyncMock()
        resume = AsyncMock()
        monkeypatch.setattr(admin, "payment_reviews_screen", review)
        monkeypatch.setattr(admin, "resume_custom_mailing", resume)
        for data in ("admin:payments:resolve:1:1", "admin:mailings:resume:test"):
            cb = SimpleNamespace(
                from_user=SimpleNamespace(id=43), data=data, answer=AsyncMock()
            )
            await admin.admin_section(cb, object(), context())
            cb.answer.assert_awaited_once()
        review.assert_not_awaited()
        resume.assert_not_awaited()

    asyncio.run(scenario())


def message(bot, **kwargs):
    return Message(
        message_id=7,
        date=datetime.now(timezone.utc),
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="QA"),
        **kwargs,
    ).as_(bot)


@pytest.mark.parametrize(
    "value", ["/start", "/Start", "/START", "Start", " старт ", "/start referral"]
)
def test_start_is_dispatched_and_clears_abandoned_admin_state(monkeypatch, value):
    async def scenario():
        bot = Bot("123:TEST")
        state = context()
        await state.set_state(admin.CustomMailingState.waiting_text)
        output = AsyncMock()
        monkeypatch.setattr(start, "get_or_create_user", AsyncMock())
        monkeypatch.setattr(start, "get_text", AsyncMock(return_value="Welcome"))
        monkeypatch.setattr(start, "send_clean_screen", output)
        await start.router.propagate_event(
            "message",
            message(bot, text=value),
            bot=bot,
            session=SimpleNamespace(commit=AsyncMock()),
            state=state,
            raw_state=await state.get_state(),
        )
        assert output.await_count == 1
        assert await state.get_state() is None
        await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["text", "photo", "photo_without_caption"])
def test_mailing_text_or_caption_reaches_preview_without_sending(monkeypatch, kind):
    async def scenario():
        bot = Bot("123:TEST")
        copied = AsyncMock(return_value=SimpleNamespace(message_id=99))
        monkeypatch.setattr(bot, "copy_message", copied)
        monkeypatch.setattr(bot, "__call__", AsyncMock())
        # Message.answer returns an aiogram method; intercept its transport.
        calls = []

        async def transport(bot, method, timeout=None):
            calls.append(method)
            return message(bot, text="reply")

        monkeypatch.setattr(bot.session, "make_request", transport)
        state = context()
        await state.set_state(admin.CustomMailingState.waiting_text)
        await state.set_data({"audience": "all"})
        monkeypatch.setattr(admin, "settings", SimpleNamespace(admin_tg_ids=[42]))
        monkeypatch.setattr(admin, "get_mailings_enabled", AsyncMock(return_value=True))
        monkeypatch.setattr(
            admin, "custom_audience_ids", AsyncMock(return_value=[10, 11])
        )
        send = AsyncMock()
        monkeypatch.setattr(admin, "send_custom_broadcast", send)
        kwargs = (
            {"text": "Набор открыт"}
            if kind == "text"
            else {
                "photo": [
                    PhotoSize(
                        file_id="file", file_unique_id="unique", width=10, height=10
                    )
                ],
                "caption": "Набор открыт" if kind == "photo" else None,
            }
        )
        await admin.router.propagate_event(
            "message",
            message(bot, **kwargs),
            bot=bot,
            state=state,
            session=SimpleNamespace(commit=AsyncMock()),
            raw_state=await state.get_state(),
        )
        assert await state.get_state() == admin.CustomMailingState.confirming.state
        assert copied.await_count == 1
        assert send.await_count == 0
        assert (await state.get_data())["source_message_id"] == 99
        assert "Получателей: 2" in calls[-1].text
        await bot.session.close()

    asyncio.run(scenario())


def test_used_confirmation_cannot_repeat_broadcast(monkeypatch):
    async def scenario():
        state = context()
        await state.set_state(admin.CustomMailingState.confirming)
        await state.set_data(
            {
                "key": "once",
                "audience": "all",
                "user_ids": [10],
                "source_chat_id": 42,
                "source_message_id": 99,
            }
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            answer=AsyncMock(),
            message=object(),
            bot=object(),
        )
        monkeypatch.setattr(admin, "get_mailings_enabled", AsyncMock(return_value=True))
        monkeypatch.setattr(admin, "claim_attempt", AsyncMock(return_value=True))
        monkeypatch.setattr(admin, "edit_screen", AsyncMock())
        send = AsyncMock(
            return_value=dict(
                sent=1, blocked=0, failed=0, unknown=0, skipped=0, rate_limited=0
            )
        )
        monkeypatch.setattr(admin, "send_custom_broadcast", send)
        await admin.confirm_custom_mailing(callback, object(), state, "once")
        await admin.confirm_custom_mailing(callback, object(), state, "once")
        assert send.await_count == 1

    asyncio.run(scenario())


def test_whole_dispatcher_admin_text_flow_matches_screenshot(monkeypatch):
    async def scenario():
        bot = Bot("123:TEST")
        dp = Dispatcher(events_isolation=SimpleEventIsolation())
        for router in (
            start.router,
            join_requests.router,
            membership.router,
            menu.router,
            admin.router,
        ):
            dp.include_router(router)
        replies = []

        async def transport(bot, method, timeout=None):
            replies.append(method)
            return message(bot, text="reply")

        monkeypatch.setattr(bot.session, "make_request", transport)
        monkeypatch.setattr(admin, "settings", SimpleNamespace(admin_tg_ids=[42]))
        monkeypatch.setattr(admin, "get_mailings_enabled", AsyncMock(return_value=True))
        monkeypatch.setattr(
            admin, "custom_audience_ids", AsyncMock(return_value=[10, 11])
        )
        monkeypatch.setattr(admin, "edit_screen", AsyncMock())
        copied = AsyncMock(return_value=SimpleNamespace(message_id=99))
        monkeypatch.setattr(bot, "copy_message", copied)
        session = SimpleNamespace(commit=AsyncMock())
        callback = CallbackQuery(
            id="qa",
            from_user=User(id=42, is_bot=False, first_name="QA"),
            chat_instance="qa",
            message=message(bot, text="menu"),
            data="admin:mailings:custom:all",
        )
        await dp.feed_update(
            bot, Update(update_id=1, callback_query=callback), session=session
        )
        state = dp.fsm.get_context(bot=bot, chat_id=42, user_id=42)
        assert await state.get_state() == admin.CustomMailingState.waiting_text.state
        await dp.feed_update(
            bot,
            Update(
                update_id=2,
                message=message(
                    bot,
                    text=(
                        "Набор на новый 49 поток уже открыт.\nВас ждет:\n"
                        "1. Еще больше прямых эфиров от кураторов\n"
                        "Для оплаты/продления откройте меню, нажмите на 'Оплата' 🌿"
                    ),
                ),
            ),
            session=session,
        )
        assert await state.get_state() == admin.CustomMailingState.confirming.state
        assert copied.await_count == 1
        assert "Получателей: 2" in replies[-1].text
        await bot.session.close()
        await dp.fsm.close()

    asyncio.run(scenario())
