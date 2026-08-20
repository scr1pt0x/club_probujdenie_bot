import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot.access_control.service import AccessChangeResult
from bot.db.models import MembershipStatus
from bot.scheduler import jobs
from bot.scheduler import setup as scheduler_setup

NOW = datetime.now(timezone.utc)


class FakeScalars:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return FakeScalars(self.rows)


class FakeSession:
    def __init__(self, rows=None):
        self.commits = 0
        self.rows = rows or []

    async def execute(self, _query):
        return FakeResult(self.rows)

    async def commit(self):
        self.commits += 1


def _patch_revoke_dependencies(monkeypatch, *, stale, access, revoke):
    async def list_stale(*args, **kwargs):
        return stale

    async def lock_user(_session, user_id):
        return SimpleNamespace(id=user_id, tg_id=user_id + 1000)

    async def no_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(jobs, "_is_revoke_jobs_enabled", lambda: True)
    monkeypatch.setattr(jobs.membership_repo, "list_memberships_to_expire", list_stale)
    monkeypatch.setattr(jobs, "has_valid_access", access)
    monkeypatch.setattr(jobs.user_repo, "lock_user_by_id", lock_user)
    monkeypatch.setattr(jobs, "revoke_access", revoke)
    monkeypatch.setattr(jobs, "_record_automatic_revoke", no_audit)


def test_expiry_counts_only_users_who_would_actually_lose_access(monkeypatch):
    stale_protected = SimpleNamespace(id=1, user_id=10, status=MembershipStatus.ACTIVE)
    stale_revoke = SimpleNamespace(id=2, user_id=20, status=MembershipStatus.ACTIVE)
    session = FakeSession()
    revoked = []
    safety_counts = []

    async def has_access(_session, user_id, _now, **kwargs):
        return user_id == 10

    async def revoke(_bot, tg_id):
        revoked.append(tg_id)
        return AccessChangeResult(channel_ok=True, group_ok=True)

    _patch_revoke_dependencies(
        monkeypatch,
        stale=[stale_protected, stale_revoke],
        access=has_access,
        revoke=revoke,
    )

    def mass_limit(_job_name, count):
        safety_counts.append(count)
        return False

    monkeypatch.setattr(jobs, "_is_mass_revoke_blocked", mass_limit)
    asyncio.run(jobs.expire_memberships(session, SimpleNamespace()))

    assert safety_counts == [1]
    assert revoked == [1020]
    assert stale_protected.status == MembershipStatus.EXPIRED
    assert stale_revoke.status == MembershipStatus.EXPIRED
    assert session.commits == 2


def test_payment_won_race_is_rechecked_before_telegram_revoke(monkeypatch):
    stale = SimpleNamespace(id=1, user_id=20, status=MembershipStatus.ACTIVE)
    session = FakeSession()
    checks = 0
    revoked = []

    async def access_appears_after_lock(*args, **kwargs):
        nonlocal checks
        checks += 1
        return checks == 2

    async def revoke(_bot, tg_id):
        revoked.append(tg_id)
        return AccessChangeResult(channel_ok=True, group_ok=True)

    _patch_revoke_dependencies(
        monkeypatch, stale=[stale], access=access_appears_after_lock, revoke=revoke
    )
    monkeypatch.setattr(jobs, "_is_mass_revoke_blocked", lambda *args: False)

    asyncio.run(jobs.expire_memberships(session, SimpleNamespace()))

    assert checks == 2
    assert revoked == []
    assert stale.status == MembershipStatus.EXPIRED
    assert session.commits == 1


def test_failed_telegram_revoke_is_retried_instead_of_hidden(monkeypatch):
    stale = SimpleNamespace(id=1, user_id=20, status=MembershipStatus.ACTIVE)
    session = FakeSession()

    async def no_access(*args, **kwargs):
        return False

    async def failed_revoke(*args, **kwargs):
        return AccessChangeResult(channel_ok=True, group_ok=False)

    _patch_revoke_dependencies(
        monkeypatch, stale=[stale], access=no_access, revoke=failed_revoke
    )
    monkeypatch.setattr(jobs, "_is_mass_revoke_blocked", lambda *args: False)

    asyncio.run(jobs.expire_memberships(session, SimpleNamespace()))

    assert stale.status == MembershipStatus.ACTIVE
    assert session.commits == 1


def test_mass_revoke_limit_stops_changes_before_mutation(monkeypatch):
    stale = SimpleNamespace(id=1, user_id=20, status=MembershipStatus.ACTIVE)
    session = FakeSession()

    async def no_access(*args, **kwargs):
        return False

    async def should_not_revoke(*args, **kwargs):
        raise AssertionError("revoke must not run above the safety limit")

    _patch_revoke_dependencies(
        monkeypatch, stale=[stale], access=no_access, revoke=should_not_revoke
    )
    monkeypatch.setattr(jobs, "_is_mass_revoke_blocked", lambda *args: True)

    asyncio.run(jobs.expire_memberships(session, SimpleNamespace()))

    assert stale.status == MembershipStatus.ACTIVE
    assert session.commits == 0


def test_paid_user_is_not_revoked_when_pay_later_row_expires(monkeypatch):
    overdue = SimpleNamespace(id=8, user_id=20, status=MembershipStatus.ACTIVE)
    session = FakeSession(rows=[overdue])
    revoked = []

    async def has_paid_access(*args, **kwargs):
        return True

    async def lock_user(_session, user_id):
        return SimpleNamespace(id=user_id, tg_id=1020)

    async def revoke(_bot, tg_id):
        revoked.append(tg_id)
        return AccessChangeResult(channel_ok=True, group_ok=True)

    async def text(*args, **kwargs):
        return "expired"

    monkeypatch.setattr(jobs, "_is_revoke_jobs_enabled", lambda: True)
    monkeypatch.setattr(jobs, "has_valid_access", has_paid_access)
    monkeypatch.setattr(jobs.user_repo, "lock_user_by_id", lock_user)
    monkeypatch.setattr(jobs, "revoke_access", revoke)
    monkeypatch.setattr(jobs, "get_text", text)
    monkeypatch.setattr(jobs, "_is_mass_revoke_blocked", lambda *args: False)

    asyncio.run(jobs.enforce_pay_later_deadlines(session, SimpleNamespace()))

    assert revoked == []
    assert overdue.status == MembershipStatus.EXPIRED
    assert session.commits == 1


def test_duplicate_flow_start_revoke_job_is_not_registered(monkeypatch):
    monkeypatch.setattr(
        scheduler_setup,
        "settings",
        SimpleNamespace(scheduler_timezone="UTC", revoke_jobs_enabled=True),
    )
    scheduler = scheduler_setup.setup_scheduler(
        SimpleNamespace(), payment_adapter=SimpleNamespace()
    )
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert "expire_memberships" in job_ids
    assert "enforce_pay_later_deadlines" in job_ids
    assert "remove_non_renewed" not in job_ids


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
