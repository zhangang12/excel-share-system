"""🆕 v3 M15 逾期每日提醒：扫描进行中且超预计完成的任务，推送部门主管 + 抄送管理层。

幂等键 = (order_id, 当日)：同一任务同一天只推一次（防多实例/重复扫描重复发，红线 F2）。
通过站内 messages 去重：若该任务今日已有 biz_type='order_overdue' 的消息则跳过。
启动期挂 asyncio 周期任务；也可由 cron 调 POST /api/internal/overdue-scan。
"""
import asyncio
import logging
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from . import models
from .config import settings

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


async def scan_management_todos(db: AsyncSession) -> dict:
    """🆕 管理层待办·每日提醒：
    - 承诺日已过且未「已完成」→ 每日推逾期提醒给收件人 + 抄送下达人。
    - 一直未回复承诺时间（下发满 1 天仍 pending）→ 每日催回复承诺时间。
    幂等键 =(biz_type='mgmt_todo_remind', biz_id=target.id, 当日)：同一待办同一人一天只推一次。"""
    from sqlalchemy.orm import joinedload
    today = datetime.now(_CN_TZ).date()
    today_s = today.isoformat()
    r = await db.execute(
        select(models.ManagementTodoTarget)
        .options(joinedload(models.ManagementTodoTarget.todo)
                 .joinedload(models.ManagementTodo.creator),
                 joinedload(models.ManagementTodoTarget.user))
        .where(models.ManagementTodoTarget.status != "done"))
    targets = list(r.scalars().all())
    notified = 0
    for t in targets:
        todo = t.todo
        if todo is None:
            continue
        # 逾期 = 未完成且已过截止日；截止日取「管理层 due_date」与「收件人承诺日」最早者(#251)
        overdue = False
        over_ref = None      # 触发逾期的日期（用于文案/天数）
        for d in (t.committed_at if t.status == "committed" else None, todo.due_date):
            if not d:
                continue
            try:
                if date.fromisoformat(d) < today:
                    overdue = True
                    over_ref = d if over_ref is None else min(over_ref, d)
            except (ValueError, TypeError):
                continue
        # 未回复承诺时间：下发满 1 天仍 pending 才开始催（当天不催）；已逾期则不再走待回复
        need_reply = False
        if t.status == "pending" and not overdue:
            created_d = date.fromisoformat(_cn_date(todo.created_at))
            need_reply = (today - created_d).days >= 1
        if not overdue and not need_reply:
            continue
        # 幂等：该待办·该人今日是否已推过
        r2 = await db.execute(
            select(models.Message.created_at).where(
                models.Message.biz_type == "mgmt_todo_remind",
                models.Message.biz_id == t.id,
            ).order_by(models.Message.created_at.desc()).limit(1))
        last = r2.scalar_one_or_none()
        if last is not None and _cn_date(last) == today_s:
            continue

        title = todo.title
        if overdue:
            over_days = (today - date.fromisoformat(over_ref)).days
            kind_txt = "承诺" if over_ref == t.committed_at else "要求"
            text = (f"【待办逾期】你的待办「{title}」{kind_txt} {over_ref} 完成，"
                    f"已逾期 {over_days} 天，请尽快完成并点「已完成」，或申请顺延。")
        else:
            text = f"【待办待回复】你有一条待办「{title}」尚未回复承诺完成时间，请尽快回复。"
        # biz_id 用 target.id 做当日去重键（与「按 order/ledger 去重」同理）
        await push_message(db, to_user_id=t.user_id, kind="warn", text=text,
                           biz_type="mgmt_todo_remind", biz_id=t.id)
        # 抄送下达人（逾期才抄送，避免未回复也打扰管理层）
        if overdue and todo.created_by:
            cc = (f"【待办逾期·抄送】{(t.user.full_name or t.user.username) if t.user else '收件人'} "
                  f"的待办「{title}」承诺 {t.committed_at}，已逾期。")
            await push_message(db, to_user_id=todo.created_by, kind="warn", text=cc,
                               biz_type="mgmt_todo_remind", biz_id=t.id)
        notified += 1

    if notified:
        log.info("[scan_management_todos] 推送 %d 条管理层待办提醒（共扫描 %d）", notified, len(targets))
    return {"scanned": len(targets), "notified": notified}


async def scan_po_arrival_overdue(db: AsyncSession) -> dict:
    """🆕 采购到期未到货每日提醒：预计到货日期(expected_arrival)已到(含当天)、仍未收货(arrival_date 为空)
    的采购明细，每天推一次提醒——该明细的采购员(buyer_id) + 采购主管(buyer_lead) + 全体管理层(admin/manager)。

    幂等键 =(biz_type='po_arrival_overdue', biz_id=purchase_item.id, 当日)：同一条同一天只推一次；
    货到(填了到货日期)即不再命中扫描、自动停推；预计到货留空的明细不提醒。返回 {scanned, notified}。
    """
    today = datetime.now(_CN_TZ).date()
    today_s = today.isoformat()
    r = await db.execute(
        select(models.PurchaseItem).where(
            models.PurchaseItem.expected_arrival.isnot(None),
            models.PurchaseItem.expected_arrival != "",
            models.PurchaseItem.expected_arrival <= today_s,   # 到货日当天仍未收货即开始提醒
            or_(models.PurchaseItem.arrival_date.is_(None),
                models.PurchaseItem.arrival_date == ""),
        )
    )
    items = list(r.scalars().all())
    notified = 0
    for it in items:
        # 幂等：该明细今日是否已推过（查 messages biz_type=po_arrival_overdue 当日）
        r2 = await db.execute(
            select(models.Message.created_at).where(
                models.Message.biz_type == "po_arrival_overdue",
                models.Message.biz_id == it.id,
            )
        )
        if any(ts and _cn_date(ts) == today_s for (ts,) in r2.all()):
            continue
        try:
            over_days = (today - date.fromisoformat(it.expected_arrival)).days
        except (ValueError, TypeError):
            continue  # 脏数据(非法日期)跳过，避免误推
        sup = it.supplier.name if it.supplier else "—"
        po = f"采购单 {it.po_no} " if it.po_no else ""
        if over_days <= 0:
            text = (f"【未到货提醒】{po}{it.item_name}（供应商：{sup}）预计今天（{it.expected_arrival}）到货，"
                    f"目前仍未到货，请尽快跟进。")
        else:
            text = (f"【未到货提醒】{po}{it.item_name}（供应商：{sup}）预计 {it.expected_arrival} 到货，"
                    f"已超期 {over_days} 天仍未到货，请尽快跟进。")
        # 采购员本人（无归属采购员则只推主管/管理层）+ 采购主管 + 全体管理层(admin/manager)；
        # 角色扇出排除采购员本人，避免其兼有主管/管理角色时同日收到两条相同文本
        excl = {it.buyer_id} if it.buyer_id else None
        if it.buyer_id:
            await push_message(db, to_user_id=it.buyer_id, kind="warn",
                               text=text, biz_type="po_arrival_overdue", biz_id=it.id)
        await push_message(db, to_role="buyer_lead", kind="warn",
                           text=text, biz_type="po_arrival_overdue", biz_id=it.id,
                           exclude_user_ids=excl)
        await push_message(db, to_role="manager", kind="warn",
                           text=text, biz_type="po_arrival_overdue", biz_id=it.id,
                           exclude_user_ids=excl)
        await push_message(db, to_role="admin", kind="warn",
                           text=text, biz_type="po_arrival_overdue", biz_id=it.id,
                           exclude_user_ids=excl)
        notified += 1

    if notified:
        log.info("[scan_po_arrival_overdue] 推送 %d 条到期未到货提醒（共扫描 %d）", notified, len(items))
    return {"scanned": len(items), "notified": notified}


def _try_acquire_scheduler_lock():
    """多 worker(uvicorn --workers N)部署时用 flock 保证只有一个进程跑周期扫描：
    否则 N 个 worker 同时醒来会同时通过 messages 表的当日去重检查(check-then-act 竞态)，
    同一明细同一天被重复推送 N 份。进程退出锁自动释放；拿不到锁的进程不跑 scheduler，
    其 HTTP 服务不受影响。返回文件句柄(保持锁)或 None(锁被占用)。"""
    try:
        import fcntl   # Unix only；Windows 开发环境退化为不锁（单进程运行无所谓）
    except ImportError:
        return "no-lock"
    lock_dir = Path(settings.files_dir).resolve()
    lock_dir.mkdir(parents=True, exist_ok=True)
    fh = open(lock_dir / ".overdue_scheduler.lock", "w")  # noqa: SIM115 句柄需随进程存活
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


async def overdue_scheduler(interval_hours: int = 12) -> None:
    """启动期周期任务：每 interval_hours 扫一次（多 worker 部署由 flock 保证单实例运行）。
    含：逾期任务提醒 + 尾款到期提醒 + 逾期尾款周催办 + 人事到期提醒
    + 管理层待办逾期/待回复提醒 + 🆕 采购到期未到货提醒（各用独立会话，互不影响）。"""
    _lock = _try_acquire_scheduler_lock()
    if _lock is None:
        log.info("[overdue_scheduler] 另一进程已在运行周期扫描，本 worker 跳过（防多实例重复推送）")
        return
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
        try:
            async with SessionLocal() as db:
                await scan_management_todos(db)
        except Exception as e:  # noqa: BLE001
            log.warning("scan_management_todos 失败: %s", e)
        try:
            async with SessionLocal() as db:
                await scan_po_arrival_overdue(db)
        except Exception as e:  # noqa: BLE001
            log.warning("scan_po_arrival_overdue 失败: %s", e)
        await asyncio.sleep(interval_hours * 3600)
