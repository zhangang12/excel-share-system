"""实体解析与纵深查询 —— 智能体 v2 阶段一（见 docs/agent-architecture-v2.md）。

为什么需要这一层
----------------
v1 的 13 个工具里 12 个是同一个形状：「列一类」。后果是 `for _ in range(4)`
的多轮工具循环**实际永远只跑 1 轮** —— 第一轮把那类列完就没有下一步可走。
**没有 `find → get → agg` 的递进，ReAct 就无事可做。**

这个模块补上缺的两头：
  find_entity  模糊词 → 实体（「南京那个」「迈克斯」「诺朋」都能落到 id）
  get_*        单实体全景（一次给全，而不是让模型分五次去列五个清单）

服务对象（拿生产 30 天审计日志定的，不是拍脑袋）
------------------------------------------------
  杨坛   188 次操作：销售台账+订单 62 次(32%)、收货人 12、派活 8、请款审批 4
         → 需要 get_customer / get_project
  赵仁辉 635 次操作：**仓库 230 次(36%)**、物料字典 170 次、派活 43、采购开票 18
         → 需要 get_material；助手原来对他的覆盖率只有 5%

口径纪律（手册第三章原则一：单一访问路径）
------------------------------------------
行级隔离一律复用 `sales_router._all_view`，**不在这里重写任何权限谓词**。
`Project.is_deleted == False` 每个查询都要带 —— 生产上有 28 行幽灵数据。
"""
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models
from ..routers.sales_router import _all_view

_FIND_MAX = 8          # 候选给太多，模型反而挑不定
_DETAIL_MAX = 30       # 单实体纵深里每一类明细的上限


def _age(d: datetime | None) -> int | None:
    if not d:
        return None
    ts = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return max(0, (date.today() - ts.astimezone(timezone.utc).date()).days)


def _over_days(expected: str | None) -> int | None:
    """预计到货日到今天差几天（正数=已超期）。日期坏就返回 None，不猜。"""
    e = (expected or "").strip()
    if not e:
        return None
    try:
        return (date.today() - date.fromisoformat(e)).days
    except ValueError:
        return None


def _live(q):
    """挂上「项目未被软删」。生产上漏这个会多算 28 行幽灵数据。"""
    return q.join(models.Project, models.Project.id == models.SalesLedger.project_id
                  ).where(models.Project.is_deleted == False)  # noqa: E712


# ══════════════════════════════ find ══════════════════════════════

async def find_entity(db: AsyncSession, current: models.User, q: str,
                      kind: str | None = None) -> dict:
    """模糊词 → 候选实体。这是「南京那个项目」能被理解的前提。

    kind 可限定 project/customer/supplier/material；不给就四类都找。
    **只返回候选，不猜**——同名多个时让模型/用户挑，猜错比问一句代价大得多。
    """
    text = (q or "").strip()
    if not text:
        return {"error": "要找什么？给个名字或编号"}
    like = f"%{text}%"
    out: dict[str, list] = {}

    if kind in (None, "project"):
        rows = (await db.execute(
            select(models.Project)
            .where(models.Project.is_deleted == False,  # noqa: E712
                   or_(models.Project.code.ilike(like), models.Project.name.ilike(like)))
            .order_by(models.Project.id.desc()).limit(_FIND_MAX))).scalars().all()
        out["project"] = [{"id": p.id, "code": p.code, "name": p.name} for p in rows]

    if kind in (None, "customer"):
        # 客户在台账上是自由文本，没有独立主数据表 —— 只能去重取名
        rows = (await db.execute(
            _live(select(models.SalesLedger.customer, func.count().label("n"))
                  .where(models.SalesLedger.customer.ilike(like),
                         models.SalesLedger.customer != ""))
            .group_by(models.SalesLedger.customer)
            .order_by(func.count().desc()).limit(_FIND_MAX))).all()
        out["customer"] = [{"name": r[0], "ledger_rows": r[1]} for r in rows]

    if kind in (None, "supplier"):
        rows = (await db.execute(
            select(models.Supplier).where(models.Supplier.name.ilike(like))
            .limit(_FIND_MAX))).scalars().all()
        out["supplier"] = [{"id": s.id, "name": s.name} for s in rows]

    if kind in (None, "material"):
        rows = (await db.execute(
            select(models.WhMaterial)
            .where(or_(models.WhMaterial.code.ilike(like),
                       models.WhMaterial.name.ilike(like),
                       models.WhMaterial.spec.ilike(like)))
            .limit(_FIND_MAX))).scalars().all()
        out["material"] = [{"id": m.id, "code": m.code, "name": m.name,
                            "spec": m.spec} for m in rows]

    hits = {k: v for k, v in out.items() if v}
    total = sum(len(v) for v in hits.values())
    return {"query": text, "total": total, "matches": hits,
            "hint": "一个都没找到，换个说法或直接给编号" if not total else None}


# ══════════════════════════════ get ══════════════════════════════

async def get_customer(db: AsyncSession, current: models.User, name: str) -> dict:
    """客户全景：这家客户的所有台账 + 回款到哪一步 + 有没有在拖。

    杨坛 32% 的操作在销售台账，这是他最需要「一次看全」的实体。
    ⚠️ 账龄只说「台账建了 N 天」——`ship_date` 生产上 0 条有值、
       `shipments.shipped_at` 90 条只填了 3 条，说「逾期 N 天」就是编。
    """
    nm = (name or "").strip()
    if not nm:
        return {"error": "要查哪个客户？"}
    q = _live(select(models.SalesLedger).where(models.SalesLedger.customer.ilike(f"%{nm}%")))
    if not _all_view(current):
        q = q.where(models.SalesLedger.sales_uid == current.id)
    leds = list((await db.execute(q)).scalars().all())
    if not leds:
        return {"customer": nm, "found": False,
                "hint": "没有这个客户的台账；可能是名字对不上，先用 find_entity 找"}

    pids = {l.project_id for l in leds}
    projs = {p.id: p for p in (await db.execute(select(models.Project).where(
        models.Project.id.in_(pids)))).scalars().all()}
    ships: dict[int, str] = {}
    for sh in (await db.execute(select(models.Shipment).where(
            models.Shipment.project_id.in_(pids))
            .order_by(models.Shipment.project_id, models.Shipment.id.desc()))).scalars().all():
        ships.setdefault(sh.project_id, sh.status or "")

    rows, unpaid, contract = [], 0.0, 0.0
    for l in leds:
        p = projs.get(l.project_id)
        due = float(l.ship_receivable or 0) + float(l.balance or 0)
        unpaid += due
        contract += float(l.amount or 0)
        rows.append({
            "project_code": p.code if p else f"#{l.project_id}",
            "project_name": (p.name if p else "") or "",
            "contract": float(l.amount or 0),
            "prepay": float(l.prepay or 0), "prepay_paid": bool(l.prepay_note),
            "before_ship": float(l.before_ship or 0), "before_ship_paid": bool(l.before_ship_note),
            "ship_receivable": float(l.ship_receivable or 0),
            "balance": float(l.balance or 0),
            "balance_date": l.balance_date or "",
            "ship_status": ships.get(l.project_id),
            "order_state": l.order_state,
            "ledger_age_days": _age(l.created_at),
        })
    rows.sort(key=lambda r: -(r["ship_receivable"] + r["balance"]))

    no_due = [r for r in rows if r["balance"] > 0 and not r["balance_date"].strip()]
    pending_ship = [r for r in rows if r["ship_receivable"] > 0 and r["ship_status"] == "pending"]
    return {
        "customer": nm, "found": True,
        "ledger_count": len(rows),
        "contract_total": round(contract, 2),
        "unpaid_total": round(unpaid, 2),
        "paid_ratio": round((contract - unpaid) / contract, 3) if contract else None,
        # 这两条是「该不该催」的判断依据，不是装饰
        "balance_without_due_date": len(no_due),
        "ship_receivable_but_not_shipped": len(pending_ship),
        "items": rows[:_DETAIL_MAX],
        "count": len(rows),
    }


async def get_project(db: AsyncSession, current: models.User, code: str) -> dict:
    """项目全景：台账 + 各部门任务 + 采购在途 + 发货 + 附件，一次给全。

    比 v1 的 project_status 多给：台账收款分解、发货状态、采购未到货明细。
    """
    c = (code or "").strip()
    if not c:
        return {"error": "要查哪个项目？给编号"}
    p = (await db.execute(select(models.Project).where(
        models.Project.is_deleted == False,  # noqa: E712
        or_(models.Project.code.ilike(f"%{c}%"), models.Project.name.ilike(f"%{c}%")))
        .order_by(models.Project.id.desc()).limit(1))).scalar_one_or_none()
    if not p:
        return {"project": c, "found": False, "hint": "查无此项目，先用 find_entity 找"}

    led = (await db.execute(select(models.SalesLedger).where(
        models.SalesLedger.project_id == p.id))).scalar_one_or_none()
    orders = list((await db.execute(select(models.DeptOrder).where(
        models.DeptOrder.project_id == p.id))).scalars().all())
    # ⚠️ PurchaseItem 上没有 project_id，关联字段是 **project_code**（字符串）。
    #    「未到货」的口径照抄 agent_router._po_arrival_overdue_rows：
    #    有预计到货日 且 arrival_date 为空 = 还没收货。别自创第二套口径。
    pos = list((await db.execute(select(models.PurchaseItem).where(
        models.PurchaseItem.project_code == p.code))).scalars().all())
    sh = (await db.execute(select(models.Shipment).where(
        models.Shipment.project_id == p.id)
        .order_by(models.Shipment.id.desc()).limit(1))).scalar_one_or_none()

    today = date.today().isoformat()
    return {
        "project": p.code, "name": p.name, "found": True,
        "ledger": None if not led else {
            "customer": led.customer, "contract": float(led.amount or 0),
            "prepay": float(led.prepay or 0), "before_ship": float(led.before_ship or 0),
            "ship_receivable": float(led.ship_receivable or 0),
            "balance": float(led.balance or 0), "balance_date": led.balance_date or "",
            "order_state": led.order_state,
        },
        "dept_orders": [{"dept": o.dept, "status": o.status, "due_date": o.due_date,
                         "overdue": bool(o.status == "in_progress" and o.due_date
                                         and o.due_date < today)}
                        for o in orders],
        "dept_overdue_count": sum(1 for o in orders if o.status == "in_progress"
                                  and o.due_date and o.due_date < today),
        "purchase_pending": [{"name": i.item_name, "spec": i.spec,
                              "supplier_id": i.supplier_id, "po_no": i.po_no,
                              "expected_arrival": i.expected_arrival,
                              "over_days": _over_days(i.expected_arrival)}
                             for i in pos if not (i.arrival_date or "").strip()][:_DETAIL_MAX],
        "purchase_overdue_count": sum(1 for i in pos
                                      if not (i.arrival_date or "").strip()
                                      and (_over_days(i.expected_arrival) or -1) >= 0),
        "shipment_status": sh.status if sh else None,
        "shipment_receiver": (sh.receiver_name or "") if sh else None,
    }


async def get_supplier(db: AsyncSession, current: models.User, name: str) -> dict:
    """供应商画像：准时率、平均超期、涉及项目、在途。

    「哪家供应商靠不住」现在只能靠一次性的清单感觉，这里给可比的数。
    """
    nm = (name or "").strip()
    if not nm:
        return {"error": "要查哪个供应商？"}
    sup = (await db.execute(select(models.Supplier).where(
        models.Supplier.name.ilike(f"%{nm}%")).limit(1))).scalar_one_or_none()
    if not sup:
        return {"supplier": nm, "found": False, "hint": "查无此供应商，先用 find_entity 找"}

    items = list((await db.execute(select(models.PurchaseItem).where(
        models.PurchaseItem.supplier_id == sup.id))).scalars().all())
    today = date.today()
    on_time = late = 0
    late_days: list[int] = []
    pending = []
    for i in items:
        exp = (i.expected_arrival or "").strip()
        if not exp:
            continue
        try:
            ed = date.fromisoformat(exp)
        except ValueError:
            continue
        arrived = (i.arrival_date or "").strip()
        if arrived:
            # 有实际到货日就能真比一次准时与否；比不了的（日期格式坏）算准时，不编数
            try:
                on_time += 1 if date.fromisoformat(arrived) <= ed else 0
                if date.fromisoformat(arrived) > ed:
                    late += 1
                    late_days.append((date.fromisoformat(arrived) - ed).days)
            except ValueError:
                on_time += 1
        elif ed <= today:
            late += 1
            late_days.append((today - ed).days)
            pending.append({"name": i.item_name, "spec": i.spec, "expected_arrival": exp,
                            "over_days": (today - ed).days, "po_no": i.po_no,
                            "project_code": i.project_code})
    pending.sort(key=lambda x: -x["over_days"])
    total = on_time + late
    return {
        "supplier": sup.name, "found": True,
        "purchase_items": len(items),
        "on_time": on_time, "overdue": late,
        "on_time_rate": round(on_time / total, 3) if total else None,
        "avg_overdue_days": round(sum(late_days) / len(late_days), 1) if late_days else 0,
        "max_overdue_days": max(late_days) if late_days else 0,
        "items": pending[:_DETAIL_MAX], "count": len(pending),
    }


async def get_material(db: AsyncSession, current: models.User, q: str) -> dict:
    """物料全景：库存、库位、安全库存、近期出入库、在途采购。

    赵仁辉 36% 的操作在仓库，而 v1 一个仓库工具都没有（对他覆盖率 5%）。
    """
    text = (q or "").strip()
    if not text:
        return {"error": "要查哪个物料？给编码或名称"}
    m = (await db.execute(select(models.WhMaterial).where(
        or_(models.WhMaterial.code.ilike(f"%{text}%"),
            models.WhMaterial.name.ilike(f"%{text}%"),
            models.WhMaterial.spec.ilike(f"%{text}%"))).limit(1))).scalar_one_or_none()
    if not m:
        return {"material": text, "found": False, "hint": "查无此物料，先用 find_entity 找"}

    txns = list((await db.execute(select(models.WhTxn).where(
        models.WhTxn.material_id == m.id, models.WhTxn.is_reversal == False)  # noqa: E712
        .order_by(models.WhTxn.id.desc()))).scalars().all())
    stock = float(m.init_stock or 0)
    for t in txns:
        stock += float(t.qty or 0) if t.direction == "in" else -float(t.qty or 0)

    safety = float(m.safety_stock or 0)
    return {
        "material": m.name, "code": m.code, "spec": m.spec, "unit": m.unit, "found": True,
        "stock": round(stock, 3),
        "safety_stock": safety,
        # 低于安全库存是「要不要马上补货」的直接依据
        "below_safety": bool(safety and stock < safety),
        "shortfall": round(safety - stock, 3) if safety and stock < safety else 0,
        "location": m.location,
        "txn_count": len(txns),
        "recent_txns": [{"date": t.biz_date, "direction": t.direction, "qty": float(t.qty or 0),
                         "party": t.party, "project_id": t.project_id, "ref_no": t.ref_no}
                        for t in txns[:_DETAIL_MAX]],
    }
