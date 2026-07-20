"""🆕 桌面客户端在线统计查询（admin/manager 专属，只读）。

- GET /api/admin/desktop-clients → { distribution: [{version, count}], items: [...] }
  - distribution 按版本聚合计数（在线台数）
  - items 按 last_seen 倒序（最近在线在前）
- 数据由 main.py 的统计中间件按 X-PMS-Client/X-PMS-Device/X-PMS-User 请求头 upsert（60s 节流）
- 只读红线：本模块仅 SELECT，不提供任何写操作接口
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models
from ..database import get_db
from ..deps import require_admin_or_manager

log = logging.getLogger("desktop")

router = APIRouter(prefix="/api/admin", tags=["桌面端统计"])


@router.get("/desktop-clients")
async def list_desktop_clients(
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """在线版本分布 + 设备明细（admin/manager 专属）。"""
    dist_rows = (await db.execute(
        select(models.DesktopClient.version, func.count().label("count"))
        .group_by(models.DesktopClient.version)
        .order_by(func.count().desc(), models.DesktopClient.version)
    )).all()
    items = (await db.execute(
        select(models.DesktopClient)
        .order_by(models.DesktopClient.last_seen.desc())
    )).scalars().all()
    return {
        "distribution": [{"version": v, "count": c} for v, c in dist_rows],
        "items": [{
            "device_id": it.device_id,
            "version": it.version,
            "username": it.username,
            "last_seen": it.last_seen.isoformat() if it.last_seen else None,
        } for it in items],
    }
