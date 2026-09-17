"""Regression scenarios from the deep audit; disposable PostgreSQL only."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery
from test_postgres_safety import URL, database_scenario

from bot.access_control.service import AccessChangeResult
from bot.admin import router as admin
from bot.db.models import Flow, Membership, Payment, PaymentReceipt, User
from bot.repositories.audit_log import has_action_with_key
from bot.repositories.memberships import expire_all_active_memberships
from bot.services import mailings, payments
from bot.services.entitlements import has_valid_access
from bot.services.memberships import is_within_grace

pytestmark = pytest.mark.skipif(not URL, reason="Disposable TEST_DATABASE_URL required")


@pytest.mark.parametrize("job", ["expire_memberships", "enforce_pay_later_deadlines"])
def test_payment_must_preserve_previously_granted_manual_extension(monkeypatch, job):
    from bot.scheduler import jobs

    async def scenario(sessions, ids, now):
        async with sessions() as session:
            membership = await session.get(Membership, ids.member)
            flow = await session.get(Flow, membership.flow_id)
            flow.end_at = now + timedelta(days=3)
            promised_end = now + timedelta(days=10)
            membership.access_end_at = promised_end
            membership.grace_end_at = promised_end + timedelta(days=1)
            membership.pay_later_deadline_at = None
            await session.commit()
            monkeypatch.setattr(
                payments,
                "grant_access",
                AsyncMock(return_value=AccessChangeResult(True, True)),
            )
            payment = await session.get(Payment, ids.payment)
            await payments.confirm_payment(
                session, object(), payment, paid_at=now, notify_user=False
            )
            await session.commit()
            assert membership.access_end_at >= promised_end, (
                "Payment shortened an already granted term"
            )
            assert membership.grace_end_at >= promised_end + timedelta(days=1)
            monkeypatch.setattr(jobs, "_is_revoke_jobs_enabled", lambda: True)
            monkeypatch.setattr(
                jobs,
                "datetime",
                SimpleNamespace(
                    now=lambda tz: now + timedelta(days=6),
                ),
            )
            ban = AsyncMock(side_effect=AssertionError("Promised access must survive"))
            monkeypatch.setattr(jobs, "revoke_access", ban)
            await getattr(jobs, job)(session, object())
            ban.assert_not_awaited()
            assert membership.status == "active"

    asyncio.run(database_scenario(scenario))


def test_price_uses_stored_grace_after_settings_change():
    from bot.repositories.app_settings import set_setting

    async def scenario(sessions, ids, now):
        async with sessions() as session:
            member = await session.get(Membership, ids.member)
            member.access_end_at = now - timedelta(days=1)
            member.grace_end_at = now + timedelta(days=2)
            await set_setting(session, "grace_days", "0")
            await set_setting(session, "intro_price_rub", "2990")
            await set_setting(session, "renewal_price_rub", "1990")
            await session.commit()
            assert await payments.calculate_price_rub(session, ids.user, now) == 1990

    asyncio.run(database_scenario(scenario))


def test_payment_preserves_longer_deferral_and_grace(monkeypatch):
    async def scenario(sessions, ids, now):
        async with sessions() as session:
            member = await session.get(Membership, ids.member)
            (await session.get(Flow, member.flow_id)).end_at = now + timedelta(days=3)
            member.pay_later_deadline_at = now + timedelta(days=7)
            member.grace_end_at = now + timedelta(days=12)
            await session.commit()
            monkeypatch.setattr(
                payments,
                "grant_access",
                AsyncMock(return_value=AccessChangeResult(True, True)),
            )
            await payments.confirm_payment(
                session,
                object(),
                await session.get(Payment, ids.payment),
                paid_at=now,
                notify_user=False,
            )
            await session.commit()
            assert member.access_end_at >= now + timedelta(days=7)
            assert member.grace_end_at >= now + timedelta(days=12)

    asyncio.run(database_scenario(scenario))


@pytest.mark.parametrize("route", ["checkout", "links", "refresh", "join"])
def test_suspension_blocks_all_self_service_entry_points(monkeypatch, route):
    from bot.handlers import join_requests, membership, menu

    async def scenario(sessions, ids, now):
        async with sessions() as session:
            user = await session.get(User, ids.user)
            user.access_suspended = True
            member = await session.get(Membership, ids.member)
            (await session.get(Flow, member.flow_id)).end_at = now + timedelta(days=20)
            (await session.get(Payment, ids.payment)).status = "paid"
            await session.commit()
            grant = AsyncMock(side_effect=AssertionError("Suspension bypass"))
            monkeypatch.setattr(menu, "grant_access", grant)
            monkeypatch.setattr(membership, "grant_access", grant)
            monkeypatch.setattr(
                menu,
                "ScreenResponder",
                lambda *a, **kw: SimpleNamespace(answer=AsyncMock(), bot=object()),
            )
            tg_user = SimpleNamespace(
                id=900000001, username="qa", first_name="QA", last_name=None
            )
            cb = SimpleNamespace(
                from_user=tg_user, bot=object(), message=object(), answer=AsyncMock()
            )
            if route == "checkout":
                await menu._send_personal_payment_link(
                    session, tg_user, SimpleNamespace(answer=AsyncMock(), bot=object())
                )
            elif route == "links":
                await membership.access_links_handler(cb, session)
            elif route == "refresh":
                await menu.payment_refresh_handler(cb, session)
            else:
                bot = SimpleNamespace(
                    approve_chat_join_request=AsyncMock(),
                    decline_chat_join_request=AsyncMock(),
                )
                req = SimpleNamespace(
                    from_user=tg_user,
                    bot=bot,
                    chat=SimpleNamespace(id=join_requests.settings.primary_channel_id),
                )
                await join_requests.approve_join_request(req, session)
                bot.approve_chat_join_request.assert_not_awaited()
                bot.decline_chat_join_request.assert_awaited_once()
            grant.assert_not_awaited()

    asyncio.run(database_scenario(scenario))


def test_explicit_admin_revocation_must_not_be_undone_by_paid_fallback():
    async def scenario(sessions, ids, now):
        async with sessions() as session:
            membership = await session.get(Membership, ids.member)
            (await session.get(Flow, membership.flow_id)).end_at = now + timedelta(
                days=20
            )
            (await session.get(Payment, ids.payment)).status = "paid"
            await session.commit()
            # Explicit administrative restriction, not an ordinary expiry.
            (await session.get(User, ids.user)).access_suspended = True
            await expire_all_active_memberships(session, ids.user)
            await session.commit()
            assert not await has_valid_access(session, ids.user, now), (
                "Explicitly revoked user can approve a join request again"
            )

    asyncio.run(database_scenario(scenario))


def test_expired_callback_must_not_consume_unsent_campaign(monkeypatch):
    async def scenario(sessions, ids, now):
        state = FSMContext(
            MemoryStorage(), StorageKey(bot_id=123, chat_id=42, user_id=42)
        )
        await state.set_state(admin.CustomMailingState.confirming)
        await state.set_data(
            dict(
                key="audit-campaign",
                audience="all",
                user_ids=[ids.user],
                source_chat_id=42,
                source_message_id=1,
            )
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            bot=object(),
            message=object(),
            answer=AsyncMock(
                side_effect=TelegramBadRequest(
                    AnswerCallbackQuery(callback_query_id="old"), "query is too old"
                )
            ),
        )
        monkeypatch.setattr(admin, "get_mailings_enabled", AsyncMock(return_value=True))
        monkeypatch.setattr(admin, "edit_screen", AsyncMock())
        send = AsyncMock(
            return_value=dict(
                sent=1, blocked=0, failed=0, rate_limited=0, unknown=0, skipped=0
            )
        )
        monkeypatch.setattr(admin, "send_custom_broadcast", send)
        async with sessions() as session:
            try:
                await admin.confirm_custom_mailing(
                    callback, session, state, "audit-campaign"
                )
            except TelegramBadRequest:
                pass
            consumed = await has_action_with_key(
                session, "custom_mailing_started", "audit-campaign"
            )
            assert send.await_count == 1 or not consumed, (
                "Campaign consumed, state erased, zero deliveries"
            )

    asyncio.run(database_scenario(scenario))


def test_paid_entitlement_must_not_be_classified_as_former():
    async def scenario(sessions, ids, now):
        async with sessions() as session:
            membership = await session.get(Membership, ids.member)
            membership.status = "expired"
            (await session.get(Flow, membership.flow_id)).end_at = now + timedelta(
                days=20
            )
            (await session.get(Payment, ids.payment)).status = "paid"
            await session.commit()
            assert await has_valid_access(session, ids.user, now)
            assert ids.user in await mailings.custom_audience_ids(session, "active"), (
                "Paid entitled user excluded from active audience"
            )

    asyncio.run(database_scenario(scenario))


def test_stored_grace_promise_survives_admin_default_change():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    membership = SimpleNamespace(
        access_end_at=now - timedelta(days=1), grace_end_at=now + timedelta(days=2)
    )
    assert is_within_grace(membership, now), (
        "Stored grace still valid but renewal price is lost"
    )


def test_schedule_must_show_open_enrollment_for_upcoming_flow(monkeypatch):
    from dataclasses import replace

    from bot.handlers import menu

    async def scenario(sessions, ids, now):
        async with sessions() as session:
            membership = await session.get(Membership, ids.member)
            current = await session.get(Flow, membership.flow_id)
            current.end_at = now + timedelta(days=3)
            session.add(
                Flow(
                    title="QA upcoming",
                    start_at=now + timedelta(days=5),
                    end_at=now + timedelta(days=40),
                    duration_weeks=5,
                    is_free=False,
                    sales_open_at=now - timedelta(days=2),
                    sales_close_at=now + timedelta(days=12),
                )
            )
            await session.commit()
            monkeypatch.setattr(
                menu, "settings", replace(menu.settings, free_flows_enabled=False)
            )
            monkeypatch.setattr(
                menu, "get_text", AsyncMock(return_value="{sales_status}")
            )
            assert "Набор открыт" in await menu._schedule_content(session), (
                "Schedule hides open enrollment behind the current closed flow"
            )

    asyncio.run(database_scenario(scenario))


@pytest.mark.parametrize("failure", ["answer", "edit"])
def test_mailing_ui_failure_does_not_abort_delivery(monkeypatch, failure):
    async def scenario(sessions, ids, now):
        state = FSMContext(
            MemoryStorage(), StorageKey(bot_id=123, chat_id=42, user_id=42)
        )
        await state.set_state(admin.CustomMailingState.confirming)
        await state.set_data(
            dict(
                key="ui",
                audience="all",
                user_ids=[ids.user],
                source_chat_id=42,
                source_message_id=1,
            )
        )
        error = TelegramBadRequest(AnswerCallbackQuery(callback_query_id="old"), "old")
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            bot=object(),
            message=object(),
            answer=AsyncMock(),
        )
        edit = AsyncMock()
        if failure == "answer":
            callback.answer.side_effect = error
        else:
            edit.side_effect = error
        monkeypatch.setattr(admin, "edit_screen", edit)
        monkeypatch.setattr(admin, "get_mailings_enabled", AsyncMock(return_value=True))
        send = AsyncMock(
            return_value=dict(
                sent=1, blocked=0, failed=0, unknown=0, skipped=0, rate_limited=0
            )
        )
        monkeypatch.setattr(admin, "send_custom_broadcast", send)
        async with sessions() as session:
            await admin.confirm_custom_mailing(callback, session, state, "ui")
        send.assert_awaited_once()

    asyncio.run(database_scenario(scenario))


def test_campaign_survives_interruption_and_resume_without_repeat(monkeypatch):
    async def scenario(sessions, ids, now):
        state = FSMContext(
            MemoryStorage(), StorageKey(bot_id=123, chat_id=42, user_id=42)
        )
        async with sessions() as session:
            second = User(tg_id=900000002)
            session.add(second)
            await session.commit()
            await state.set_state(admin.CustomMailingState.confirming)
            await state.set_data(
                dict(
                    key="resume",
                    audience="all",
                    user_ids=[ids.user, second.id],
                    source_chat_id=42,
                    source_message_id=1,
                )
            )
            bot = SimpleNamespace(
                copy_message=AsyncMock(side_effect=[RuntimeError("crash"), None])
            )
            cb = SimpleNamespace(
                bot=bot,
                message=object(),
                answer=AsyncMock(),
                from_user=SimpleNamespace(id=42),
            )
            monkeypatch.setattr(admin, "edit_screen", AsyncMock())
            monkeypatch.setattr(
                admin, "get_mailings_enabled", AsyncMock(return_value=True)
            )
            monkeypatch.setattr(
                mailings, "get_mailings_enabled", AsyncMock(return_value=True)
            )
            await admin.confirm_custom_mailing(cb, session, state, "resume")
            assert await state.get_state() is None
        # A new session, without FSM state, models restart / a different admin.
        async with sessions() as session:
            await admin.resume_custom_mailing(cb, session, "resume")
            await admin.resume_custom_mailing(cb, session, "resume")
        assert [c.kwargs["chat_id"] for c in bot.copy_message.await_args_list] == [
            900000001,
            900000002,
        ]

    asyncio.run(database_scenario(scenario))


@pytest.mark.parametrize("exempt", [False, True])
def test_real_admin_revoke_and_restore_keep_payment_history(monkeypatch, exempt):
    async def scenario(sessions, ids, now):
        monkeypatch.setattr(
            admin,
            "settings",
            SimpleNamespace(**{**vars(admin.settings), "admin_tg_ids": [42]}),
        )
        ban = AsyncMock(return_value=AccessChangeResult(True, True))
        grant = AsyncMock(
            return_value=AccessChangeResult(
                True, True, "https://t.me/+QA1", "https://t.me/+QA2"
            )
        )
        monkeypatch.setattr(admin, "revoke_access", ban)
        monkeypatch.setattr(admin, "grant_access", grant)
        state = FSMContext(
            MemoryStorage(), StorageKey(bot_id=123, chat_id=42, user_id=42)
        )
        cb = SimpleNamespace(
            data=f"admin:users:revoke_confirm:{ids.user}",
            answer=AsyncMock(),
            from_user=SimpleNamespace(
                id=42, username="qa", first_name="QA", last_name=None
            ),
            message=SimpleNamespace(
                answer=AsyncMock(), bot=SimpleNamespace(send_message=AsyncMock())
            ),
        )
        async with sessions() as session:
            user = await session.get(User, ids.user)
            user.access_exempt = exempt
            member = await session.get(Membership, ids.member)
            (await session.get(Flow, member.flow_id)).end_at = now + timedelta(days=20)
            (await session.get(Payment, ids.payment)).status = "paid"
            await session.commit()
            await admin.admin_section(cb, session, state)
            assert ban.await_count == int(not exempt), cb.message.answer.await_args_list
            assert await has_valid_access(session, ids.user, now) == exempt
            await session.refresh(user)
            assert user.access_suspended == (not exempt)
            assert ban.await_count == int(not exempt)
            assert (await session.get(Payment, ids.payment)).status == "paid"
            if not exempt:
                cb.data = f"admin:users:grant:{ids.user}"
                await admin.admin_section(cb, session, state)
                assert await has_valid_access(session, ids.user, now)
                assert not user.access_suspended

    asyncio.run(database_scenario(scenario))


@pytest.mark.parametrize("payment_status", ["pending", "paid"])
def test_payment_does_not_unban_suspended_user(monkeypatch, payment_status):
    async def scenario(sessions, ids, now):
        async with sessions() as session:
            user = await session.get(User, ids.user)
            user.access_suspended = True
            member = await session.get(Membership, ids.member)
            (await session.get(Flow, member.flow_id)).end_at = now + timedelta(days=20)
            payment = await session.get(Payment, ids.payment)
            payment.status = payment_status
            await session.commit()
            grant = AsyncMock(side_effect=AssertionError("Manual suspension bypassed"))
            monkeypatch.setattr(payments, "grant_access", grant)
            await payments.confirm_payment(
                session, object(), payment, paid_at=now, notify_user=False
            )
            await session.commit()
            grant.assert_not_awaited()
            assert payment.status == "paid"
            assert not await has_valid_access(session, ids.user, now)

    asyncio.run(database_scenario(scenario))


@pytest.mark.parametrize("kind", ["paid", "deadline", "exempt", "suspended"])
def test_audiences_share_exact_entitlement_rules(kind):
    async def scenario(sessions, ids, now):
        async with sessions() as session:
            user = await session.get(User, ids.user)
            member = await session.get(Membership, ids.member)
            if kind in {"paid", "suspended"}:
                member.status = "expired"
                (await session.get(Payment, ids.payment)).status = "paid"
                (await session.get(Flow, member.flow_id)).end_at = now + timedelta(
                    days=20
                )
            if kind == "suspended":
                user.access_suspended = True
            if kind == "exempt":
                user.access_exempt = True
                user.access_suspended = (
                    True  # Protection wins even in inconsistent data.
                )
            if kind == "deadline":
                member.pay_later_deadline_at = now + timedelta(days=2)
            await session.commit()
            valid = await has_valid_access(session, ids.user, now)
            assert valid == (kind != "suspended")
            assert (
                ids.user in await mailings.custom_audience_ids(session, "active")
            ) == valid
            assert (
                ids.user in await mailings.custom_audience_ids(session, "former")
            ) != valid

    asyncio.run(database_scenario(scenario))


@pytest.mark.parametrize(
    "outcome",
    ["paid", "canceled", "pending", "mismatch", "unavailable", "no_flow", "suspended"],
)
def test_payment_review_resolves_only_verified_orders(monkeypatch, outcome):
    from bot.services.payment_reviews import resolve_payment_review

    async def scenario(sessions, ids, now):
        async with sessions() as session:
            member = await session.get(Membership, ids.member)
            flow_id = member.flow_id
            (await session.get(Flow, flow_id)).end_at = now + timedelta(days=20)
            payment = await session.get(Payment, ids.payment)
            payment.status = "needs_review"
            payment.flow_id = None
            if outcome == "suspended":
                (await session.get(User, ids.user)).access_suspended = True
            await session.commit()
            remote = dict(
                id="qa-payment",
                status="succeeded",
                amount=dict(value="1990.00", currency="RUB"),
                metadata=dict(
                    internal_payment_id=str(ids.payment), user_id=str(ids.user)
                ),
            )
            if outcome in {"canceled", "pending"}:
                remote["status"] = outcome
            if outcome == "mismatch":
                remote["amount"]["value"] = "1.00"
            adapter = SimpleNamespace(get_payment=AsyncMock(return_value=remote))
            if outcome == "unavailable":
                adapter.get_payment.side_effect = OSError("offline")
            grant = AsyncMock(return_value=AccessChangeResult(True, True))
            monkeypatch.setattr(payments, "grant_access", grant)
            bot = SimpleNamespace(send_message=AsyncMock())
            result = await resolve_payment_review(
                session,
                bot,
                adapter,
                ids.payment,
                actor_tg_id=42,
                flow_id=None if outcome == "no_flow" else flow_id,
            )
            expected = {
                "paid": "paid",
                "suspended": "paid",
                "canceled": "failed",
                "pending": "pending",
            }.get(outcome, "needs_review")
            assert payment.status == expected, result
            # Confirmation commits a delivery intent; Telegram runs afterwards.
            grant.assert_not_awaited()
            receipt = await session.get(PaymentReceipt, ids.payment)
            assert (receipt is not None) == (outcome in {"paid", "suspended"})
            if outcome in {"paid", "suspended"}:
                # Stale second admin click cannot rebind, regrant or notify twice.
                await resolve_payment_review(
                    session, bot, adapter, ids.payment, actor_tg_id=43, flow_id=99999
                )
                assert payment.flow_id == flow_id
                assert adapter.get_payment.await_count == 1

    asyncio.run(database_scenario(scenario))
