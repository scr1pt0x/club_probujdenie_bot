"""Explicit, provider-verified resolution. No charges, refunds or force-paid path."""

from datetime import datetime, timezone

from bot.db.models import Flow, Payment, PaymentStatus
from bot.payments.verification import validate_remote_payment
from bot.repositories.audit_log import add_audit_log
from bot.repositories.users import lock_user_by_id
from bot.services.payments import confirm_payment, notify_payment_status


async def resolve_payment_review(
    session,
    bot,
    adapter,
    payment_id: int,
    *,
    actor_tg_id: int,
    flow_id: int | None = None,
) -> str:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        return "Платёж не найден."
    user = await lock_user_by_id(session, payment.user_id)
    await session.refresh(payment)
    if user is None:
        return "Профиль не найден. Проверка требует помощи разработчика."
    if payment.status != PaymentStatus.NEEDS_REVIEW:
        return f"Платёж уже обработан: {payment.status}. Повторных действий нет."
    if not payment.external_id:
        return "Нет идентификатора YooKassa. Нельзя безопасно подтвердить платёж."
    try:
        remote = await adapter.get_payment(payment.external_id)
    except Exception:
        return "YooKassa недоступна. Статус не изменён; повторите проверку позже."
    if not isinstance(remote, dict):
        return "Некорректный ответ YooKassa. Статус не изменён."
    error = validate_remote_payment(
        remote,
        external_id=payment.external_id,
        internal_payment_id=payment.id,
        user_id=payment.user_id,
        amount_rub=payment.amount_rub,
        currency=payment.currency,
    )
    if error:
        await add_audit_log(
            session,
            "payment_review_rejected",
            {
                "payment_id": payment.id,
                "reason": error,
                "actor_tg_id": actor_tg_id,
            },
        )
        await session.commit()
        return (
            f"Данные платежа не совпадают ({error}). Подтверждение запрещено. "
            "Нужна сверка заказа с разработчиком; не просите клиентку платить повторно."
        )

    now = datetime.now(timezone.utc)
    status = remote.get("status")
    old_flow_id = payment.flow_id
    if status == "succeeded":
        selected_id = flow_id if flow_id is not None else payment.flow_id
        flow = await session.get(Flow, selected_id) if selected_id else None
        if flow is None or flow.is_free or flow.end_at <= now:
            return (
                "Оплата подтверждена YooKassa, но нет подходящего потока. "
                "Выберите оплаченный поток ниже. Завершённые потоки не выдаются."
            )
        payment.flow_id = flow.id
        await confirm_payment(session, bot, payment, paid_at=now)
        if payment.status != PaymentStatus.PAID:
            return "Не удалось активировать участие. Платёж оставлен на проверке."
        message = (
            "Оплата подтверждена, участие сохранено. Ручное ограничение доступа "
            "не снято; восстановление — отдельным действием в карточке участницы."
            if user.access_suspended and not user.access_exempt
            else "Оплата подтверждена, участие активировано. "
            "Ссылки доступны в «Мой доступ»."
        )
    elif status == "canceled":
        payment.status = PaymentStatus.FAILED
        await notify_payment_status(
            session,
            bot,
            payment.user_id,
            "payment_failed",
            dedupe_key=f"payment:{payment.id}:payment_failed",
        )
        message = "YooKassa подтвердила отмену. Можно создать новый счёт в окне набора."
    elif status in {"pending", "waiting_for_capture"}:
        # Provider matches the order; scheduled reconciliation can safely resume.
        payment.status = PaymentStatus.PENDING
        message = "Платёж ещё не завершён. Автоматическая проверка возобновлена."
    else:
        return "Неизвестный статус YooKassa. Ручная проверка сохранена."

    await add_audit_log(
        session,
        "payment_review_resolved",
        {
            "payment_id": payment.id,
            "actor_tg_id": actor_tg_id,
            "provider_status": status,
            "status": payment.status,
            "previous_flow_id": old_flow_id,
            "flow_id": payment.flow_id,
        },
    )
    await session.commit()
    return message
