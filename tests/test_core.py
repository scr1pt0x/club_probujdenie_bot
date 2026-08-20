import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx

from bot.db.models import MembershipStatus
from bot.handlers.menu import _shop_menu_kb
from bot.payments.verification import validate_remote_payment
from bot.services import memberships as membership_service
from bot.ui.formatters import format_flow_period, format_local_date, format_price_rub
from bot.ui.keyboards import main_menu_kb
from bot.ui.messages import split_message
from bot.webhooks.app import create_app

NOW = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def test_price_and_date_formatters_are_user_friendly():
    assert format_price_rub(2990) == "2 990"
    assert format_local_date(NOW) == "20.08.2026"
    assert format_flow_period(NOW, NOW + timedelta(days=35)) == (
        "20.08.2026 — 24.09.2026"
    )


def test_main_menu_exposes_status_and_keeps_primary_actions_first():
    keyboard = main_menu_kb().keyboard
    labels = [[button.text for button in row] for row in keyboard]
    assert labels[0] == ["💳 Моя оплата", "👤 Мой статус"]
    assert labels[-1] == ["ℹ️ Помощь"]


def test_long_telegram_messages_are_split_without_data_loss():
    text = "A" * 30 + "\n" + "B" * 30
    chunks = split_message(text, limit=40)
    assert chunks == ["A" * 30, "B" * 30]
    assert all(len(chunk) <= 40 for chunk in chunks)


def test_tariffs_have_one_personal_checkout_instead_of_conflicting_prices():
    keyboard = _shop_menu_kb("0 ₽", include_free_offer=False)
    buttons = keyboard.inline_keyboard
    assert len(buttons) == 1
    assert buttons[0][0].callback_data == "shop:order:personal"


def test_remote_payment_must_match_identity_amount_and_currency():
    remote = {
        "id": "provider-1",
        "metadata": {"internal_payment_id": "12", "user_id": "7"},
        "amount": {"value": "1990.00", "currency": "RUB"},
    }
    kwargs = {
        "external_id": "provider-1",
        "internal_payment_id": 12,
        "user_id": 7,
        "amount_rub": 1990,
    }
    assert validate_remote_payment(remote, **kwargs) is None
    assert validate_remote_payment({**remote, "id": "other"}, **kwargs) == (
        "external_id_mismatch"
    )
    assert (
        validate_remote_payment(
            {**remote, "amount": {"value": "1.00", "currency": "RUB"}}, **kwargs
        )
        == "amount_mismatch"
    )
    assert (
        validate_remote_payment(
            {**remote, "amount": {"value": "1990.00", "currency": "USD"}}, **kwargs
        )
        == "currency_mismatch"
    )


def test_webhook_rejects_invalid_json_without_server_error():
    async def make_request():
        transport = httpx.ASGITransport(app=create_app(SimpleNamespace()))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.post(
                "/api/yookassa/webhook",
                content=b"{not-json",
                headers={"content-type": "application/json"},
            )

    response = run(make_request())
    assert response.status_code == 400


def test_pay_later_is_not_offered_for_stale_membership(monkeypatch):
    membership = SimpleNamespace(
        grace_end_at=NOW - timedelta(days=1),
        pay_later_deadline_at=None,
        access_end_at=NOW - timedelta(days=2),
    )

    async def get_membership(*args, **kwargs):
        return membership

    monkeypatch.setattr(
        membership_service.membership_repo, "get_active_membership", get_membership
    )
    result = run(membership_service.evaluate_pay_later(SimpleNamespace(), 1, NOW))
    assert not result.eligible
    assert "завершено" in result.message


def test_pay_later_reports_existing_deadline(monkeypatch):
    deadline = NOW + timedelta(days=3)
    membership = SimpleNamespace(
        grace_end_at=NOW + timedelta(days=1),
        pay_later_deadline_at=deadline,
        access_end_at=NOW,
    )

    async def get_membership(*args, **kwargs):
        return membership

    monkeypatch.setattr(
        membership_service.membership_repo, "get_active_membership", get_membership
    )
    result = run(membership_service.evaluate_pay_later(SimpleNamespace(), 1, NOW))
    assert not result.eligible
    assert "23.08.2026" in result.message


def test_apply_pay_later_updates_membership_after_eligibility_check(monkeypatch):
    membership = SimpleNamespace(
        status=MembershipStatus.ACTIVE,
        grace_end_at=NOW + timedelta(days=2),
        pay_later_used_at=None,
        pay_later_deadline_at=None,
        access_end_at=NOW + timedelta(days=1),
    )
    next_flow = SimpleNamespace(start_at=NOW + timedelta(days=2))

    async def get_membership(*args, **kwargs):
        return membership

    async def get_next_flow(*args, **kwargs):
        return next_flow

    async def get_settings(*args, **kwargs):
        return SimpleNamespace(pay_later_max_days=7, grace_days=1)

    monkeypatch.setattr(
        membership_service.membership_repo, "get_active_membership", get_membership
    )
    monkeypatch.setattr(membership_service, "get_next_paid_flow", get_next_flow)
    monkeypatch.setattr(membership_service, "get_effective_settings", get_settings)

    ok, message = run(membership_service.apply_pay_later(SimpleNamespace(), 1, NOW))
    assert ok
    assert message == "Отсрочка активна до 29.08.2026."
    assert membership.pay_later_deadline_at == NOW + timedelta(days=9)
    assert membership.access_end_at == NOW + timedelta(days=9)
    assert membership.grace_end_at == NOW + timedelta(days=10)
