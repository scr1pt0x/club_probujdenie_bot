import asyncio
import logging
from datetime import datetime, time, timedelta, timezone

from aiogram import Bot
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.admin.templates import DEFAULT_TEMPLATES
from bot.db.models import Flow, Membership, MembershipStatus, User
from bot.repositories import flows as flow_repo
from bot.repositories.audit_log import add_audit_log, has_action_with_key
from bot.repositories.message_templates import get_template_by_key
from bot.services.texts import get_text


logger = logging.getLogger(__name__)


async def _get_active_user_ids(session: AsyncSession, now: datetime) -> list[int]:
    result = await session.execute(
        select(distinct(Membership.user_id))
        .where(Membership.status == MembershipStatus.ACTIVE)
        .where(Membership.access_end_at >= now)
    )
    return [row[0] for row in result.all()]


async def _get_active_flow_user_ids(
    session: AsyncSession, flow_id: int, now: datetime
) -> list[int]:
    result = await session.execute(
        select(distinct(Membership.user_id))
        .where(Membership.status == MembershipStatus.ACTIVE)
        .where(Membership.flow_id == flow_id)
        .where(Membership.access_end_at >= now)
    )
    return [row[0] for row in result.all()]


async def _get_former_user_ids(session: AsyncSession) -> list[int]:
    latest_subq = (
        select(
            Membership.user_id,
            func.max(Membership.created_at).label("max_created"),
        )
        .group_by(Membership.user_id)
        .subquery()
    )
    result = await session.execute(
        select(Membership.user_id)
        .join(
            latest_subq,
            (Membership.user_id == latest_subq.c.user_id)
            & (Membership.created_at == latest_subq.c.max_created),
        )
        .where(Membership.status != MembershipStatus.ACTIVE)
    )
    return [row[0] for row in result.all()]


async def _send_bulk(
    session: AsyncSession,
    bot: Bot,
    user_ids: list[int],
    text: str,
    mailing_key: str | None,
    delay_seconds: float = 0.5,
    idempotent: bool = True,
) -> int:
    if idempotent and mailing_key:
        if await has_action_with_key(session, "mailing_sent", mailing_key):
            return 0

    sent = 0
    for user_id in user_ids:
        result = await session.execute(select(User.tg_id).where(User.id == user_id))
        row = result.first()
        if not row:
            continue
        tg_id = row[0]
        try:
            await bot.send_message(tg_id, text)
            sent += 1
        except Exception:
            # Ошибки Telegram API не должны останавливать рассылку
            pass
        await asyncio.sleep(delay_seconds)

    if idempotent and mailing_key:
        await add_audit_log(session, "mailing_sent", {"key": mailing_key})
    return sent


async def _get_template_text(session: AsyncSession, key: str) -> str:
    template = await get_template_by_key(session, key)
    if template:
        return template.text
    return DEFAULT_TEMPLATES.get(key, "")


async def send_flow_mailings(
    session: AsyncSession, bot: Bot, flow_id: int, flow_start: datetime
) -> None:
    now = datetime.now(flow_start.tzinfo)
    days_before = (flow_start.date() - now.date()).days

    if days_before not in (7, 3):
        return

    active_ids = await _get_active_user_ids(session, now)
    former_ids = await _get_former_user_ids(session)

    active_key = f"flow:{flow_id}:active:{days_before}"
    former_key = f"flow:{flow_id}:former:{days_before}"

    active_template = await get_template_by_key(
        session, f"mailing_active_{days_before}"
    )
    former_template = await get_template_by_key(
        session, f"mailing_former_{days_before}"
    )

    active_text = (
        active_template.text
        if active_template
        else "Скоро новый поток. Продлите участие."
    )
    former_text = (
        former_template.text
        if former_template
        else "Стартует новый поток. Приглашаем присоединиться."
    )

    # Критично: рассылки должны быть идемпотентными и с анти-спам ограничением.
    await _send_bulk(session, bot, active_ids, active_text, active_key)
    await _send_bulk(session, bot, former_ids, former_text, former_key)


async def send_manual_mailings(
    session: AsyncSession, bot: Bot, mode: str
) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    current_ids = await _get_active_user_ids(session, now)
    former_ids = await _get_former_user_ids(session)

    sent_current = 0
    sent_former = 0
    if mode in (
        "free_end_minus_7",
        "free_end_minus_3",
        "paid_end_minus_3",
        "paid_end_minus_1",
    ):
        delta_map = {
            "free_end_minus_7": 7,
            "free_end_minus_3": 3,
            "paid_end_minus_3": 3,
            "paid_end_minus_1": 1,
        }
        target_date = (now.date() + timedelta(days=delta_map[mode]))
        tz = now.tzinfo or timezone.utc
        window_start = datetime.combine(target_date, time.min, tz)
        window_end = datetime.combine(target_date, time.max, tz)
        is_free = mode.startswith("free_")
        result = await session.execute(
            select(Flow).where(
                Flow.is_free.is_(is_free),
                Flow.end_at >= window_start,
                Flow.end_at <= window_end,
            )
        )
        flows = list(result.scalars().all())
        text = await get_text(session, mode)
        for flow in flows:
            user_ids = await _get_active_flow_user_ids(session, flow.id, now)
            if not user_ids:
                continue
            sent_current += await _send_bulk(
                session,
                bot,
                user_ids,
                text,
                mailing_key=None,
                idempotent=False,
            )
        sent_former = 0
        return sent_current, sent_former
    if mode == "minus_7":
        current_text = await _get_template_text(session, "paid_transition_minus_7")
        former_text = await _get_template_text(session, "reminder_minus_7")
        sent_current = await _send_bulk(
            session,
            bot,
            current_ids,
            current_text,
            mailing_key=None,
            idempotent=False,
        )
        sent_former = await _send_bulk(
            session,
            bot,
            former_ids,
            former_text,
            mailing_key=None,
            idempotent=False,
        )
    elif mode == "minus_3":
        all_ids = list({*current_ids, *former_ids})
        text = await _get_template_text(session, "reminder_minus_3")
        sent_current = await _send_bulk(
            session,
            bot,
            all_ids,
            text,
            mailing_key=None,
            idempotent=False,
        )
        sent_former = 0

    logger.info(
        "Manual mailings sent",
        extra={
            "mode": mode,
            "sent_current": sent_current,
            "sent_former": sent_former,
        },
    )
    return sent_current, sent_former


async def send_auto_end_mailings(
    session: AsyncSession, bot: Bot, now: datetime
) -> int:
    today = now.date()
    tz = now.tzinfo or timezone.utc
    window_start = datetime.combine(today - timedelta(days=7), time.min, tz)
    window_end = datetime.combine(today + timedelta(days=1), time.max, tz)
    result = await session.execute(
        select(Flow).where(Flow.end_at >= window_start, Flow.end_at <= window_end)
    )
    flows = list(result.scalars().all())
    total_sent = 0
    for flow in flows:
        end_date = flow.end_at.date()
        template_key: str | None = None
        if flow.is_free:
            if today == end_date - timedelta(days=7):
                template_key = "free_end_minus_7"
            elif today == end_date - timedelta(days=3):
                template_key = "free_end_minus_3"
        else:
            if today == end_date - timedelta(days=3):
                template_key = "paid_end_minus_3"
            elif today == end_date - timedelta(days=1):
                template_key = "paid_end_minus_1"

        if not template_key:
            continue

        key = f"auto:{template_key}:{flow.id}:{today}"
        if await has_action_with_key(session, "mailing_sent", key):
            continue

        user_ids = await _get_active_flow_user_ids(session, flow.id, now)
        if not user_ids:
            await add_audit_log(
                session, action="mailing_sent", payload={"key": key, "count": 0}
            )
            continue

        text = await _get_template_text(session, template_key)
        sent = await _send_bulk(
            session,
            bot,
            user_ids,
            text,
            mailing_key=key,
            idempotent=True,
        )
        total_sent += sent
    return total_sent
