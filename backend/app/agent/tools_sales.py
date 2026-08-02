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

_LIMIT = 20        # 单次最多返回多少条明细；总数另给，不让模型拿截断后的数去算比例


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
            "items": rows[:_LIMIT]}


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

    # 历史收货人：按公司名归集，供前端做候选
    hist_q = select(models.Shipment.receiver_company, models.Shipment.receiver_name,
                    models.Shipment.receiver_phone).where(
        models.Shipment.receiver_name.isnot(None), models.Shipment.receiver_name != "")
    hist: dict[str, dict] = {}
    for comp, name, phone in (await db.execute(hist_q)).all():
        if comp:
            hist.setdefault(comp, {"name": name, "phone": phone})

    rows = [{"id": s.id, "company": s.receiver_company or "—",
             "status": s.status,
             "suggest": hist.get(s.receiver_company or "")} for s in ships]
    return {"count": len(rows), "items": rows[:_LIMIT]}


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
    return {"count": len(rows), "items": rows[:_LIMIT]}


# ────────────────────────── 4. 销售线索待跟进 ──────────────────────────

async def tool_leads_followup(db: AsyncSession, current: models.User) -> dict:
    """还没闭环（既未成交也未放弃）的销售线索。"""
    q = select(models.SalesLead).where(
        models.SalesLead.status.notin_(["已成交", "已放弃"])
    ).order_by(models.SalesLead.id.desc())
    rows = [{"id": x.id, "company": x.company or "—", "status": x.status or "—",
             "age_days": _age_days(x.created_at)}
            for x in (await db.execute(q)).scalars().all()]
    return {"count": len(rows), "items": rows[:_LIMIT]}


# ────────────────────────── 5. 待审批销售订单 ──────────────────────────

async def tool_order_pending(db: AsyncSession, current: models.User) -> dict:
    """销售下单后等销售主管审批的订单（order_state='pending'）。"""
    q = _scope(_live(select(models.SalesLedger).where(
        models.SalesLedger.order_state == "pending")), current)
    rows = [_row(led) | {"amount": led.amount or 0,
                         "age_days": _age_days(led.created_at)}
            for led in (await db.execute(q)).scalars().all()]
    return {"count": len(rows), "items": rows[:_LIMIT]}


# ────────────────────────── 6. 待开票 ──────────────────────────

async def tool_invoice_pending(db: AsyncSession, current: models.User) -> dict:
    """已申请开票、等财务出票的台账行（invoice_state='pending_invoice'）。"""
    q = _scope(_live(select(models.SalesLedger).where(
        models.SalesLedger.invoice_state == "pending_invoice")), current)
    rows = [_row(led) | {"amount": led.amount or 0,
                         "age_days": _age_days(led.created_at)}
            for led in (await db.execute(q)).scalars().all()]
    return {"count": len(rows), "items": rows[:_LIMIT]}
