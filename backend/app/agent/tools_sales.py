"""🆕 销售/台账域的只读工具（第二批）。

为什么补这一批：现有 7 个工具里有 3 个是采购的，而杨坛 2 个月一次采购都没碰过；
他真正的活是销售台账 243 次、请款审批 40 笔、物流收货人 34 次、销售订单 29 次。
门户上摆着他不用的卡，等于没有。

本模块严格遵守手册第三章原则一：**不重写权限谓词**。
行级隔离一律复用 sales_router 的 `_all_view` —— 那是唯一真源，
自己抄一份必然随业务改动漂移。

另外每个查询都要 `Project.is_deleted == False`：软删项目的台账行还留着钱，
不过滤就会把幽灵数据算进来（现网有 28 行、¥40 万发货应收属于此类）。
"""
from datetime import date, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models
from ..routers.sales_router import _all_view          # ← 行级隔离唯一真源，勿重写

# 截断统一由 agent_router._cap 处理（可被 limit 参数调整）；
# 工具本身返回全量，别在这里再截一次，否则 limit=200 也拿不到第 21 条。


def _scope(q, current: models.User):
    """销售行级隔离：非管理层/非销售主管只看本人负责的台账行。口径同 /api/sales/ledger。"""
    if not _all_view(current):
        q = q.where(models.SalesLedger.sales_uid == current.id)
    return q


def _live(q):
    """排除软删项目。漏了这条会把幽灵台账算进来。"""
    return q.join(models.Project,
                  models.Project.id == models.SalesLedger.project_id
                  ).where(models.Project.is_deleted == False)  # noqa: E712


def _row(led: models.SalesLedger) -> dict:
    p = led.project
    return {"id": led.id,
            "project_code": p.code if p else f"#{led.project_id}",
            "customer": led.customer or "—",
            "sales": (led.sales_user.full_name or led.sales_user.username)
                     if led.sales_user else "—"}


def _age_days(d: datetime | None) -> int | None:
    return (date.today() - d.date()).days if d else None


# ────────────────────────── 1. 盯不住的应收 ──────────────────────────

async def tool_receivable_blind(db: AsyncSession, current: models.User) -> dict:
    """现有催办与 balance_due 工具都盯不到的应收。

    两类：
      A. 尾款 > 0 但**没填到期日** —— balance_due 的口径是
         `balance_date IS NOT NULL AND <= 今天+14天`，没填日期的一条都查不到，
         `overdue.scan_balance_due` 同口径，所以也从来不会催。现网 13 笔 ¥27 万。
      B. 发货款应收 > 0 —— `ship_receivable` 这个字段**全系统没有任何工具或提醒碰过**。
         现网 36 笔 ¥226 万，是账面上最大的一笔盲区。
    """
    q = _scope(_live(select(models.SalesLedger).where(
        or_(models.SalesLedger.ship_receivable > 0,
            and_(models.SalesLedger.balance > 0,
                 or_(models.SalesLedger.balance_date.is_(None),
                     models.SalesLedger.balance_date == ""))))), current)
    rows = []
    for led in (await db.execute(q)).scalars().all():
        base = _row(led)
        if (led.ship_receivable or 0) > 0:
            rows.append(base | {"kind": "ship", "kind_cn": "发货款",
                                "amount": led.ship_receivable,
                                "age_days": _age_days(led.created_at)})
        if (led.balance or 0) > 0 and not (led.balance_date or "").strip():
            rows.append(base | {"kind": "balance", "kind_cn": "尾款",
                                "amount": led.balance,
                                "age_days": _age_days(led.created_at)})
    rows.sort(key=lambda r: -(r["amount"] or 0))
    return {"count": len(rows),
            "total": round(sum(r["amount"] or 0 for r in rows), 2),
            "items": rows}


# ────────────────────────── 2. 待填收货人 ──────────────────────────

async def tool_shipment_receiver(db: AsyncSession, current: models.User) -> dict:
    """已建但还没填收货人的发货单。填了才能安排送货与签收。

    收货人常常是同一客户重复出现，所以顺带带出该客户历史上用过的收货人，
    前端可做成候选让人点选，不用在手机上打字。
    """
    q = (select(models.Shipment)
         .where(or_(models.Shipment.receiver_name.is_(None),
                    models.Shipment.receiver_name == ""))
         .order_by(models.Shipment.id.desc()))
    ships = list((await db.execute(q)).scalars().all())

    # ⚠️ 客户名必须沿 project_id 取，**不能**用 shipment.receiver_company ——
    #    那正是「还没填」的那个字段，拿它当客户名的结果是每条都空，
    #    助手于是回「系统里查不到客户信息」，让人以为数据不存在。
    #    实际生产上 49 条待填里 49 条有项目编号、44 条有客户名，全都在，只是没去取。
    pids = {s_.project_id for s_ in ships if s_.project_id}
    proj: dict[int, str] = {}
    cust: dict[int, str] = {}
    if pids:
        for pr in (await db.execute(select(models.Project).where(
                models.Project.id.in_(pids)))).scalars().all():
            proj[pr.id] = pr.code or f"#{pr.id}"
        for led in (await db.execute(select(models.SalesLedger).where(
                models.SalesLedger.project_id.in_(pids)))).scalars().all():
            if led.customer:
                cust[led.project_id] = led.customer

    # 历史收货人：**按客户名**归集，供前端做候选（同一客户往往重复用同一个收货人）。
    # ⚠️ 索引键必须和查找键一致。曾经索引用 receiver_company、查找用客户名 ——
    #    两套键永远对不上，候选功能等于没有。
    filled = list((await db.execute(select(models.Shipment).where(
        models.Shipment.receiver_name.isnot(None),
        models.Shipment.receiver_name != ""))).scalars().all())
    hist_pids = {x.project_id for x in filled if x.project_id}
    hist_cust: dict[int, str] = {}
    if hist_pids:
        for led in (await db.execute(select(models.SalesLedger).where(
                models.SalesLedger.project_id.in_(hist_pids)))).scalars().all():
            if led.customer:
                hist_cust[led.project_id] = led.customer
    hist: dict[str, dict] = {}
    for x in filled:
        key = hist_cust.get(x.project_id) or x.receiver_company
        if key:
            hist.setdefault(key, {"name": x.receiver_name, "phone": x.receiver_phone})

    rows = [{"id": s_.id,
             "project_code": proj.get(s_.project_id, f"#{s_.project_id}"),
             "customer": cust.get(s_.project_id) or "—",
             "status": s_.status,
             # 同一客户的历史收货人，前端可做候选，省得在手机上打字。
             # ⚠️ 查找键要和上面 hist 的索引键**同一套兜底**（客户名优先，退回 receiver_company），
             #    否则两边键不一致，候选永远命中不了。
             "suggest": hist.get(cust.get(s_.project_id) or s_.receiver_company or "")}
            for s_ in ships]
    return {"count": len(rows), "items": rows}


# ────────────────────────── 3. 台账缺件 ──────────────────────────

async def tool_ledger_incomplete(db: AsyncSession, current: models.User) -> dict:
    """台账上关键字段还没填的行。

    合同额为 0 影响最大：项目毛利会被算成**假亏损**，
    这正是问「哪些项目在亏钱」时助手必须拒答的原因。现网 18 行。
    """
    q = _scope(_live(select(models.SalesLedger).where(
        or_(models.SalesLedger.amount.is_(None), models.SalesLedger.amount == 0,
            models.SalesLedger.customer.is_(None), models.SalesLedger.customer == ""))),
        current)
    rows = []
    for led in (await db.execute(q)).scalars().all():
        miss = []
        if not (led.amount or 0):
            miss.append("合同额")
        if not (led.customer or "").strip():
            miss.append("客户")
        rows.append(_row(led) | {"missing": miss})
    return {"count": len(rows), "items": rows}


# ────────────────────────── 4. 销售线索待跟进 ──────────────────────────

# 线索状态枚举来自 models.SalesLead.status 的注释：潜在需求 / 报价 / 成交 / 丢单。
# 别照搬别处的「已成交/已放弃」——那是另一套说法，写错了这个工具会把全部线索都算成未闭环。
_LEAD_CLOSED = ("成交", "丢单")


async def tool_leads_followup(db: AsyncSession, current: models.User) -> dict:
    """还没闭环（既没成交也没丢单）的销售线索。

    行级隔离按 owner_uid：非管理层只看分给自己的。
    """
    q = select(models.SalesLead).where(
        models.SalesLead.status.notin_(_LEAD_CLOSED)
    ).order_by(models.SalesLead.id.desc())
    if not _all_view(current):
        q = q.where(models.SalesLead.owner_uid == current.id)
    rows = [{"id": x.id, "customer": x.customer or "—", "status": x.status or "—",
             "contact": x.contact or "", "age_days": _age_days(x.created_at)}
            for x in (await db.execute(q)).scalars().all()]
    return {"count": len(rows), "items": rows}


# ────────────────────────── 5. 待审批销售订单 ──────────────────────────

async def tool_order_pending(db: AsyncSession, current: models.User) -> dict:
    """销售下单后等销售主管审批的订单（order_state='pending'）。"""
    q = _scope(_live(select(models.SalesLedger).where(
        models.SalesLedger.order_state == "pending")), current)
    rows = [_row(led) | {"amount": led.amount or 0,
                         "age_days": _age_days(led.created_at)}
            for led in (await db.execute(q)).scalars().all()]
    return {"count": len(rows), "items": rows}


# ────────────────────────── 6. 待开票 ──────────────────────────

async def tool_invoice_pending(db: AsyncSession, current: models.User) -> dict:
    """已申请开票、等财务出票的台账行（invoice_state='pending_invoice'）。"""
    q = _scope(_live(select(models.SalesLedger).where(
        models.SalesLedger.invoice_state == "pending_invoice")), current)
    rows = [_row(led) | {"amount": led.amount or 0,
                         "age_days": _age_days(led.created_at)}
            for led in (await db.execute(q)).scalars().all()]
    return {"count": len(rows), "items": rows}
