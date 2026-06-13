"""🆕 v3 M15 逾期每日提醒：扫描进行中且超预计完成的任务，推送部门主管 + 抄送管理层。

幂等键 = (order_id, 当日)：同一任务同一天只推一次（防多实例/重复扫描重复发，红线 F2）。
通过站内 messages 去重：若该任务今日已有 biz_type='order_overdue' 的消息则跳过。
启动期挂 asyncio 周期任务；也可由 cron 调 POST /api/internal/overdue-scan。
"""
import asyncio
import logging
from datetime import date, datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import models

# 业务时区(中国 UTC+8)；messages.created_at 由 func.now() 落 UTC。
# 幂等与逾期判定统一用业务自然日，避免 UTC/本地日界错位导致重复推送(#78)。
_CN_TZ = timezone(timedelta(hours=8))


def _cn_date(ts: datetime) -> str:
    """把存储的 created_at(UTC；SQLite 为 naive、PG 为 aware) 归一到业务(中国)自然日。"""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(_CN_TZ).date().isoformat()
from .database import SessionLocal
from .dept_config import DEPTS
from .notify import push_message

log = logging.getLogger("overdue")


async def scan_overdue(db: AsyncSession) -> dict:
    """扫描逾期任务并推送（幂等）。返回 {scanned, notified}。"""
    today_s = datetime.now(_CN_TZ).date().isoformat()  # 业务(中国)今天
    r = await db.execute(
        select(models.DeptOrder).where(
            models.DeptOrder.status == "in_progress",
            models.DeptOrder.due_date.isnot(None),
            models.DeptOrder.due_date < today_s,
        )
    )
    orders = list(r.scalars().all())
    notified = 0
    for o in orders:
        # 幂等：该任务今日是否已推过逾期提醒（查 messages biz_type=order_overdue 当日）
        r = await db.execute(
            select(models.Message.created_at).where(
                models.Message.biz_type == "order_overdue",
                models.Message.biz_id == o.id,
            )
        )
        already_today = any(
            ts and _cn_date(ts) == today_s for (ts,) in r.all()
        )
        if already_today:
            continue

        cfg = DEPTS.get(o.dept)
        if not cfg:
            continue
        over_days = (date.fromisoformat(today_s) - date.fromisoformat(o.due_date)).days
        wname = (o.worker.full_name or o.worker.username) if o.worker else "—"
        code = o.project.code if o.project else f"#{o.project_id}"
        text = (f"【逾期提醒】{cfg['name']} {code} 预计 {o.due_date} 应完成，"
                f"已逾期 {over_days} 天（负责人：{wname}），请尽快处理。")
        await push_message(db, to_role=cfg["lead_role"], kind="warn",
                           text=text, biz_type="order_overdue", biz_id=o.id)
        await push_message(db, to_role="manager", kind="warn",
                           text=text, biz_type="order_overdue", biz_id=o.id)
        notified += 1

    if notified:
        log.info("[scan_overdue] 推送 %d 个逾期任务提醒（共扫描 %d）", notified, len(orders))
    return {"scanned": len(orders), "notified": notified}


async def overdue_scheduler(interval_hours: int = 12) -> None:
    """启动期周期任务：每 interval_hours 扫一次（单容器部署用 asyncio 即可）。"""
    while True:
        try:
            async with SessionLocal() as db:
                await scan_overdue(db)
        except Exception as e:  # noqa: BLE001
            log.warning("overdue_scheduler 失败: %s", e)
        await asyncio.sleep(interval_hours * 3600)
