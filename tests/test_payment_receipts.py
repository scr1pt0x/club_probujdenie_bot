import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
from sqlalchemy import select
from test_postgres_safety import URL, database_scenario

from bot.access_control import service as access_service
from bot.db.models import Flow, Membership, Payment, PaymentReceipt, User
from bot.services import payment_receipts as receipts
from bot.services import payments


def fake_bot(channel="member", group="member"):
    async def get_member(*, chat_id, user_id):
        status = (
            channel if chat_id == access_service.settings.primary_channel_id else group
        )
        return SimpleNamespace(status=status, is_member=status == "restricted")

    return SimpleNamespace(
        get_chat_member=AsyncMock(side_effect=get_member),
        unban_chat_member=AsyncMock(return_value=True),
        ban_chat_member=AsyncMock(side_effect=AssertionError("Never ban on payment")),
        create_chat_invite_link=AsyncMock(
            return_value=SimpleNamespace(invite_link="https://example.invalid/join")
        ),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)),
        edit_message_text=AsyncMock(return_value=SimpleNamespace(message_id=77)),
    )


@pytest.mark.parametrize(
    "channel,group,missing",
    [
        ("member", "member", 0),
        ("administrator", "creator", 0),
        ("restricted", "member", 0),
        ("left", "member", 1),
        ("member", "kicked", 1),
        ("left", "left", 2),
    ],
)
def test_invites_only_for_missing_chats(channel, group, missing):
    async def scenario():
        bot = fake_bot(channel, group)
        result = await access_service.prepare_paid_access(bot, 42)
        assert result.successful
        assert bot.unban_chat_member.await_count == missing
        assert bot.create_chat_invite_link.await_count == missing
        assert (
            len([x for x in (result.channel_link, result.group_link) if x]) == missing
        )
        for call in bot.unban_chat_member.await_args_list:
            assert call.kwargs["only_if_banned"] is True
        bot.ban_chat_member.assert_not_awaited()

    asyncio.run(scenario())


def test_unknown_membership_is_not_treated_as_absence():
    async def scenario():
        bot = fake_bot()
        bot.get_chat_member.side_effect = TimeoutError()
        result = await access_service.prepare_paid_access(bot, 42)
        assert not result.successful
        bot.unban_chat_member.assert_not_awaited()
        bot.create_chat_invite_link.assert_not_awaited()

    asyncio.run(scenario())


def test_payment_reconciliation_and_receipts_run_every_minute():
    from bot.scheduler.setup import setup_scheduler

    scheduler = setup_scheduler(object(), payment_adapter=object())
    assert scheduler.get_job("check_payments").trigger.interval.total_seconds() == 60
    assert scheduler.get_job("payment_receipts").trigger.interval.total_seconds() == 60
    assert scheduler.get_job("check_payments").max_instances == 1


def test_receipt_list_does_not_accept_non_admin(monkeypatch):
    from bot.admin import router as admin

    async def scenario():
        monkeypatch.setattr(admin, "settings", SimpleNamespace(admin_tg_ids=[42]))
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=43),
            data="admin:payments:receipts:0",
            answer=AsyncMock(),
        )
        view = AsyncMock()
        monkeypatch.setattr(admin, "payment_reviews_screen", view)
        await admin.admin_section(callback, object(), object())
        view.assert_not_awaited()
        callback.answer.assert_awaited_once()

    asyncio.run(scenario())


pg = pytest.mark.skipif(not URL, reason="Disposable TEST_DATABASE_URL required")


async def paid_fixture(sessions, ids, now):
    async with sessions() as session:
        payment = await session.get(Payment, ids.payment)
        payment.status = "paid"
        payment.paid_at = now
        member = await session.get(Membership, ids.member)
        member.access_end_at = now + timedelta(days=35)
        member.grace_end_at = now + timedelta(days=36)
        member.pay_later_deadline_at = None
        (await session.get(Flow, member.flow_id)).end_at = member.access_end_at
        await receipts.queue_receipt(session, payment.id)
        await session.commit()


@pg
@pytest.mark.parametrize(
    "channel,group",
    [("member", "member"), ("left", "member"), ("member", "left"), ("left", "left")],
)
@pytest.mark.parametrize("interactive", [False, True])
def test_every_success_path_shows_period_and_only_missing_links(
    channel, group, interactive
):
    async def scenario(sessions, ids, now):
        await paid_fixture(sessions, ids, now)
        bot = fake_bot(channel, group)
        responder = (
            SimpleNamespace(
                answer=AsyncMock(return_value=SimpleNamespace(message_id=91))
            )
            if interactive
            else None
        )
        async with sessions() as session:
            await receipts.deliver_receipt(
                session, bot, ids.payment, responder=responder
            )
        rendered = responder.answer if interactive else bot.send_message
        call = rendered.await_args
        text = call.args[0] if interactive else call.args[1]
        buttons = [
            b for row in call.kwargs["reply_markup"].inline_keyboard for b in row
        ]
        assert "до " in text
        assert "Повторно платить" in text
        assert sum(bool(b.url) for b in buttons) == (channel == "left") + (
            group == "left"
        )
        assert any(b.callback_data == "payment:access" for b in buttons)
        if channel == group == "member":
            assert "Участие продлено" in text
            assert "Повторно вступать не нужно" in text
        async with sessions() as session:
            await receipts.process_receipts(session, bot)
            receipt = await session.get(PaymentReceipt, ids.payment)
            assert receipt.status == "sent"
            assert receipt.attempts == 1
            assert (await session.get(Payment, ids.payment)).status == "paid"
        assert bot.send_message.await_count == int(not interactive)
        assert rendered.await_count == 1

    asyncio.run(database_scenario(scenario))


@pg
@pytest.mark.parametrize(
    "failure,expected",
    [
        ("timeout", "unknown"),
        ("blocked", "blocked"),
        ("bad_request", "failed"),
        ("rate_limit", "pending"),
    ],
)
def test_delivery_failure_preserves_payment_and_has_bounded_recovery(failure, expected):
    async def scenario(sessions, ids, now):
        await paid_fixture(sessions, ids, now)
        bot = fake_bot()
        method = SendMessage(chat_id=42, text="test")
        errors = {
            "timeout": TimeoutError(),
            "blocked": TelegramForbiddenError(method=method, message="blocked"),
            "bad_request": TelegramBadRequest(method=method, message="bad"),
            "rate_limit": TelegramRetryAfter(
                method=method, message="retry", retry_after=120
            ),
        }
        bot.send_message.side_effect = errors[failure]
        async with sessions() as session:
            await receipts.deliver_receipt(session, bot, ids.payment)
        async with sessions() as session:
            receipt = await session.get(PaymentReceipt, ids.payment)
            assert receipt.status == expected
            assert receipt.error_code
            assert (await session.get(Payment, ids.payment)).status == "paid"
            await receipts.process_receipts(session, bot)
        assert bot.send_message.await_count == 1
        bot.send_message.side_effect = None
        if failure == "rate_limit":
            async with sessions() as session:
                (await session.get(PaymentReceipt, ids.payment)).available_at = (
                    now - timedelta(seconds=1)
                )
                await session.commit()
                await receipts.process_receipts(session, bot)
            assert bot.send_message.await_count == 2
        else:
            # An explicit recovery updates the menu; no blind duplicate send.
            responder = SimpleNamespace(
                answer=AsyncMock(return_value=SimpleNamespace(message_id=91))
            )
            async with sessions() as session:
                await receipts.deliver_receipt(
                    session, bot, ids.payment, responder=responder
                )
            responder.answer.assert_awaited_once()
            assert bot.send_message.await_count == 1
        async with sessions() as session:
            assert (await session.get(PaymentReceipt, ids.payment)).status == "sent"

    asyncio.run(database_scenario(scenario))


@pg
def test_two_workers_send_only_once():
    async def scenario(sessions, ids, now):
        await paid_fixture(sessions, ids, now)
        bot = fake_bot()
        async with sessions() as first, sessions() as second:
            await asyncio.gather(
                receipts.deliver_receipt(first, bot, ids.payment),
                receipts.deliver_receipt(second, bot, ids.payment),
            )
        bot.send_message.assert_awaited_once()

    asyncio.run(database_scenario(scenario))


@pg
def test_partial_link_failure_repairs_the_same_message():
    async def scenario(sessions, ids, now):
        await paid_fixture(sessions, ids, now)
        bot = fake_bot("left", "member")
        bot.create_chat_invite_link.side_effect = TimeoutError()
        async with sessions() as session:
            await receipts.deliver_receipt(session, bot, ids.payment)
        async with sessions() as session:
            row = await session.get(PaymentReceipt, ids.payment)
            assert row.status == "pending" and row.message_id == 77
            row.available_at = now - timedelta(seconds=1)
            await session.commit()
        bot.create_chat_invite_link.side_effect = None
        async with sessions() as session:
            await receipts.process_receipts(session, bot)
            assert (await session.get(PaymentReceipt, ids.payment)).status == "sent"
        bot.send_message.assert_awaited_once()
        bot.edit_message_text.assert_awaited_once()

    asyncio.run(database_scenario(scenario))


@pg
@pytest.mark.parametrize("restricted", ["suspended", "expired"])
def test_recovery_does_not_restore_ineligible_access(restricted):
    async def scenario(sessions, ids, now):
        await paid_fixture(sessions, ids, now)
        async with sessions() as session:
            if restricted == "suspended":
                (await session.get(User, ids.user)).access_suspended = True
            else:
                member = await session.get(Membership, ids.member)
                member.access_end_at = member.grace_end_at = now - timedelta(days=1)
                (await session.get(Flow, member.flow_id)).end_at = now - timedelta(
                    days=1
                )
            await session.commit()
            bot = fake_bot("left", "left")
            await receipts.deliver_receipt(session, bot, ids.payment)
        bot.get_chat_member.assert_not_awaited()
        bot.unban_chat_member.assert_not_awaited()
        bot.send_message.assert_awaited_once()

    asyncio.run(database_scenario(scenario))


@pg
def test_confirmation_queues_atomically_and_does_not_send_before_commit():
    async def scenario(sessions, ids, now):
        bot = fake_bot()
        async with sessions() as session:
            member = await session.get(Membership, ids.member)
            (await session.get(Flow, member.flow_id)).end_at = now + timedelta(days=35)
            await session.commit()
            await payments.confirm_payment(
                session, bot, await session.get(Payment, ids.payment), paid_at=now
            )
            assert (await session.get(PaymentReceipt, ids.payment)).status == "pending"
            bot.send_message.assert_not_awaited()
            bot.get_chat_member.assert_not_awaited()
            await session.rollback()
        async with sessions() as session:
            assert await session.get(PaymentReceipt, ids.payment) is None
            assert (await session.get(Payment, ids.payment)).status == "pending"
            await payments.confirm_payment(
                session, bot, await session.get(Payment, ids.payment), paid_at=now
            )
            await session.commit()
        async with sessions() as session:
            await receipts.process_receipts(session, bot)
            assert (await session.get(PaymentReceipt, ids.payment)).status == "sent"
        bot.send_message.assert_awaited_once()

    asyncio.run(database_scenario(scenario))


@pg
def test_restart_with_uncertain_attempt_does_not_duplicate():
    async def scenario(sessions, ids, now):
        await paid_fixture(sessions, ids, now)
        async with sessions() as session:
            row = await session.get(PaymentReceipt, ids.payment)
            row.status = "sending"
            row.updated_at = now - timedelta(minutes=10)
            await session.commit()
        bot = fake_bot()
        async with sessions() as session:
            await receipts.process_receipts(session, bot)
            assert (await session.get(PaymentReceipt, ids.payment)).status == "unknown"
        bot.send_message.assert_not_awaited()

    asyncio.run(database_scenario(scenario))


@pg
def test_old_paid_records_are_not_backfilled_or_broadcast():
    async def scenario(sessions, ids, now):
        async with sessions() as session:
            (await session.get(Payment, ids.payment)).status = "paid"
            await session.commit()
            bot = fake_bot()
            await receipts.process_receipts(session, bot)
            assert not list((await session.scalars(select(PaymentReceipt))).all())
        bot.send_message.assert_not_awaited()

    asyncio.run(database_scenario(scenario))


@pg
@pytest.mark.parametrize("route", ["refresh", "checkout", "promo"])
def test_actual_payment_handlers_record_menu_delivery(monkeypatch, route):
    from bot.handlers import menu

    async def scenario(sessions, ids, now):
        async with sessions() as session:
            member = await session.get(Membership, ids.member)
            flow = await session.get(Flow, member.flow_id)
            flow.start_at = now + timedelta(days=3)
            flow.end_at = now + timedelta(days=38)
            flow.sales_open_at = now - timedelta(days=4)
            flow.sales_close_at = now + timedelta(days=10)
            if route == "promo":
                (await session.get(Payment, ids.payment)).status = "failed"
            await session.commit()
        bot = fake_bot()
        remote = dict(
            id="qa-payment",
            status="succeeded",
            metadata=dict(internal_payment_id=str(ids.payment), user_id=str(ids.user)),
            amount=dict(value="1990.00", currency="RUB"),
        )
        create = AsyncMock(side_effect=AssertionError("No new monetary order"))
        monkeypatch.setattr(
            menu,
            "YooKassaAdapter",
            lambda: SimpleNamespace(
                get_payment=AsyncMock(return_value=remote),
                create_payment=create,
            ),
        )
        if route == "promo":
            monkeypatch.setattr(menu, "calculate_price_rub", AsyncMock(return_value=0))
        rendered = SimpleNamespace(message_id=91)
        message = SimpleNamespace(bot=bot, edit_text=AsyncMock(return_value=rendered))
        actor = SimpleNamespace(
            id=900000001, username=None, first_name="QA", last_name=None
        )
        callback = SimpleNamespace(from_user=actor, message=message, answer=AsyncMock())
        async with sessions() as session:
            if route == "refresh":
                await menu.payment_refresh_handler(callback, session)
            else:
                await menu._send_personal_payment_link(
                    session, actor, menu.ScreenResponder(message, edit_existing=True)
                )
        assert "Участие продлено" in message.edit_text.await_args.args[0]
        create.assert_not_awaited()
        async with sessions() as session:
            rows = list((await session.scalars(select(PaymentReceipt))).all())
            assert len(rows) == 1 and rows[0].status == "sent"
            await receipts.process_receipts(session, bot)
        bot.send_message.assert_not_awaited()
        bot.create_chat_invite_link.assert_not_awaited()
        bot.ban_chat_member.assert_not_awaited()

    asyncio.run(database_scenario(scenario))


@pg
def test_manual_extension_can_recover_access_after_flow_end():
    from bot.handlers import menu

    async def scenario(sessions, ids, now):
        await paid_fixture(sessions, ids, now)
        bot = fake_bot()
        responder = SimpleNamespace(
            bot=bot, answer=AsyncMock(return_value=SimpleNamespace(message_id=91))
        )
        async with sessions() as session:
            member = await session.get(Membership, ids.member)
            member.last_payment_id = ids.payment
            (await session.get(Flow, member.flow_id)).end_at = now - timedelta(days=1)
            await session.commit()
            await menu._send_paid_access_links(session, responder, 900000001)
        assert "Участие продлено" in responder.answer.await_args.args[0]
        bot.ban_chat_member.assert_not_awaited()

    asyncio.run(database_scenario(scenario))


@pg
def test_delivery_journal_lists_failure_without_sending():
    from bot.admin.payment_reviews import payment_reviews_screen

    async def scenario(sessions, ids, now):
        await paid_fixture(sessions, ids, now)
        edit = AsyncMock()
        callback = SimpleNamespace(
            answer=AsyncMock(), message=SimpleNamespace(edit_text=edit)
        )
        async with sessions() as session:
            (await session.get(PaymentReceipt, ids.payment)).status = "unknown"
            await session.commit()
            await payment_reviews_screen(callback, session, "payments:receipts:0")
        assert "результат отправки неизвестен" in edit.await_args.args[0]
        assert "без автоповтора" in edit.await_args.args[0]

    asyncio.run(database_scenario(scenario))
