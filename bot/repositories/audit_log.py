from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import AuditLog


async def add_audit_log(
    session: AsyncSession,
    action: str,
    payload: dict,
    actor_user_id: int | None = None,
) -> AuditLog:
    entry = AuditLog(action=action, payload=payload, actor_user_id=actor_user_id)
    session.add(entry)
    return entry


async def has_action_with_key(session: AsyncSession, action: str, key: str) -> bool:
    result = await session.execute(
        select(AuditLog.id)
        .where(AuditLog.action == action)
        .where(AuditLog.payload["key"].astext == key)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def list_audit_logs(session: AsyncSession, limit: int = 50) -> list[AuditLog]:
    result = await session.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def get_action_payload(session, action: str, key: str) -> dict | None:
    result = await session.execute(
        select(AuditLog.payload)
        .where(
            AuditLog.action == action,
            AuditLog.payload["key"].astext == key,
        )
        .order_by(AuditLog.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def recent_campaigns(session, limit=10):
    result = await session.execute(
        select(AuditLog)
        .where(AuditLog.action == "custom_mailing_started")
        .order_by(AuditLog.id.desc())
        .limit(limit)
    )
    return list(result.scalars())
