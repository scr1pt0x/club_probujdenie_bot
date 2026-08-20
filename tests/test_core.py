import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx

from bot.access_control import service as access_service
from bot.admin.keyboards import user_card_kb
from bot.admin.templates import DEFAULT_TEMPLATES, TEMPLATE_LABELS
from bot.db.models import MembershipStatus
from bot.handlers.menu import _pay_later_screen, _shop_menu_kb
from bot.payments.verification import validate_remote_payment
from bot.services import memberships as membership_service
from bot.services import payments as payment_service
from bot.services.flows import sales_window_for_start
from bot.services.memberships import PayLaterEligibility
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
    keyboard = main_menu_kb().inline_keyboard
    labels = [[button.text for button in row] for row in keyboard]
    callbacks = [[button.callback_data for button in row] for row in keyboard]
    assert labels[0] == ["💳 Оплата", "👤 Мой доступ"]
    assert callbacks[0] == ["nav:payment", "nav:status"]
    assert labels[-1] == ["💬 Помощь"]


def test_admin_card_makes_complimentary_protection_explicit():
    normal_labels = [
        button.text for row in user_card_kb(7).inline_keyboard for button in row
    ]
    exempt_labels = [
        button.text
        for row in user_card_kb(7, access_exempt=True).inline_keyboard
        for button in row
    ]
    assert "🛡 Сделать льготницей" in normal_labels
    assert "🔓 Снять льготную защиту" in exempt_labels


def test_legacy_pay_later_template_is_not_exposed_in_admin():
    assert "pay_later_unavailable" not in DEFAULT_TEMPLATES
    assert "pay_later_unavailable" not in TEMPLATE_LABELS


def test_long_telegram_messages_are_split_without_data_loss():
    text = "A" * 30 + "\n" + "B" * 30
    chunks = split_message(text, limit=40)
    assert chunks == ["A" * 30, "B" * 30]
    assert all(len(chunk) <= 40 for chunk in chunks)


def test_tariffs_have_one_personal_checkout_instead_of_conflicting_prices():
    keyboard = _shop_menu_kb("0 ₽", include_free_offer=False)
    buttons = keyboard.inline_keyboard
    assert len(buttons) == 2
    assert buttons[0][0].callback_data == "shop:order:personal"
    assert buttons[-1][0].callback_data == "nav:home"


def test_pay_later_screen_never_appends_internal_no_membership_note():
    deadline = NOW + timedelta(days=3)
    text, _ = _pay_later_screen(
        PayLaterEligibility(True, "Отсрочка доступна.", deadline=deadline)
    )
    assert "23.08.2026" in text
    assert "нет активного участия" not in text
    assert "(" not in text


def test_sales_window_is_exactly_one_week_before_and_after_start():
    start = NOW + timedelta(days=10)
    open_at, close_at = sales_window_for_start(start)
    assert open_at == start - timedelta(days=7)
    assert close_at == start + timedelta(days=7)


def test_payment_is_not_attached_to_future_flow_outside_sales_window(monkeypatch):
    calls = []

    async def no_open_flow(_session, now):
        calls.append(now)
        return None

    monkeypatch.setattr(
        payment_service.flow_repo, "get_paid_flow_in_sales_window", no_open_flow
    )
    flow_id = run(payment_service.resolve_flow_for_payment(SimpleNamespace(), NOW))
    assert flow_id is None
    assert calls == [NOW]


def test_grant_access_never_kicks_an_existing_member(monkeypatch):
    calls = []

    class FakeBot:
        async def unban_chat_member(self, **kwargs):
            calls.append(("unban", kwargs))

        async def create_chat_invite_link(self, **kwargs):
            calls.append(("invite", kwargs))
            return SimpleNamespace(invite_link=f"https://t.me/+{kwargs['chat_id']}")

    monkeypatch.setattr(
        access_service,
        "settings",
        SimpleNamespace(primary_channel_id=-1001, secondary_discussion_id=-1002),
    )
    result = run(access_service.grant_access(FakeBot(), 42))

    unbans = [kwargs for kind, kwargs in calls if kind == "unban"]
    assert len(unbans) == 2
    assert all(kwargs["only_if_banned"] is True for kwargs in unbans)
    assert result.successful


def test_revoke_access_never_removes_configured_administrator(monkeypatch):
    class FailIfCalledBot:
        async def ban_chat_member(self, **kwargs):
            raise AssertionError("Telegram ban must not be called for an administrator")

    monkeypatch.setattr(
        access_service,
        "settings",
        SimpleNamespace(
            primary_channel_id=-1001,
            secondary_discussion_id=-1002,
            admin_tg_ids=[42],
        ),
    )
    result = run(access_service.revoke_access(FailIfCalledBot(), 42))
    assert result.successful
    assert result.protected


def test_revoke_stops_before_group_when_channel_operation_fails(monkeypatch):
    calls = []

    async def fail_first(_bot, chat_id, tg_id):
        calls.append((chat_id, tg_id))
        return False

    monkeypatch.setattr(
        access_service,
        "settings",
        SimpleNamespace(
            primary_channel_id=-1001,
            secondary_discussion_id=-1002,
            admin_tg_ids=[],
        ),
    )
    monkeypatch.setattr(access_service, "_safe_ban", fail_first)

    result = run(access_service.revoke_access(SimpleNamespace(), 42))

    assert calls == [(-1001, 42)]
    assert not result.successful


def test_confirmed_payment_locks_user_before_granting_access(monkeypatch):
    calls = []

    async def lock_user(_session, user_id):
        calls.append(("lock", user_id))
        return SimpleNamespace(id=user_id, tg_id=42)

    async def grant(_bot, tg_id):
        calls.append(("grant", tg_id))
        return access_service.AccessChangeResult(channel_ok=True, group_ok=True)

    monkeypatch.setattr(payment_service.user_repo, "lock_user_by_id", lock_user)
    monkeypatch.setattr(payment_service, "grant_access", grant)
    payment = SimpleNamespace(status="paid", user_id=7)

    result = run(
        payment_service.confirm_payment(
            SimpleNamespace(), SimpleNamespace(), payment, notify_user=False
        )
    )

    assert result.successful
    assert calls == [("lock", 7), ("grant", 42)]


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
