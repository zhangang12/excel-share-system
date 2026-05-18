"""通用工具：审计日志写入"""
import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from . import models

log = logging.getLogger("audit")


async def write_audit(
    db: AsyncSession,
    *,
    user: Optional[models.User] = None,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    detail: Optional[str] = None,
    ip: Optional[str] = None,
) -> None:
    """异步写入审计日志，失败不抛"""
    try:
        rec = models.AuditLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
            ip=ip,
        )
        db.add(rec)
        await db.commit()
    except Exception as e:
        log.warning("write_audit failed: %s", e)
