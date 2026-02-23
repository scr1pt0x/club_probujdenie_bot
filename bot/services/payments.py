from datetime import datetime, timezone
import logging

from aiogram import Bot, types
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Payment, PaymentStatus
from bot.repositories import flows as flow_repo
from bot.repositories import memberships as membership_repo
from bot.services import memberships as membership_service
from bot.services.promos import apply_promo_to_price
from bot.services.settings import get_effective_settings
from config import settings
from bot.access_control.service import grant_access
from bot.repositories import users as user_repo


logger = logging.getLogger(__name__)


def _access_links_kb(
    channel_link: str | None, group_link: str | None
) -> types.InlineKeyboardMarkup | None:
    rows: list[list[types.InlineKeyboardButton]] = []
    if channel_link:
        rows.append(
            [types.InlineKeyboardButton(text="📢 Войти в канал", url=channel_link)]
        )
    if group_link:
        rows.append(
            [types.InlineKeyboardButton(text="💬 Войти в группу", url=group_link)]
        )
    if not rows:
        return None
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def calculate_price_rub(
    session: AsyncSession, user_id: int, paid_at: datetime
) -> int:
    active_membership = await membership_repo.get_active_membership(session, user_id)
    effective = await get_effective_settings(session)
    if active_membership and membership_service.is_within_grace(
        active_membership, paid_at, effective.grace_days
    ):
        base_price = effective.renewal_price_rub
    else:
        base_price = effective.intro_price_rub
    return await apply_promo_to_price(session, user_id, base_price)


async def resolve_flow_for_payment(
    session: AsyncSession, paid_at: datetime
) -> int | None:
    flow = await flow_repo.get_flow_in_sales_window(session, paid_at)
    if flow is None:
        flow = await flow_repo.get_active_paid_flow(session, paid_at)
    return flow.id if flow else None


async def resolve_early_full_payment_flow(
    session: AsyncSession, payment: Payment, paid_at: datetime
) -> int | None:
    effective = await get_effective_settings(session)
    if payment.amount_rub != effective.intro_price_rub:
        return None

    next_free_flow = await flow_repo.get_next_free_flow(session, paid_at)
    if next_free_flow is None:
        return None
    if paid_at >= next_free_flow.start_at:
        return None

    next_paid_flow = await flow_repo.get_next_paid_flow(session, paid_at)
    return next_paid_flow.id if next_paid_flow else None


async def confirm_payment(
    session: AsyncSession, bot: Bot, payment: Payment, paid_at: datetime | None = None
) -> None:
    # Важно: commit выполняется вызывающим кодом.
    if payment.status == PaymentStatus.PAID:
        return

    paid_at = paid_at or datetime.now(timezone.utc)
    early_flow_id = await resolve_early_full_payment_flow(session, payment, paid_at)
    flow_id = payment.flow_id or early_flow_id or await resolve_flow_for_payment(
        session, paid_at
    )
    if flow_id is None:
        # Критично: нельзя помечать PAID без привязки к потоку.
        payment.status = PaymentStatus.NEEDS_REVIEW
        logger.error(
            "Payment needs review: no flow matched",
            extra={"payment_id": payment.id, "external_id": payment.external_id},
        )
        return

    payment.status = PaymentStatus.PAID
    payment.paid_at = paid_at
    payment.flow_id = flow_id

    flow = await flow_repo.get_flow_by_id(session, flow_id)
    if flow is None:
        payment.status = PaymentStatus.NEEDS_REVIEW
        logger.error(
            "Payment needs review: flow not found",
            extra={"payment_id": payment.id, "flow_id": flow_id},
        )
        return
    access_start_at = paid_at if early_flow_id else flow.start_at
    membership = await membership_service.upsert_membership_for_flow(
        session=session,
        user_id=payment.user_id,
        flow_id=flow_id,
        access_start_at=access_start_at,
        access_end_at=flow.end_at,
        payment=payment,
    )

    user = await user_repo.get_user_by_id(session, payment.user_id)
    if user:
        links = await grant_access(bot, user.tg_id)
        kb = _access_links_kb(links.get("channel_link"), links.get("group_link"))
        if kb is not None:
            try:
                await bot.send_message(
                    user.tg_id,
                    "Оплата подтверждена. Нажмите кнопки и отправьте заявку на вступление.",
                    reply_markup=kb,
                )
            except Exception:
                logger.exception(
                    "Failed to send access links after payment",
                    extra={"user_id": payment.user_id, "payment_id": payment.id},
                )
    if membership.pay_later_deadline_at:
        membership.pay_later_deadline_at = None
        membership.pay_later_used_at = None


async def manual_confirm_payment(
    session: AsyncSession,
    bot: Bot,
    payment: Payment,
    flow_id: int,
    paid_at: datetime | None = None,
) -> None:
    paid_at = paid_at or datetime.now(timezone.utc)
    flow = await flow_repo.get_flow_by_id(session, flow_id)
    if flow is None:
        payment.status = PaymentStatus.NEEDS_REVIEW
        logger.error(
            "Manual confirm failed: flow not found",
            extra={"payment_id": payment.id, "flow_id": flow_id},
        )
        return

    payment.status = PaymentStatus.PAID
    payment.paid_at = paid_at
    payment.flow_id = flow_id

    membership = await membership_service.upsert_membership_for_flow(
        session=session,
        user_id=payment.user_id,
        flow_id=flow_id,
        access_start_at=flow.start_at,
        access_end_at=flow.end_at,
        payment=payment,
    )

    user = await user_repo.get_user_by_id(session, payment.user_id)
    if user:
        links = await grant_access(bot, user.tg_id)
        kb = _access_links_kb(links.get("channel_link"), links.get("group_link"))
        if kb is not None:
            try:
                await bot.send_message(
                    user.tg_id,
                    "Оплата подтверждена. Нажмите кнопки и отправьте заявку на вступление.",
                    reply_markup=kb,
                )
            except Exception:
                logger.exception(
                    "Failed to send access links after manual payment",
                    extra={"user_id": payment.user_id, "payment_id": payment.id},
                )
    if membership.pay_later_deadline_at:
        membership.pay_later_deadline_at = None
        membership.pay_later_used_at = None
