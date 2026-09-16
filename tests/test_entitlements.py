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


def test_shared_policy_positive_result_grants_access():
    session = FakeSession([123])
    assert asyncio.run(has_valid_access(session, 7, NOW))
    assert session.executions == 1


def test_shared_policy_positive_payment_result_grants_access():
    session = FakeSession([456])
    assert asyncio.run(has_valid_access(session, 7, NOW))
    assert session.executions == 1


def test_user_without_membership_or_payment_has_no_access():
    session = FakeSession([None])
    assert not asyncio.run(has_valid_access(session, 7, NOW))


def test_access_exempt_user_is_protected_without_membership_or_payment_lookup():
    session = FakeSession([7])
    assert asyncio.run(has_valid_access(session, 7, NOW))
    assert session.executions == 1
