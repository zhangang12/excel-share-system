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


async def scan_balance_due(db: AsyncSession, *, advance_days: int = 14) -> dict:
    """🆕 尾款到期提醒：尾款日期(balance_date)前 advance_days 天起触发，
    每条台账**只提醒一次**（终身去重），推送销售本人 + 抄送销售主管(sales_lead) + 管理层(manager)。

    幂等键 = (biz_type='balance_due', biz_id=ledger.id)：只要历史上推过就跳过
    （区别于逾期提醒的「按天去重」，尾款提醒口径是「只提醒一次即可」，用户 2026-06-17 确认）。
    返回 {scanned, notified}。
    """
    today = datetime.now(_CN_TZ).date()
    # balance_date <= 今天+advance_days 即进入提醒窗（含已逾期）；ISO 字符串可直接字典序比较
    threshold = (today + timedelta(days=advance_days)).isoformat()
    r = await db.execute(
        select(models.SalesLedger).where(
            models.SalesLedger.balance > 0,
            models.SalesLedger.balance_date.isnot(None),
            models.SalesLedger.balance_date != "",
            models.SalesLedger.balance_date <= threshold,
        )
    )
    ledgers = list(r.scalars().all())
    notified = 0
    for led in ledgers:
        # 幂等：该台账历史上是否已推过尾款提醒（任意时间，非当日）→ 只提醒一次
        r = await db.execute(
            select(models.Message.id).where(
                models.Message.biz_type == "balance_due",
                models.Message.biz_id == led.id,
            ).limit(1)
        )
        if r.scalar_one_or_none() is not None:
            continue
        try:
            due = date.fromisoformat(led.balance_date)
        except (ValueError, TypeError):
            continue  # 脏数据(非法日期)跳过，避免误推
        p = led.project
        code = p.code if p else f"#{led.project_id}"
        name = p.name if p else ""
        days = (due - today).days
        when = (f"还有 {days} 天到期" if days > 0
                else ("今天到期" if days == 0 else f"已逾期 {-days} 天"))
        text = (f"【尾款提醒】{code} {name} 尾款 ¥{led.balance:,.0f} 预计 {led.balance_date} 到期"
                f"（{when}），请及时跟进收款。")
        # 销售本人 + 抄送主管/管理层；三条均带同一 biz 键，下次扫描即被去重
        if led.sales_uid:
            await push_message(db, to_user_id=led.sales_uid, kind="wx",
                               text=text, biz_type="balance_due", biz_id=led.id)
        await push_message(db, to_role="sales_lead", kind="warn",
                           text=text, biz_type="balance_due", biz_id=led.id)
        await push_message(db, to_role="manager", kind="warn",
                           text=text, biz_type="balance_due", biz_id=led.id)
        notified += 1

    if notified:
        log.info("[scan_balance_due] 推送 %d 条尾款到期提醒（共扫描 %d）", notified, len(ledgers))
    return {"scanned": len(ledgers), "notified": notified}


async def scan_balance_overdue(db: AsyncSession) -> dict:
    """🆕 盈利改善2·逾期尾款每周升级催办（补上 scan_balance_due「终身只提醒一次」之后的盲区）。

    balance>0 且 balance_date < 今天 的台账：每 7 天催一次，逐级升级——
    第 1 周内 → 销售本人；第 2 周 → +销售主管；第 3 周起 → +管理层。
    幂等：biz_type='balance_overdue'，距最近一次催办不足 7 天则跳过。返回 {scanned, notified}。
    """
    today = datetime.now(_CN_TZ).date()
    r = await db.execute(
        select(models.SalesLedger).where(
            models.SalesLedger.balance > 0,
            models.SalesLedger.balance_date.isnot(None),
            models.SalesLedger.balance_date != "",
            models.SalesLedger.balance_date < today.isoformat(),
        )
    )
    ledgers = list(r.scalars().all())
    notified = 0
    for led in ledgers:
        p = led.project
        if not p or p.is_deleted:
            continue
        try:
            due = date.fromisoformat(led.balance_date)
        except (ValueError, TypeError):
            continue
        over_days = (today - due).days
        if over_days <= 0:
            continue
        # 7 天窗口去重：最近一次周催办距今不足 7 天则跳过
        r2 = await db.execute(
            select(models.Message.created_at).where(
                models.Message.biz_type == "balance_overdue",
                models.Message.biz_id == led.id,
            ).order_by(models.Message.created_at.desc()).limit(1))
        last = r2.scalar_one_or_none()
        if last is not None:
            last_d = date.fromisoformat(_cn_date(last))
            if (today - last_d).days < 7:
                continue
        week = (over_days - 1) // 7 + 1   # 逾期第几周
        text = (f"【尾款催办·第{week}周】{p.code} {p.name} 尾款 ¥{led.balance:,.0f} "
                f"约定 {led.balance_date}，已逾期 {over_days} 天，请立即跟进收款。")
        if led.sales_uid:
            await push_message(db, to_user_id=led.sales_uid, kind="wx",
                               text=text, biz_type="balance_overdue", biz_id=led.id)
        if week >= 2:
            await push_message(db, to_role="sales_lead", kind="warn",
                               text=text + "（升级抄送销售主管）", biz_type="balance_overdue", biz_id=led.id)
        if week >= 3:
            await push_message(db, to_role="manager", kind="warn",
                               text=text + "（升级抄送管理层）", biz_type="balance_overdue", biz_id=led.id)
        notified += 1

    if notified:
        log.info("[scan_balance_overdue] 周催办 %d 条逾期尾款（共扫描 %d）", notified, len(ledgers))
    return {"scanned": len(ledgers), "notified": notified}


async def scan_hr_reminders(db: AsyncSession) -> dict:
    """🆕 人事部一期·到期提醒：合同到期前 30 天(含已过期) / 试用期转正前 7 天。
    每日扫、按员工 7 天窗口去重（同 balance_overdue 模式），推 hr + 管理层。"""
    today = datetime.now(_CN_TZ).date()
    r = await db.execute(select(models.Employee).where(models.Employee.status != "离职"))
    emps = list(r.scalars().all())
    notified = 0
    for e in emps:
        for kind_biz, dt_s, window, label in (
                ("hr_contract", e.contract_end, 30, "合同"),
                ("hr_regular", e.regular_date if e.status == "试用" else None, 7, "试用期转正")):
            if not dt_s:
                continue
            try:
                due = date.fromisoformat(dt_s)
            except (ValueError, TypeError):
                continue
            days = (due - today).days
            if days > window:
                continue
            r2 = await db.execute(
                select(models.Message.created_at).where(
                    models.Message.biz_type == kind_biz,
                    models.Message.biz_id == e.id,
                ).order_by(models.Message.created_at.desc()).limit(1))
            last = r2.scalar_one_or_none()
            if last is not None and (today - date.fromisoformat(_cn_date(last))).days < 7:
                continue
            dept = e.department.name if e.department else "未分部门"
            when = (f"还有 {days} 天" if days > 0 else ("今天" if days == 0 else f"已过 {-days} 天"))
            text = f"【{label}到期提醒】{e.name}（{dept}）{label}日期 {dt_s}（{when}），请及时处理。"
            await push_message(db, to_role="hr", kind="warn", text=text,
                               biz_type=kind_biz, biz_id=e.id)
            await push_message(db, to_role="manager", kind="warn", text=text,
                               biz_type=kind_biz, biz_id=e.id)
            notified += 1
    if notified:
        log.info("[scan_hr_reminders] 推送 %d 条人事到期提醒（共扫描 %d 人）", notified, len(emps))
    return {"scanned": len(emps), "notified": notified}


async def overdue_scheduler(interval_hours: int = 12) -> None:
    """启动期周期任务：每 interval_hours 扫一次（单容器部署用 asyncio 即可）。
    含：逾期任务提醒 + 🆕 尾款到期提醒 + 🆕 逾期尾款周催办（各用独立会话，互不影响）。"""
    while True:
        try:
            async with SessionLocal() as db:
                await scan_overdue(db)
        except Exception as e:  # noqa: BLE001
            log.warning("scan_overdue 失败: %s", e)
        try:
            async with SessionLocal() as db:
                await scan_balance_due(db)
        except Exception as e:  # noqa: BLE001
            log.warning("scan_balance_due 失败: %s", e)
        try:
            async with SessionLocal() as db:
                await scan_balance_overdue(db)
        except Exception as e:  # noqa: BLE001
            log.warning("scan_balance_overdue 失败: %s", e)
        try:
            async with SessionLocal() as db:
                await scan_hr_reminders(db)
        except Exception as e:  # noqa: BLE001
            log.warning("scan_hr_reminders 失败: %s", e)
        await asyncio.sleep(interval_hours * 3600)
