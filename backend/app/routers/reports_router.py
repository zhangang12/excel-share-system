"""🆕 v3 M14 报表：月度工作报表（仅管理层）+ 部门报表（负责人+管理层）+ 销售报表（销售主管+管理层）。

口径（与 dept_config.compute_efficiency 单一来源一致）：
- C1 自然日；C2 done==due 算按时；C3 预计 0 天按 1 天（效率%=实际÷预计×100）
- C4 月度统计按「下单时间(created_at)」归月（即当月下单批次的最终状态，非交付月）
- 按时率=按时÷已完成；平均效率为算术平均，不封顶（单条可>100，用于暴露严重超期，
  考核口径如需封顶/中位数请业务确认后在 compute_efficiency 统一调整）
报表为只读聚合，无新表。
"""
from collections import defaultdict
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_db
from .. import models
from ..deps import get_current_user, require_roles
from ..dept_config import DEPTS, compute_efficiency

router = APIRouter(prefix="/api/reports", tags=["报表"])


# ==================== 公共：人员效率聚合 ====================
class WorkerStat(BaseModel):
    dept: str
    dept_name: str
    worker_name: str
    total: int = 0
    done: int = 0
    ontime: int = 0
    over: int = 0
    rate: Optional[int] = None      # 按时率%
    avg_eff: Optional[int] = None   # 平均效率%


class OverdueItem(BaseModel):
    dept_name: str
    worker_name: str
    code: str
    due_date: Optional[str] = None
    done_date: Optional[str] = None
    over_days: int = 0
    eff: Optional[int] = None


def _agg_workers(orders: list[models.DeptOrder]) -> tuple[list[WorkerStat], list[OverdueItem]]:
    by: dict[tuple, dict] = {}
    overdue: list[OverdueItem] = []
    for o in orders:
        if o.status in ("voided", "pending_assign") or not o.worker_id:
            continue
        wname = (o.worker.full_name or o.worker.username) if o.worker else f"#{o.worker_id}"
        key = (o.dept, o.worker_id)
        r = by.setdefault(key, {"dept": o.dept, "name": wname, "total": 0, "done": 0,
                                "ontime": 0, "over": 0, "effs": []})
        r["total"] += 1
        if o.status == "done":
            r["done"] += 1
            eff, on_time, over_days = compute_efficiency(o.start_date, o.due_date, o.done_date)
            if eff is not None:
                r["effs"].append(eff)
            if on_time:
                r["ontime"] += 1
            elif over_days > 0:
                r["over"] += 1
                overdue.append(OverdueItem(
                    dept_name=DEPTS[o.dept]["name"], worker_name=wname, code=o.project.code if o.project else "",
                    due_date=o.due_date, done_date=o.done_date, over_days=over_days, eff=eff))
    stats = []
    for r in by.values():
        effs = r["effs"]
        stats.append(WorkerStat(
            dept=r["dept"], dept_name=DEPTS[r["dept"]]["name"], worker_name=r["name"],
            total=r["total"], done=r["done"], ontime=r["ontime"], over=r["over"],
            rate=round(r["ontime"] / r["done"] * 100) if r["done"] else None,
            avg_eff=round(sum(effs) / len(effs)) if effs else None,
        ))
    stats.sort(key=lambda s: -s.total)
    return stats, overdue


# ==================== 月度工作报表（仅管理层） ====================
class MonthlyReport(BaseModel):
    month: str
    total: int = 0
    done: int = 0
    overdue: int = 0
    ontime_rate: Optional[int] = None
    avg_eff: Optional[int] = None
    dept_cards: list[dict]
    workers: list[WorkerStat]
    overdue_items: list[OverdueItem]
    sales_order_count: int = 0     # 其它部门工作量：销售下单数（当月新建项目）
    wh_txn_count: int = 0          # 仓库出入库笔数（当月）


@router.get("/monthly", response_model=MonthlyReport)
async def monthly(
    month: Optional[str] = Query(None, description="YYYY-MM；缺省当月"),
    _: models.User = Depends(require_roles()),  # 仅 admin/manager（require_roles 默认只放行管理层）
    db: AsyncSession = Depends(get_db),
):
    ym = month or datetime.now(timezone.utc).strftime("%Y-%m")
    r = await db.execute(select(models.DeptOrder))
    all_orders = list(r.scalars().all())
    # 按下单月(created_at)过滤（C4）
    orders = [o for o in all_orders if o.created_at and o.created_at.strftime("%Y-%m") == ym]

    stats, overdue_items = _agg_workers(orders)
    done = [o for o in orders if o.status == "done"]
    effs = []
    ontime = 0
    for o in done:
        eff, on_time, _ = compute_efficiency(o.start_date, o.due_date, o.done_date)
        if eff is not None:
            effs.append(eff)
        if on_time:
            ontime += 1

    # 部门概览
    dept_cards = []
    for dept, cfg in DEPTS.items():
        ds = [o for o in orders if o.dept == dept and o.status not in ("voided", "pending_assign")]
        dd = [o for o in ds if o.status == "done"]
        de = [compute_efficiency(o.start_date, o.due_date, o.done_date) for o in dd]
        de_eff = [x[0] for x in de if x[0] is not None]
        d_ontime = sum(1 for x in de if x[1])
        d_over = sum(1 for x in de if x[2] > 0)
        dept_cards.append({
            "dept": dept, "name": cfg["name"], "total": len(ds), "done": len(dd),
            "over": d_over,
            "rate": round(d_ontime / len(dd) * 100) if dd else None,
            "avg_eff": round(sum(de_eff) / len(de_eff)) if de_eff else None,
        })

    # 其它部门工作量
    r = await db.execute(select(models.Project).where(models.Project.is_deleted == False))  # noqa: E712
    sales_cnt = sum(1 for p in r.scalars().all() if p.created_at and p.created_at.strftime("%Y-%m") == ym)
    # 🆕 #22 仓库当月出入库笔数（biz_date=YYYY-MM-DD，按月前缀统计；含冲红反向单，反映实际作业量）
    wr = await db.execute(
        select(func.count(models.WhTxn.id)).where(models.WhTxn.biz_date.like(f"{ym}-%"))
    )
    wh_cnt = int(wr.scalar() or 0)

    return MonthlyReport(
        month=ym, total=len(orders), done=len(done), overdue=len(overdue_items),
        ontime_rate=round(ontime / len(done) * 100) if done else None,
        avg_eff=round(sum(effs) / len(effs)) if effs else None,
        dept_cards=dept_cards, workers=stats, overdue_items=overdue_items,
        sales_order_count=sales_cnt, wh_txn_count=wh_cnt,
    )


# ==================== 部门报表（负责人 + 管理层） ====================
class DeptReport(BaseModel):
    dept: str
    dept_name: str
    total: int = 0
    done: int = 0
    overdue: int = 0
    ontime_rate: Optional[int] = None
    avg_eff: Optional[int] = None
    workers: list[WorkerStat]
    overdue_items: list[OverdueItem]


@router.get("/dept/{dept}", response_model=DeptReport)
async def dept_report(
    dept: str,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cfg = DEPTS.get(dept)
    if not cfg:
        raise HTTPException(400, "未知部门")
    code = current.role.code if current.role else ""
    if code not in ("admin", "manager", cfg["lead_role"]):
        raise HTTPException(403, "仅本部门负责人或管理层可看部门报表")

    r = await db.execute(select(models.DeptOrder).where(models.DeptOrder.dept == dept))
    orders = [o for o in r.scalars().all() if o.status not in ("voided", "pending_assign")]
    stats, overdue_items = _agg_workers(orders)
    done = [o for o in orders if o.status == "done"]
    effs, ontime = [], 0
    for o in done:
        eff, on_time, _ = compute_efficiency(o.start_date, o.due_date, o.done_date)
        if eff is not None:
            effs.append(eff)
        if on_time:
            ontime += 1
    return DeptReport(
        dept=dept, dept_name=cfg["name"], total=len(orders), done=len(done),
        overdue=len(overdue_items),
        ontime_rate=round(ontime / len(done) * 100) if done else None,
        avg_eff=round(sum(effs) / len(effs)) if effs else None,
        workers=stats, overdue_items=overdue_items,
    )


# ==================== 销售报表（销售主管 + 管理层） ====================
class SalesReport(BaseModel):
    project_count: int = 0
    total_amount: float = 0
    invoiced_amount: float = 0
    uninvoiced_amount: float = 0
    shipped_count: int = 0
    contract_count: int = 0
    contract_rate: Optional[int] = None
    invoice_rate: Optional[int] = None
    by_salesperson: list[dict]
    by_cust_type: list[dict]
    by_invoice_state: list[dict]
    receivables: dict


@router.get("/sales", response_model=SalesReport)
async def sales_report(
    _: models.User = Depends(require_roles("sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        select(models.SalesLedger).join(models.Project)
        .where(models.Project.is_deleted == False))  # noqa: E712
    leds = list(r.scalars().all())
    total_amount = sum(l.amount or 0 for l in leds)
    invoiced = [l for l in leds if l.invoice_state == "invoiced"]
    invoiced_amount = sum(l.amount or 0 for l in invoiced)
    contract_cnt = sum(1 for l in leds if l.contract == "有")
    shipped = sum(1 for l in leds if l.ship_date)

    # 按销售员
    by_s: dict = defaultdict(lambda: {"name": "", "n": 0, "amt": 0, "inv": 0, "ship": 0})
    for l in leds:
        k = l.sales_uid or 0
        name = (l.sales_user.full_name or l.sales_user.username) if l.sales_user else "（未分配）"
        r2 = by_s[k]
        r2["name"] = name
        r2["n"] += 1
        r2["amt"] += l.amount or 0
        if l.invoice_state == "invoiced":
            r2["inv"] += l.amount or 0
        if l.ship_date:
            r2["ship"] += 1
    by_salesperson = sorted(
        [{"name": v["name"], "count": v["n"], "amount": v["amt"], "invoiced": v["inv"],
          "shipped": v["ship"], "pct": round(v["amt"] / total_amount * 100) if total_amount else 0}
         for v in by_s.values()], key=lambda x: -x["amount"])

    # 客户分类
    by_ct = []
    for ct in ("经销商", "终端客户"):
        arr = [l for l in leds if l.cust_type == ct]
        by_ct.append({"type": ct, "count": len(arr), "amount": sum(l.amount or 0 for l in arr)})

    # 开票状态
    state_map = {None: "未申请", "applying": "待主管审批", "pending_invoice": "待财务开票", "invoiced": "已开票"}
    by_is = []
    for st, label in state_map.items():
        arr = [l for l in leds if l.invoice_state == st]
        by_is.append({"label": label, "count": len(arr), "amount": sum(l.amount or 0 for l in arr)})

    return SalesReport(
        project_count=len(leds), total_amount=total_amount,
        invoiced_amount=invoiced_amount, uninvoiced_amount=total_amount - invoiced_amount,
        shipped_count=shipped, contract_count=contract_cnt,
        contract_rate=round(contract_cnt / len(leds) * 100) if leds else None,
        invoice_rate=round(invoiced_amount / total_amount * 100) if total_amount else None,
        by_salesperson=by_salesperson, by_cust_type=by_ct, by_invoice_state=by_is,
        receivables={
            "prepay": sum(l.prepay or 0 for l in leds),
            "before_ship": sum(l.before_ship or 0 for l in leds),
            "ship_receivable": sum(l.ship_receivable or 0 for l in leds),
            "balance": sum(l.balance or 0 for l in leds),
        },
    )
