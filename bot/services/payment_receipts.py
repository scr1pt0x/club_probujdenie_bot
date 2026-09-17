"""Payment != delivery. Persist intent, claim before Telegram, retain uncertainty."""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from bot.access_control.service import prepare_paid_access
from bot.db.models import Flow, Membership, Payment, PaymentReceipt, PaymentStatus
from bot.repositories.audit_log import add_audit_log
from bot.repositories.users import lock_user_by_id
from bot.services.entitlements import has_valid_access
from config import settings

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


async def queue_receipt(session, payment_id):
    now = datetime.now(timezone.utc)
    await session.execute(
        insert(PaymentReceipt)
        .values(
            payment_id=payment_id,
            status="pending",
            attempts=0,
            available_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=[PaymentReceipt.payment_id])
    )


async def receipt_content(session, payment, user, bot, access=None):
    """Called under the user lock, using current entitlement and Telegram state."""
    rows = [
        [
            InlineKeyboardButton(
                text="🔄 Обновить доступ и ссылки", callback_data="payment:access"
            )
        ],
        [InlineKeyboardButton(text="← Главное меню", callback_data="nav:home")],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    if user.access_suspended and not user.access_exempt:
        return (
            "Оплата подтверждена. Доступ ограничен администратором. "
            "Не платите повторно — обратитесь к администратору.",
            kb,
            False,
        )
    now = datetime.now(timezone.utc)
    if not await has_valid_access(session, user.id, now):
        return (
            "Оплата сохранена, но срок доступа уже завершён. "
            "Проверьте участие в главном меню или обратитесь к администратору.",
            kb,
            False,
        )
    member = (
        await session.scalars(
            select(Membership).where(
                Membership.user_id == user.id, Membership.flow_id == payment.flow_id
            )
        )
    ).first()
    flow = await session.get(Flow, payment.flow_id) if payment.flow_id else None
    end = member.access_end_at if member else (flow.end_at if flow else None)
    period = (
        end.astimezone(ZoneInfo(settings.scheduler_timezone)).strftime(
            "%d.%m.%Y в %H:%M"
        )
        if end
        else None
    )
    access = access or await prepare_paid_access(bot, user.tg_id)
    both_present = access.channel_present and access.group_present
    message = "✅ Участие продлено" if both_present else "✅ Участие подтверждено"
    if period:
        message += f" до {period} ({settings.scheduler_timezone})"
    message += ".\nПовторно платить за этот поток не нужно.\n\n"
    if both_present:
        message += "Вы уже в канале и группе. Повторно вступать не нужно."
    else:
        for label, present, link in (
            ("канал", access.channel_present, access.channel_link),
            ("группу", access.group_present, access.group_link),
        ):
            if present:
                message += f"Доступ в {label} уже есть.\n"
            elif link:
                message += (
                    f"Для входа в {label} нажмите кнопку ниже и отправьте заявку.\n"
                )
                rows.insert(
                    0, [InlineKeyboardButton(text=f"Войти в {label}", url=link)]
                )
            else:
                message += f"Не удалось проверить или подготовить вход в {label}. "
                message += "Оплата сохранена; нажмите «Обновить доступ и ссылки».\n"
    return message, InlineKeyboardMarkup(inline_keyboard=rows), not access.successful


async def deliver_receipt(session, bot, payment_id, *, responder=None, access=None):
    """Commit money first. responder denotes an explicit user interaction."""
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.status != PaymentStatus.PAID:
        if responder:
            await responder.answer(
                "Платёж ещё не подтверждён. Повторно платить не нужно; "
                "обратитесь к администратору."
            )
        return
    user = await lock_user_by_id(session, payment.user_id)
    if user is None:
        await session.rollback()
        return
    await session.refresh(payment)
    if payment.status != PaymentStatus.PAID:
        await session.commit()
        return
    if responder:
        await queue_receipt(session, payment_id)
    receipt = (
        await session.scalars(
            select(PaymentReceipt)
            .where(PaymentReceipt.payment_id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).first()
    now = datetime.now(timezone.utc)
    if receipt is None:
        await session.commit()
        return
    if receipt.status == "sending":
        # A crash after the durable claim is ambiguous: never blindly resend.
        if now - receipt.updated_at > timedelta(minutes=5):
            receipt.status = "unknown"
            receipt.error_code = "interrupted_attempt"
        else:
            await session.commit()
            if responder:
                await responder.answer(
                    "Подтверждение готовится. Повторите проверку через минуту."
                )
            return
    if responder is None and (
        receipt.status != "pending"
        or receipt.available_at > now
        or receipt.attempts >= MAX_ATTEMPTS
    ):
        await session.commit()
        return
    receipt.status = "sending"
    receipt.attempts += 1
    receipt.updated_at = now
    receipt.error_code = None
    await session.commit()  # A second worker/restart cannot repeat a send.
    user = await lock_user_by_id(session, payment.user_id)
    # Recheck current access after acquiring the lock, not a stale pre-commit snapshot.
    try:
        text, kb, repair = await receipt_content(session, payment, user, bot, access)
        if responder:
            rendered = await responder.answer(text, reply_markup=kb)
        elif receipt.message_id:
            try:
                rendered = await bot.edit_message_text(
                    chat_id=user.tg_id,
                    message_id=receipt.message_id,
                    text=text,
                    reply_markup=kb,
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
                rendered = None
        else:
            rendered = await bot.send_message(user.tg_id, text, reply_markup=kb)
        if getattr(rendered, "message_id", None) is not None:
            receipt.message_id = rendered.message_id
        # Repairs edit the same confirmation. Never create a stream of new messages.
        receipt.status = (
            "pending"
            if repair
            and responder is None
            and receipt.message_id
            and receipt.attempts < MAX_ATTEMPTS
            else "sent"
        )
        receipt.available_at = now + timedelta(minutes=1)
        receipt.error_code = "access_incomplete" if repair else None
        await add_audit_log(
            session,
            "payment_receipt_delivered",
            {
                "payment_id": payment_id,
                "user_id": user.id,
                "via": "menu" if responder else "notification",
                "access_complete": not repair,
                "message_id": receipt.message_id,
            },
        )
    except TelegramRetryAfter as exc:
        receipt.status = "pending" if receipt.attempts < MAX_ATTEMPTS else "failed"
        receipt.available_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(60, exc.retry_after + 1)
        )
        receipt.error_code = "rate_limited"
    except TelegramForbiddenError:
        receipt.status, receipt.error_code = "blocked", "telegram_forbidden"
    except TelegramBadRequest:
        receipt.status, receipt.error_code = "failed", "telegram_bad_request"
    except (TelegramAPIError, TimeoutError, OSError):
        # Editing a known message is repeatable; an unknown send is not.
        receipt.status = (
            "pending"
            if receipt.message_id and receipt.attempts < MAX_ATTEMPTS
            else "unknown"
        )
        receipt.available_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        receipt.error_code = "transport_uncertain"
    except Exception:
        # Keep the durable 'sending' claim if DB/rendering fails after the send.
        await session.rollback()
        logger.exception("Payment receipt interrupted: payment=%s", payment_id)
        return
    receipt.updated_at = datetime.now(timezone.utc)
    await session.commit()


async def process_receipts(session, bot):
    now = datetime.now(timezone.utc)
    ids = list(
        (
            await session.scalars(
                select(PaymentReceipt.payment_id)
                .where(
                    (PaymentReceipt.status == "pending")
                    & (PaymentReceipt.available_at <= now)
                    | (PaymentReceipt.status == "sending")
                    & (PaymentReceipt.updated_at < now - timedelta(minutes=5))
                )
                .order_by(PaymentReceipt.available_at)
                .limit(30)
            )
        ).all()
    )
    await session.commit()
    for payment_id in ids:
        try:
            await deliver_receipt(session, bot, payment_id)
        except Exception:
            await session.rollback()
            logger.exception("Receipt processing failed: payment=%s", payment_id)
