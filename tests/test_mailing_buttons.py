"""All broadcast entry buttons must accept rich messages without auto-sending."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from bot.admin import router as admin
from bot.admin.keyboards import mailings_menu_kb


@pytest.mark.parametrize("audience", ["all", "active", "former", "current_unpaid"])
@pytest.mark.parametrize("entry", ["direct", "free"])
@pytest.mark.parametrize("content", ["text", "rich"])
def test_every_button_preserves_audience_and_requires_confirmation(
    monkeypatch, audience, entry, content
):
    async def scenario():
        bot = Bot("123:TEST")
        storage = MemoryStorage()
        state = FSMContext(storage, StorageKey(bot_id=123, chat_id=42, user_id=42))
        actor = User(id=42, is_bot=False, first_name="QA")

        def message(**kwargs):
            return Message(
                message_id=7,
                date=datetime.now(timezone.utc),
                chat=Chat(id=42, type="private"),
                from_user=actor,
                **kwargs,
            ).as_(bot)

        async def click(data):
            await admin.router.propagate_event(
                "callback_query",
                CallbackQuery(
                    id="qa",
                    from_user=actor,
                    chat_instance="qa",
                    message=message(text="menu"),
                    data=data,
                ).as_(bot),
                bot=bot,
                session=session,
                state=state,
                raw_state=await state.get_state(),
            )

        replies = []

        async def transport(bot, method, timeout=None):
            replies.append(method)
            return message(text="reply")

        monkeypatch.setattr(bot.session, "make_request", transport)
        monkeypatch.setattr(admin, "settings", SimpleNamespace(admin_tg_ids=[42]))
        monkeypatch.setattr(admin, "get_mailings_enabled", AsyncMock(return_value=True))
        monkeypatch.setattr(admin, "edit_screen", AsyncMock())
        recipients = {
            "all": [10, 11, 12],
            "active": [10],
            "former": [11],
            "current_unpaid": [12],
        }[audience]
        select_audience = AsyncMock(return_value=recipients)
        monkeypatch.setattr(admin, "custom_audience_ids", select_audience)
        copied = AsyncMock(return_value=SimpleNamespace(message_id=99))
        monkeypatch.setattr(bot, "copy_message", copied)
        claimed = AsyncMock(return_value=True)
        monkeypatch.setattr(admin, "claim_attempt", claimed)
        send = AsyncMock(
            return_value=dict(
                sent=len(recipients),
                blocked=0,
                failed=0,
                unknown=0,
                skipped=0,
                rate_limited=0,
            )
        )
        monkeypatch.setattr(admin, "send_custom_broadcast", send)
        session = SimpleNamespace(commit=AsyncMock())
        try:
            # A stale preview must not leak its audience, recipient list or key.
            await state.set_state(admin.CustomMailingState.confirming)
            await state.set_data({"audience": "all", "key": "old", "user_ids": [999]})
            keyboard = mailings_menu_kb(True)
            if entry == "free":
                await click("admin:mailings:custom")
                keyboard = admin._mailings_custom_audience_kb()
            button = next(
                button
                for row in keyboard.inline_keyboard
                for button in row
                if button.callback_data == f"admin:mailings:custom:{audience}"
            )
            await click(button.callback_data)
            assert (
                await state.get_state() == admin.CustomMailingState.waiting_text.state
            )
            assert await state.get_data() == {"audience": audience}
            kwargs = {"text": "Набор на 49 поток открыт 🌿"}
            if content == "rich":
                kwargs = {
                    "rich_message": {
                        "blocks": [
                            {
                                "type": "paragraph",
                                "text": {
                                    "type": "bold",
                                    "text": "Набор на 49 поток открыт 🌿",
                                },
                            },
                            {"type": "paragraph", "text": "Для оплаты откройте меню."},
                        ]
                    }
                }
            incoming = message(**kwargs)
            if content == "rich":
                assert incoming.text is None and incoming.caption is None
                assert incoming.content_type == "rich_message"
            await admin.router.propagate_event(
                "message",
                incoming,
                bot=bot,
                session=session,
                state=state,
                raw_state=await state.get_state(),
            )
            assert await state.get_state() == admin.CustomMailingState.confirming.state
            select_audience.assert_awaited_once_with(session, audience)
            copied.assert_awaited_once_with(chat_id=42, from_chat_id=42, message_id=7)
            send.assert_not_awaited()
            claimed.assert_not_awaited()
            data = await state.get_data()
            assert data["audience"] == audience
            assert data["user_ids"] == recipients
            assert data["key"] != "old"
            assert f"Получателей: {len(recipients)}" in replies[-1].text
            await click(f"admin:mailings:send:{data['key']}")
            send.assert_awaited_once_with(
                session,
                bot,
                user_ids=recipients,
                source_chat_id=42,
                source_message_id=99,
                key=data["key"],
            )
            # Repeated presses cannot start the campaign again.
            await click(f"admin:mailings:send:{data['key']}")
            assert send.await_count == 1
            assert await state.get_state() is None
        finally:
            await storage.close()
            await bot.session.close()

    asyncio.run(scenario())
