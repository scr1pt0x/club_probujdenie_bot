import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot.access_control.service import AccessChangeResult
from bot.db.models import MembershipStatus
from bot.scheduler import jobs

NOW = datetime.now(timezone.utc)


class FakeSession:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def test_expiry_counts_only_users_who_would_actually_lose_access(monkeypatch):
    stale_protected = SimpleNamespace(id=1, user_id=10, status=MembershipStatus.ACTIVE)
    stale_revoke = SimpleNamespace(id=2, user_id=20, status=MembershipStatus.ACTIVE)
    session = FakeSession()
    revoked = []
    safety_counts = []

    async def list_stale(*args, **kwargs):
        return [stale_protected, stale_revoke]

    async def has_future_payment(_session, user_id, _now):
        return user_id == 10

    async def has_other_membership(*args, **kwargs):
        return False

    async def get_user(_session, user_id):
        return SimpleNamespace(id=user_id, tg_id=user_id + 1000)

    async def revoke(_bot, tg_id):
        revoked.append(tg_id)
        return AccessChangeResult(channel_ok=True, group_ok=True)

    def mass_limit(_job_name, count):
        safety_counts.append(count)
        return False

    monkeypatch.setattr(jobs, "_is_revoke_jobs_enabled", lambda: True)
    monkeypatch.setattr(jobs.membership_repo, "list_memberships_to_expire", list_stale)
    monkeypatch.setattr(jobs, "_has_any_future_paid_payment", has_future_payment)
    monkeypatch.setattr(jobs, "_has_other_active_membership", has_other_membership)
    monkeypatch.setattr(jobs, "_is_mass_revoke_blocked", mass_limit)
    monkeypatch.setattr(jobs.user_repo, "get_user_by_id", get_user)
    monkeypatch.setattr(jobs, "revoke_access", revoke)

    asyncio.run(jobs.expire_memberships(session, SimpleNamespace()))

    assert safety_counts == [1]
    assert revoked == [1020]
    assert stale_protected.status == MembershipStatus.EXPIRED
    assert stale_revoke.status == MembershipStatus.EXPIRED
    assert session.commits == 1


def test_failed_telegram_revoke_is_retried_instead_of_hidden(monkeypatch):
    stale = SimpleNamespace(id=1, user_id=20, status=MembershipStatus.ACTIVE)
    session = FakeSession()

    async def return_stale(*args, **kwargs):
        return [stale]

    async def no_future(*args, **kwargs):
        return False

    async def no_other(*args, **kwargs):
        return False

    async def get_user(*args, **kwargs):
        return SimpleNamespace(id=20, tg_id=1020)

    async def failed_revoke(*args, **kwargs):
        return AccessChangeResult(channel_ok=True, group_ok=False)

    monkeypatch.setattr(jobs, "_is_revoke_jobs_enabled", lambda: True)
    monkeypatch.setattr(
        jobs.membership_repo, "list_memberships_to_expire", return_stale
    )
    monkeypatch.setattr(jobs, "_has_any_future_paid_payment", no_future)
    monkeypatch.setattr(jobs, "_has_other_active_membership", no_other)
    monkeypatch.setattr(jobs, "_is_mass_revoke_blocked", lambda *args: False)
    monkeypatch.setattr(jobs.user_repo, "get_user_by_id", get_user)
    monkeypatch.setattr(jobs, "revoke_access", failed_revoke)

    asyncio.run(jobs.expire_memberships(session, SimpleNamespace()))

    assert stale.status == MembershipStatus.ACTIVE
    assert session.commits == 1


def test_mass_revoke_limit_stops_changes_before_mutation(monkeypatch):
    stale = SimpleNamespace(id=1, user_id=20, status=MembershipStatus.ACTIVE)
    session = FakeSession()

    async def list_stale(*args, **kwargs):
        return [stale]

    async def no_future_payment(*args, **kwargs):
        return False

    async def no_other_membership(*args, **kwargs):
        return False

    monkeypatch.setattr(jobs, "_is_revoke_jobs_enabled", lambda: True)
    monkeypatch.setattr(jobs.membership_repo, "list_memberships_to_expire", list_stale)
    monkeypatch.setattr(jobs, "_has_any_future_paid_payment", no_future_payment)
    monkeypatch.setattr(jobs, "_has_other_active_membership", no_other_membership)
    monkeypatch.setattr(jobs, "_is_mass_revoke_blocked", lambda *args: True)

    asyncio.run(jobs.expire_memberships(session, SimpleNamespace()))

    assert stale.status == MembershipStatus.ACTIVE
    assert session.commits == 0


def test_legacy_pending_payment_gets_a_bounded_fallback_deadline():
    created_at = NOW - timedelta(days=2)
    payment = SimpleNamespace(created_at=created_at, expires_at=None)
    assert jobs._pending_payment_deadline(payment) == created_at + timedelta(hours=24)


def test_explicit_payment_expiry_takes_precedence():
    expires_at = NOW + timedelta(minutes=30)
    payment = SimpleNamespace(created_at=NOW, expires_at=expires_at)
    assert jobs._pending_payment_deadline(payment) == expires_at


def test_old_expiration_does_not_trigger_stale_user_notification():
    payment = SimpleNamespace(
        created_at=NOW - timedelta(days=10),
        expires_at=NOW - timedelta(days=9),
    )
    assert not jobs._expiration_notice_is_timely(payment, NOW)
