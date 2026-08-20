import asyncio
from datetime import datetime, timezone

from bot.services.entitlements import has_valid_access

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, results):
        self.results = iter(results)
        self.executions = 0

    async def execute(self, _query):
        self.executions += 1
        return FakeResult(next(self.results))


def test_current_membership_grants_access_without_payment_lookup():
    session = FakeSession([False, 123])
    assert asyncio.run(has_valid_access(session, 7, NOW))
    assert session.executions == 2


def test_paid_future_flow_protects_access_when_membership_is_missing():
    session = FakeSession([False, None, 456])
    assert asyncio.run(has_valid_access(session, 7, NOW))
    assert session.executions == 3


def test_user_without_membership_or_payment_has_no_access():
    session = FakeSession([False, None, None])
    assert not asyncio.run(has_valid_access(session, 7, NOW))


def test_access_exempt_user_is_protected_without_membership_or_payment_lookup():
    session = FakeSession([True])
    assert asyncio.run(has_valid_access(session, 7, NOW))
    assert session.executions == 1
