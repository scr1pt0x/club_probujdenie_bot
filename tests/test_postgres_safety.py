"""Real PostgreSQL races; opt in with TEST_DATABASE_URL (never production).

Every test creates an isolated qa_* schema and synthetic users. Telegram is fake.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.access_control.service import AccessChangeResult
from bot.db.base import Base
from bot.db.models import Flow, Membership, Payment, PromoCode, User
from bot.repositories.payments import get_payment_by_external_id
from bot.repositories.users import lock_user_by_id
from bot.scheduler import jobs
from bot.services import mailings
from bot.services.delivery import claim_attempt

URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL is not configured")


async def database_scenario(check):
    schema = "qa_" + uuid4().hex
    admin_engine = create_async_engine(URL)
    async with admin_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        URL, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with sessions() as session:
            user = User(tg_id=900000001, access_exempt=False)
            flow = Flow(
                title="QA",
                start_at=now - timedelta(days=35),
                end_at=now - timedelta(days=1),
                duration_weeks=5,
                is_free=False,
                sales_open_at=now - timedelta(days=42),
                sales_close_at=now - timedelta(days=28),
            )
            session.add_all([user, flow])
            await session.flush()
            member = Membership(
                user_id=user.id,
                flow_id=flow.id,
                status="active",
                access_start_at=flow.start_at,
                access_end_at=flow.end_at,
                grace_end_at=now - timedelta(seconds=1),
                pay_later_deadline_at=now - timedelta(seconds=1),
            )
            payment = Payment(
                user_id=user.id,
                flow_id=flow.id,
                status="pending",
                provider="qa",
                external_id="qa-payment",
                amount_rub=1990,
            )
            session.add_all([member, payment])
            await session.commit()
            ids = SimpleNamespace(user=user.id, member=member.id, payment=payment.id)
        await check(sessions, ids, now)
    finally:
        await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.parametrize("job", ["expire_memberships", "enforce_pay_later_deadlines"])
def test_extension_during_expiry_selection_never_bans(monkeypatch, job):
    async def scenario(sessions, ids, now):
        original_lock = jobs.user_repo.lock_user_by_id

        async def renew_before_lock(session, user_id):
            async with sessions() as other:
                await original_lock(other, user_id)
                member = await other.get(Membership, ids.member)
                member.access_end_at = now + timedelta(days=7)
                member.grace_end_at = now + timedelta(days=8)
                member.pay_later_deadline_at = now + timedelta(days=7)
                await other.commit()
            return await original_lock(session, user_id)

        async def must_not_ban(*args, **kwargs):
            raise AssertionError("renewed participant must never be banned")

        monkeypatch.setattr(jobs.user_repo, "lock_user_by_id", renew_before_lock)
        monkeypatch.setattr(jobs, "_is_revoke_jobs_enabled", lambda: True)
        monkeypatch.setattr(jobs, "revoke_access", must_not_ban)
        async with sessions() as session:
            await getattr(jobs, job)(session, SimpleNamespace())
        async with sessions() as session:
            assert (await session.get(Membership, ids.member)).status == "active"

    asyncio.run(database_scenario(scenario))


def test_payment_and_user_refresh_after_waiting_for_lock():
    async def scenario(sessions, ids, now):
        async with sessions() as reader, sessions() as writer:
            user = await reader.get(User, ids.user)
            payment = await get_payment_by_external_id(reader, "qa-payment")
            await lock_user_by_id(writer, ids.user)
            changed_user = await writer.get(User, ids.user)
            changed_user.access_exempt = True
            changed_payment = await writer.get(Payment, ids.payment)
            changed_payment.status = "paid"
            await writer.commit()
            await lock_user_by_id(reader, ids.user)
            payment = await get_payment_by_external_id(reader, "qa-payment")
            assert user.access_exempt is True
            assert payment.status == "paid"

    asyncio.run(database_scenario(scenario))


@pytest.mark.parametrize("job", ["expire_memberships", "enforce_pay_later_deadlines"])
@pytest.mark.parametrize(
    "protection",
    [
        "exempt",
        "current_paid",
        "future_paid",
        "grace",
        "pay_later",
        "manual_extension",
        "pending_payment",
        "payment_review",
        "deferral_with_stale_grace",
    ],
)
def test_protected_participant_never_triggers_telegram_ban(
    monkeypatch, job, protection
):
    async def scenario(sessions, ids, now):
        async with sessions() as session:
            member = await session.get(Membership, ids.member)
            payment = await session.get(Payment, ids.payment)
            payment.status = "failed"
            if protection == "exempt":
                (await session.get(User, ids.user)).access_exempt = True
            elif protection in {"current_paid", "future_paid"}:
                flow = await session.get(Flow, member.flow_id)
                flow.start_at = (
                    now + timedelta(days=7)
                    if protection == "future_paid"
                    else now - timedelta(days=7)
                )
                flow.end_at = now + timedelta(days=35)
                payment.status = "paid"
            elif protection in {"grace", "manual_extension", "pay_later"}:
                member.grace_end_at = now + timedelta(days=2)
                member.pay_later_deadline_at = None
                if protection == "manual_extension":
                    member.access_end_at = now + timedelta(days=1)
                if protection == "pay_later":
                    member.pay_later_deadline_at = now + timedelta(days=1)
            elif protection == "pending_payment":
                payment.status = "pending"
            elif protection == "payment_review":
                payment.status = "needs_review"
            elif protection == "deferral_with_stale_grace":
                member.pay_later_deadline_at = now + timedelta(days=2)
            await session.commit()
        revoke = AsyncMock(
            side_effect=AssertionError("protected participant was banned")
        )
        monkeypatch.setattr(jobs, "_is_revoke_jobs_enabled", lambda: True)
        monkeypatch.setattr(jobs, "revoke_access", revoke)
        async with sessions() as session:
            await getattr(jobs, job)(session, SimpleNamespace())
        revoke.assert_not_awaited()

    asyncio.run(database_scenario(scenario))


def test_two_expiry_jobs_cannot_ban_the_same_member_twice(monkeypatch):
    async def scenario(sessions, ids, now):
        async with sessions() as session:
            (await session.get(Payment, ids.payment)).status = "failed"
            await session.commit()
        revoke = AsyncMock(return_value=AccessChangeResult(True, True))
        monkeypatch.setattr(jobs, "_is_revoke_jobs_enabled", lambda: True)
        monkeypatch.setattr(jobs, "revoke_access", revoke)
        bot = SimpleNamespace(send_message=AsyncMock())
        async with sessions() as first, sessions() as second:
            await asyncio.gather(
                jobs.expire_memberships(first, bot),
                jobs.enforce_pay_later_deadlines(second, bot),
            )
        assert revoke.await_count == 1
        async with sessions() as session:
            assert (await session.get(Membership, ids.member)).status == "expired"

    asyncio.run(database_scenario(scenario))


def test_campaign_claim_is_atomic_across_database_sessions():
    async def scenario(sessions, ids, now):
        async with sessions() as first, sessions() as second:
            claimed = await asyncio.gather(
                claim_attempt(first, "qa_claim", "same_campaign"),
                claim_attempt(second, "qa_claim", "same_campaign"),
            )
        assert sorted(claimed) == [False, True]

    asyncio.run(database_scenario(scenario))


def test_custom_broadcast_resume_never_sends_twice(monkeypatch):
    async def scenario(sessions, ids, now):
        monkeypatch.setattr(
            mailings, "get_mailings_enabled", AsyncMock(return_value=True)
        )
        bot = SimpleNamespace(copy_message=AsyncMock())
        async with sessions() as session:
            args = dict(
                user_ids=[ids.user, ids.user],
                source_chat_id=42,
                source_message_id=10,
                key="qa-mailing",
            )
            first = await mailings.send_custom_broadcast(session, bot, **args)
            second = await mailings.send_custom_broadcast(session, bot, **args)
        assert first["sent"] == 1
        assert second["sent"] == 0
        assert second["skipped"] == 1
        assert bot.copy_message.await_count == 1

    asyncio.run(database_scenario(scenario))


def test_audiences_do_not_count_current_member_as_former():
    async def scenario(sessions, ids, now):
        async with sessions() as session:
            member = await session.get(Membership, ids.member)
            member.grace_end_at = now + timedelta(days=1)
            await session.commit()
            assert ids.user in await mailings.custom_audience_ids(session, "active")
            assert ids.user not in await mailings.custom_audience_ids(session, "former")
            member.status = "expired"
            (await session.get(User, ids.user)).access_exempt = True
            await session.commit()
            assert ids.user in await mailings.custom_audience_ids(session, "active")
            assert ids.user not in await mailings.custom_audience_ids(session, "former")

    asyncio.run(database_scenario(scenario))


def test_verified_payment_duplicate_webhooks_join_and_expiry(monkeypatch):
    from bot.handlers import join_requests
    from bot.webhooks import app as webhooks
    from config import settings

    async def scenario(sessions, ids, now):
        async with sessions() as session:
            member = await session.get(Membership, ids.member)
            flow = await session.get(Flow, member.flow_id)
            flow.start_at = now + timedelta(days=7)
            flow.end_at = now + timedelta(days=42)
            await session.commit()
        remote = dict(
            id="qa-payment",
            status="succeeded",
            metadata=dict(internal_payment_id=str(ids.payment), user_id=str(ids.user)),
            amount=dict(value="1990.00", currency="RUB"),
        )
        monkeypatch.setattr(webhooks, "AsyncSessionLocal", sessions)
        monkeypatch.setattr(
            webhooks,
            "YooKassaAdapter",
            lambda: SimpleNamespace(get_payment=AsyncMock(return_value=remote)),
        )
        bot = SimpleNamespace(
            unban_chat_member=AsyncMock(),
            create_chat_invite_link=AsyncMock(
                return_value=SimpleNamespace(invite_link="https://t.me/+QA")
            ),
            send_message=AsyncMock(),
            ban_chat_member=AsyncMock(),
            approve_chat_join_request=AsyncMock(),
            decline_chat_join_request=AsyncMock(),
        )
        transport = httpx.ASGITransport(app=webhooks.create_app(bot))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            payload = dict(event="payment.succeeded", object=dict(id="qa-payment"))
            responses = await asyncio.gather(
                *[client.post("/api/yookassa/webhook", json=payload) for _ in range(2)]
            )
        assert [r.status_code for r in responses] == [200, 200]
        async with sessions() as session:
            payment = await session.get(Payment, ids.payment)
            member = await session.get(Membership, ids.member)
            assert payment.status == "paid"
            assert member.status == "active"
            assert member.pay_later_deadline_at is None
            assert len((await session.scalars(select(Membership))).all()) == 1
            request = SimpleNamespace(
                chat=SimpleNamespace(id=settings.primary_channel_id),
                from_user=SimpleNamespace(id=900000001),
                bot=bot,
            )
            await join_requests.approve_join_request(request, session)
            monkeypatch.setattr(jobs, "_is_revoke_jobs_enabled", lambda: True)
            await jobs.expire_memberships(session, bot)
            await jobs.enforce_pay_later_deadlines(session, bot)
        bot.approve_chat_join_request.assert_awaited_once()
        bot.decline_chat_join_request.assert_not_awaited()
        bot.ban_chat_member.assert_not_awaited()
        assert bot.unban_chat_member.await_count == 2
        assert all(
            call.kwargs["only_if_banned"]
            for call in bot.unban_chat_member.await_args_list
        )
        assert bot.send_message.await_count == 1

    asyncio.run(database_scenario(scenario))


def test_last_promo_slot_keeps_discount_and_blocks_new_users():
    from bot.repositories.promos import add_user_promo
    from bot.services.promos import apply_promo_to_price

    async def scenario(sessions, ids, now):
        async with sessions() as session:
            session.add(
                PromoCode(
                    code="QA",
                    kind="percent",
                    value_int=50,
                    max_uses=1,
                    used_count=0,
                    active=True,
                )
            )
            second = User(tg_id=900000002)
            session.add(second)
            await session.commit()
            assert await add_user_promo(session, ids.user, "QA")
            await session.commit()
            assert await apply_promo_to_price(session, ids.user, 2000) == 1000
            assert not await add_user_promo(session, second.id, "QA")

    asyncio.run(database_scenario(scenario))


def test_flow_extension_preserves_and_extends_existing_rights():
    from bot.services.flows import extend_memberships_for_flow

    async def scenario(sessions, ids, now):
        async with sessions() as session:
            member = await session.get(Membership, ids.member)
            end_at = now + timedelta(days=5)
            await extend_memberships_for_flow(session, member.flow_id, end_at)
            await session.commit()
            assert member.access_end_at == end_at
            assert member.grace_end_at >= end_at
            assert member.pay_later_deadline_at == end_at
            await extend_memberships_for_flow(session, member.flow_id, now)
            await session.commit()
            assert member.access_end_at == end_at

    asyncio.run(database_scenario(scenario))


def test_restart_does_not_recreate_an_admin_edited_seed(monkeypatch):
    from bot.services import flows

    async def scenario(sessions, ids, now):
        monkeypatch.setattr(
            flows,
            "settings",
            SimpleNamespace(free_flows_enabled=False, paid_flow_start="2000-01-01"),
        )
        async with sessions() as session:
            await flows.ensure_seed_flows(session)
            await session.commit()
            assert len((await session.scalars(select(Flow))).all()) == 1

    asyncio.run(database_scenario(scenario))


def test_double_checkout_creates_only_one_provider_order(monkeypatch):
    from bot.handlers import menu

    async def scenario(sessions, ids, now):
        async with sessions() as session:
            (await session.get(Payment, ids.payment)).status = "failed"
            flow = await session.get(
                Flow, (await session.get(Membership, ids.member)).flow_id
            )
            flow.start_at = now + timedelta(days=3)
            flow.end_at = now + timedelta(days=38)
            flow.sales_open_at = now - timedelta(days=4)
            flow.sales_close_at = now + timedelta(days=10)
            await session.commit()
        created = []

        async def create(**kwargs):
            created.append(kwargs)
            return "qa-new-payment", "https://example.com/pay"

        async def remote(external_id):
            item = created[0]
            return dict(
                id=external_id,
                status="pending",
                metadata=item["metadata"],
                amount=dict(value=str(item["amount_rub"]), currency="RUB"),
                confirmation=dict(confirmation_url="https://example.com/pay"),
            )

        monkeypatch.setattr(
            menu,
            "YooKassaAdapter",
            lambda: SimpleNamespace(create_payment=create, get_payment=remote),
        )
        user = SimpleNamespace(
            id=900000001, username=None, first_name="QA", last_name=None
        )

        async def checkout():
            async with sessions() as session:
                await menu._send_personal_payment_link(
                    session, user, SimpleNamespace(bot=object(), answer=AsyncMock())
                )

        await asyncio.gather(checkout(), checkout())
        assert len(created) == 1
        async with sessions() as session:
            assert (
                len(
                    (
                        await session.scalars(
                            select(Payment).where(Payment.provider == "yookassa")
                        )
                    ).all()
                )
                == 1
            )

    asyncio.run(database_scenario(scenario))


@pytest.mark.parametrize("case", ["closed_sales", "review", "exempt"])
def test_checkout_never_creates_an_unsafe_invoice(monkeypatch, case):
    from bot.handlers import menu

    async def scenario(sessions, ids, now):
        async with sessions() as session:
            (await session.get(Payment, ids.payment)).status = (
                "needs_review" if case == "review" else "failed"
            )
            if case == "exempt":
                (await session.get(User, ids.user)).access_exempt = True
            await session.commit()
        create = AsyncMock(side_effect=AssertionError("unexpected payment order"))
        monkeypatch.setattr(
            menu, "YooKassaAdapter", lambda: SimpleNamespace(create_payment=create)
        )
        monkeypatch.setattr(
            menu, "grant_access", AsyncMock(return_value=AccessChangeResult(True, True))
        )
        user = SimpleNamespace(
            id=900000001, username=None, first_name="QA", last_name=None
        )
        async with sessions() as session:
            await menu._send_personal_payment_link(
                session, user, SimpleNamespace(bot=object(), answer=AsyncMock())
            )
        create.assert_not_awaited()

    asyncio.run(database_scenario(scenario))


@pytest.mark.parametrize("is_free,days", [(False, 3), (False, 1), (True, 7), (True, 3)])
def test_all_scheduled_end_reminder_dates_are_reachable(monkeypatch, is_free, days):
    async def scenario(sessions, ids, now):
        async with sessions() as session:
            member = await session.get(Membership, ids.member)
            flow = await session.get(Flow, member.flow_id)
            flow.is_free = is_free
            flow.end_at = now + timedelta(days=days)
            member.access_end_at = flow.end_at
            member.grace_end_at = flow.end_at + timedelta(days=1)
            await session.commit()
            monkeypatch.setattr(
                mailings, "get_mailings_enabled", AsyncMock(return_value=True)
            )
            send = AsyncMock(return_value=1)
            monkeypatch.setattr(mailings, "_send_bulk", send)
            assert await mailings.send_auto_end_mailings(session, object(), now) == 1
            assert send.await_count == 1
            assert f"end_minus_{days}" in send.await_args.kwargs["mailing_key"]

    asyncio.run(database_scenario(scenario))


@pytest.mark.parametrize("exempt", [True, False])
def test_self_service_links_require_current_entitlement(monkeypatch, exempt):
    from bot.handlers import membership as handler

    async def scenario(sessions, ids, now):
        async with sessions() as session:
            (await session.get(User, ids.user)).access_exempt = exempt
            await session.commit()
            grant = AsyncMock(
                return_value=AccessChangeResult(
                    True, True, "https://t.me/+QA1", "https://t.me/+QA2"
                )
            )
            monkeypatch.setattr(handler, "grant_access", grant)
            monkeypatch.setattr(handler, "edit_screen", AsyncMock())
            cb = SimpleNamespace(
                from_user=SimpleNamespace(id=900000001),
                bot=object(),
                message=object(),
                answer=AsyncMock(),
            )
            await handler.access_links_handler(cb, session)
            assert grant.await_count == int(exempt)

    asyncio.run(database_scenario(scenario))
