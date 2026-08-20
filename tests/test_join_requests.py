import asyncio
from types import SimpleNamespace

from bot.handlers import join_requests


class FakeBot:
    def __init__(self):
        self.approved = []
        self.declined = []

    async def approve_chat_join_request(self, **kwargs):
        self.approved.append(kwargs)

    async def decline_chat_join_request(self, **kwargs):
        self.declined.append(kwargs)


def _request(bot, chat_id=-1001, user_id=42):
    return SimpleNamespace(
        bot=bot,
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id),
    )


def test_unknown_user_join_request_is_declined(monkeypatch):
    async def no_user(*args, **kwargs):
        return None

    bot = FakeBot()
    monkeypatch.setattr(
        join_requests,
        "settings",
        SimpleNamespace(primary_channel_id=-1001, secondary_discussion_id=-1002),
    )
    monkeypatch.setattr(join_requests, "get_user_by_tg_id", no_user)

    asyncio.run(join_requests.approve_join_request(_request(bot), SimpleNamespace()))

    assert not bot.approved
    assert bot.declined == [{"chat_id": -1001, "user_id": 42}]


def test_active_user_join_request_is_approved(monkeypatch):
    async def get_user(*args, **kwargs):
        return SimpleNamespace(id=7)

    async def has_access(*args, **kwargs):
        return True

    bot = FakeBot()
    monkeypatch.setattr(
        join_requests,
        "settings",
        SimpleNamespace(primary_channel_id=-1001, secondary_discussion_id=-1002),
    )
    monkeypatch.setattr(join_requests, "get_user_by_tg_id", get_user)
    monkeypatch.setattr(join_requests, "has_valid_access", has_access)

    asyncio.run(join_requests.approve_join_request(_request(bot), SimpleNamespace()))

    assert bot.approved == [{"chat_id": -1001, "user_id": 42}]
    assert not bot.declined


def test_known_user_without_valid_access_is_declined(monkeypatch):
    async def get_user(*args, **kwargs):
        return SimpleNamespace(id=7)

    async def no_access(*args, **kwargs):
        return False

    bot = FakeBot()
    monkeypatch.setattr(
        join_requests,
        "settings",
        SimpleNamespace(primary_channel_id=-1001, secondary_discussion_id=-1002),
    )
    monkeypatch.setattr(join_requests, "get_user_by_tg_id", get_user)
    monkeypatch.setattr(join_requests, "has_valid_access", no_access)

    asyncio.run(join_requests.approve_join_request(_request(bot), SimpleNamespace()))

    assert bot.declined == [{"chat_id": -1001, "user_id": 42}]
    assert not bot.approved


def test_unmanaged_chat_request_is_ignored(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(
        join_requests,
        "settings",
        SimpleNamespace(primary_channel_id=-1001, secondary_discussion_id=-1002),
    )

    asyncio.run(
        join_requests.approve_join_request(
            _request(bot, chat_id=-9999), SimpleNamespace()
        )
    )

    assert not bot.approved
    assert not bot.declined
