"""🆕 桌面客户端统计查询 + 故障自动上报。

- GET  /api/admin/desktop-clients → { distribution: [{version, count}], items: [...] }
  - distribution 按版本聚合计数（在线台数）
  - items 按 last_seen 倒序（最近在线在前）
  - 数据由 main.py 的统计中间件按 X-PMS-Client/X-PMS-Device/X-PMS-User 请求头 upsert（60s 节流）
- GET  /api/admin/desktop-reports → 故障上报列表（admin/manager）
- POST /api/admin/desktop-reports/{rid}/handled → 标记已处理
- POST /api/desktop/report → **客户端上报入口，不要求认证**（理由见函数注释）
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models
from ..database import get_db
from ..deps import require_admin_or_manager

log = logging.getLogger("desktop")

router = APIRouter(prefix="/api/admin", tags=["桌面端统计"])
# 上报入口独立前缀：不在 /api/admin 下，也不挂任何鉴权依赖
report_router = APIRouter(prefix="/api/desktop", tags=["桌面端上报"])

_DETAIL_MAX = 64 * 1024        # 单条正文上限，超出截断（只留尾部，崩溃现场在尾部）
_PER_DEVICE_PER_DAY = 20       # 每设备每天最多存这么多条，超了直接丢弃
_KINDS = {"update_failed", "crash", "error"}


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


@router.get("/desktop-reports")
async def list_desktop_reports(
    kind: Optional[str] = Query(None),
    only_open: bool = Query(False),
    limit: int = Query(200, ge=1, le=1000),
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """故障上报列表（admin/manager 专属）。默认最近 200 条。"""
    q = select(models.DesktopReport).order_by(models.DesktopReport.id.desc())
    if kind:
        q = q.where(models.DesktopReport.kind == kind)
    if only_open:
        q = q.where(models.DesktopReport.handled == False)  # noqa: E712
    rows = (await db.execute(q.limit(limit))).scalars().all()
    open_cnt = (await db.execute(
        select(func.count()).select_from(models.DesktopReport)
        .where(models.DesktopReport.handled == False)  # noqa: E712
    )).scalar() or 0
    return {
        "open_count": open_cnt,
        "items": [{
            "id": r.id,
            "device_id": r.device_id,
            "version": r.version,
            "kind": r.kind,
            "detail": r.detail,
            "extra": r.extra,
            "username": r.username,
            "handled": r.handled,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
    }


@router.post("/desktop-reports/{rid}/handled")
async def mark_report_handled(
    rid: int,
    handled: bool = Query(True),
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    r = (await db.execute(select(models.DesktopReport)
                          .where(models.DesktopReport.id == rid))).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "上报记录不存在")
    r.handled = handled
    await db.commit()
    return {"message": "已标记" if handled else "已取消标记"}


class DesktopReportIn(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=32)
    kind: str = Field(min_length=1, max_length=32)
    detail: Optional[str] = None
    extra: Optional[dict] = None


@report_router.post("/report")
async def submit_desktop_report(body: DesktopReportIn, db: AsyncSession = Depends(get_db)):
    """客户端故障上报。**故意不要求认证**——最需要抓的场景（升级失败、启动崩溃）
    发生在用户登录之前，挂上鉴权就永远收不到，功能等于白做。

    防滥用三道：
      1. kind 白名单，不认识的一律拒；
      2. detail 截断 64KB（只留尾部）；
      3. 每 device_id 每天最多 20 条，超出直接丢弃——返回 200 而不是 429，
         不给探测者「限流阈值在哪」的反馈。
    只存文本：不解析、不执行、不回显给其它客户端。"""
    if body.kind not in _KINDS:
        raise HTTPException(400, "未知的上报类型")

    since = datetime.now(timezone.utc) - timedelta(days=1)
    n = (await db.execute(
        select(func.count()).select_from(models.DesktopReport)
        .where(models.DesktopReport.device_id == body.device_id,
               models.DesktopReport.created_at >= since)
    )).scalar() or 0
    if n >= _PER_DEVICE_PER_DAY:
        log.warning("桌面上报限流：device=%s 24h 内已 %d 条，丢弃", body.device_id[:12], n)
        return {"message": "ok"}

    # 用 device_id 反查台账带出用户名，纯展示用（上报时客户端可能还没登录）
    uname = (await db.execute(
        select(models.DesktopClient.username)
        .where(models.DesktopClient.device_id == body.device_id)
    )).scalar_one_or_none()

    db.add(models.DesktopReport(
        device_id=body.device_id,
        version=body.version,
        kind=body.kind,
        detail=(body.detail or "")[-_DETAIL_MAX:] or None,
        extra=body.extra,
        username=uname,
    ))
    await db.commit()
    log.info("桌面故障上报：%s device=%s version=%s", body.kind, body.device_id[:12], body.version)
    return {"message": "ok"}
