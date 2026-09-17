from datetime import datetime, timezone

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from bot.admin.keyboards import back_menu_kb
from bot.db.models import Flow, Payment, PaymentReceipt, PaymentStatus, User
from bot.payments.yookassa_adapter import YooKassaAdapter
from bot.services.payment_reviews import resolve_payment_review
from bot.ui.formatters import format_local_date
from bot.ui.navigation import edit_screen

RECEIPT_LABELS = {
    "pending": "ожидает доставки / повторной проверки",
    "sending": "доставка начата",
    "sent": "показано участнице",
    "unknown": "результат отправки неизвестен — без автоповтора",
    "blocked": "Telegram запретил отправку",
    "failed": "нужна ручная проверка доставки",
}


async def payment_reviews_screen(callback, session, section):
    # Called only after the central admin allowlist check.
    try:
        await callback.answer()
    except TelegramAPIError:
        pass
    parts = section.split(":")
    if len(parts) == 3 and parts[1] == "receipts" and parts[2].isdigit():
        offset = int(parts[2])
        entries = list(
            (
                await session.execute(
                    select(PaymentReceipt, Payment, User)
                    .join(Payment, Payment.id == PaymentReceipt.payment_id)
                    .join(User, User.id == Payment.user_id)
                    .order_by(PaymentReceipt.updated_at.desc())
                    .offset(offset)
                    .limit(11)
                )
            ).all()
        )
        lines = ["📨 Доставка подтверждений", ""]
        rows = []
        for receipt, payment, user in entries[:10]:
            who = f"@{user.username}" if user.username else f"ID {user.tg_id}"
            label = RECEIPT_LABELS.get(receipt.status, receipt.status)
            lines.append(f"#{payment.id} · {who}: {label}")
            if receipt.error_code:
                lines.append(f"Причина: {receipt.error_code}")
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"Платёж #{payment.id}",
                        callback_data=f"admin:payments:card:{payment.id}",
                    )
                ]
            )
        if not entries:
            lines.append(
                "Новых подтверждений пока нет. "
                "Старые оплаты автоматически не рассылаются."
            )
        if offset:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="← Предыдущие",
                        callback_data=f"admin:payments:receipts:{max(0, offset - 10)}",
                    )
                ]
            )
        if len(entries) > 10:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Следующие →",
                        callback_data=f"admin:payments:receipts:{offset + 10}",
                    )
                ]
            )
        lines.extend(
            [
                "",
                "При неизвестном результате уточните у участницы, "
                "видит ли она подтверждение. Повторно платить не нужно: "
                "доступ и ссылки открываются в разделе «Оплата».",
            ]
        )
        rows.extend(back_menu_kb("admin:payments").inline_keyboard)
        await edit_screen(
            callback.message,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return
    if len(parts) == 1 or (len(parts) == 3 and parts[1] == "page"):
        offset = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else 0
        payments = list(
            (
                await session.execute(
                    select(Payment)
                    .where(Payment.status == PaymentStatus.NEEDS_REVIEW)
                    .order_by(Payment.id)
                    .offset(offset)
                    .limit(11)
                )
            ).scalars()
        )
        rows = [
            [
                InlineKeyboardButton(
                    text=f"#{p.id} · {p.amount_rub} ₽ · участница #{p.user_id}",
                    callback_data=f"admin:payments:card:{p.id}",
                )
            ]
            for p in payments[:10]
        ]
        if offset:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="← Предыдущие",
                        callback_data=f"admin:payments:page:{max(0, offset - 10)}",
                    )
                ]
            )
        if len(payments) > 10:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Следующие →",
                        callback_data=f"admin:payments:page:{offset + 10}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="📨 Доставка подтверждений",
                    callback_data="admin:payments:receipts:0",
                )
            ]
        )
        rows.extend(back_menu_kb("admin:menu").inline_keyboard)
        await edit_screen(
            callback.message,
            "💳 Платежи на проверке\n\n"
            + (
                "Выберите платёж. Никаких повторных списаний при проверке нет."
                if payments
                else "Нет платежей, требующих ручной проверки."
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return
    if len(parts) not in {3, 4} or not parts[2].isdigit():
        return
    action, payment_id = parts[1], int(parts[2])
    payment = await session.get(Payment, payment_id)
    if payment is None:
        await edit_screen(
            callback.message,
            "Платёж не найден.",
            reply_markup=back_menu_kb("admin:payments"),
        )
        return
    if action in {"check", "resolve"}:
        flow_id = int(parts[3]) if len(parts) == 4 and parts[3].isdigit() else None
        result = await resolve_payment_review(
            session,
            callback.bot,
            YooKassaAdapter(),
            payment_id,
            actor_tg_id=callback.from_user.id,
            flow_id=flow_id,
        )
        await edit_screen(
            callback.message,
            result,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Открыть платёж",
                            callback_data=f"admin:payments:card:{payment_id}",
                        ),
                    ],
                    *back_menu_kb("admin:payments").inline_keyboard,
                ]
            ),
        )
        return
    if action == "flow" and len(parts) == 4 and parts[3].isdigit():
        flow = await session.get(Flow, int(parts[3]))
        if flow is None:
            return
        await edit_screen(
            callback.message,
            f"Зачесть платёж #{payment_id} за поток {flow.title}\n"
            f"{format_local_date(flow.start_at)} — "
            f"{format_local_date(flow.end_at)}?\n\n"
            "Доступ выдаётся только после проверки суммы и принадлежности в YooKassa.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Проверить и зачесть",
                            callback_data=f"admin:payments:resolve:{payment_id}:{flow.id}",
                        ),
                    ],
                    *back_menu_kb(f"admin:payments:card:{payment_id}").inline_keyboard,
                ]
            ),
        )
        return
    user = await session.get(User, payment.user_id)
    rows = []
    if payment.status == PaymentStatus.NEEDS_REVIEW:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Проверить в YooKassa",
                    callback_data=f"admin:payments:check:{payment_id}",
                )
            ]
        )
        flows = list(
            (
                await session.execute(
                    select(Flow)
                    .where(
                        Flow.is_free.is_(False),
                        Flow.end_at > datetime.now(timezone.utc),
                    )
                    .order_by(Flow.start_at)
                    .limit(10)
                )
            ).scalars()
        )
        for flow in flows:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=(
                            f"Зачесть за {flow.title} · "
                            f"{format_local_date(flow.start_at)}"
                        ),
                        callback_data=f"admin:payments:flow:{payment_id}:{flow.id}",
                    )
                ]
            )
    rows.extend(back_menu_kb("admin:payments").inline_keyboard)
    await edit_screen(
        callback.message,
        f"Платёж #{payment.id}\nУчастница: {user.tg_id if user else payment.user_id}\n"
        f"Сумма: {payment.amount_rub} {payment.currency}\nСтатус: {payment.status}\n"
        f"Поток: {payment.flow_id or 'не привязан'}\n\n"
        "Если привязка неверна или отсутствует, выберите оплаченный поток. "
        "Само открытие карточки ничего не меняет.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
