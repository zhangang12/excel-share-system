"""🆕 v3 M14 报表：月度工作报表（仅管理层）+ 部门报表（负责人+管理层）+ 销售报表（销售主管+管理层）。

口径（与 dept_config.compute_efficiency 单一来源一致）：
- C1 自然日；C2 done==due 算按时；C3 预计/实际不足 1 天按 1 天（效率%=预计÷实际×100，越高越好）
- C4 月度统计按「下单时间(created_at)」归月（即当月下单批次的最终状态，非交付月）
- 按时率=按时÷已完成；平均效率为算术平均（🆕 越高越好：100=按时，>100=提前，<100=超期），
  不封顶（提前越多越高），考核口径如需封顶/中位数请业务确认后在 compute_efficiency 统一调整
报表为只读聚合，无新表。
"""
import re
from collections import defaultdict
from datetime import date, datetime, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_db
from .. import models
from ..deps import get_current_user, require_roles
from ..dept_config import DEPTS, compute_efficiency
from ..utils import write_audit

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
    name: str = ""        # 项目名称（逾期清单展示用）
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
                    dept_name=DEPTS[o.dept]["name"], worker_name=wname,
                    code=o.project.code if o.project else "", name=o.project.name if o.project else "",
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


# ==================== 生产部专用聚合（钣金组 + 装配组分组任务） ====================
# 生产部不走 DeptOrder.worker_id（父生产单 worker_id 恒为空），实际工人与完成都在
# ProduceGroupTask（每项目最多 3 条：钣金组 + 装配组 + 🆕封板组）。报表须按分组任务统计，否则
# 「已完成」永远为 0、人员明细缺失。完成时效以父生产单的 start/due + 本组 done_at 计算。
GROUP_NAMES = {"sheetmetal": "钣金组", "assembly": "装配组", "sealing": "封板组"}  # 🆕 反馈#209


def _china_date_str(dt: Optional[datetime]) -> Optional[str]:
    """UTC 时间戳 → 中国自然日(UTC+8) 的 YYYY-MM-DD，与系统其它处自然日口径一致。"""
    if not dt:
        return None
    return (dt + timedelta(hours=8)).strftime("%Y-%m-%d")


def _agg_produce(rows: list, umap: dict, month: Optional[str] = None):
    """rows: list[(ProduceGroupTask, DeptOrder parent, Project)]；按 (组, 工人) 聚合。
    返回 (stats, overdue_items, total, done, ontime, effs)。"""
    by: dict[tuple, dict] = {}
    overdue: list[OverdueItem] = []
    total = done_cnt = ontime_cnt = 0
    effs: list[int] = []
    for gt, parent, proj in rows:
        if month:  # 按父生产单的接单/下单月过滤（与其它部门口径一致）
            base = parent.start_date or (parent.created_at.strftime("%Y-%m-%d") if parent.created_at else "")
            if not (base and base.startswith(month)):
                continue
        gname = GROUP_NAMES.get(gt.group, gt.group)
        u = umap.get(gt.worker_id) if gt.worker_id else None
        wname = (u.full_name or u.username) if u else "（未指派）"
        key = (gt.group, gt.worker_id)
        r = by.setdefault(key, {"name": f"{wname} · {gname}", "gname": gname,
                                "total": 0, "done": 0, "ontime": 0, "over": 0, "effs": []})
        r["total"] += 1
        total += 1
        if gt.status == "done":
            r["done"] += 1
            done_cnt += 1
            dstr = _china_date_str(gt.done_at)
            # 🆕 生产部按「本组各自填的预计完成」算效率/逾期（gt.due_date），开始=父单派发日
            eff, on_time, over_days = compute_efficiency(parent.start_date, gt.due_date, dstr)
            if eff is not None:
                r["effs"].append(eff)
                effs.append(eff)
            if on_time:
                r["ontime"] += 1
                ontime_cnt += 1
            elif over_days > 0:
                r["over"] += 1
                overdue.append(OverdueItem(
                    dept_name=gname, worker_name=wname,
                    code=proj.code if proj else "", name=proj.name if proj else "",
                    due_date=gt.due_date, done_date=dstr, over_days=over_days, eff=eff))
    stats = []
    for r in by.values():
        e = r["effs"]
        stats.append(WorkerStat(
            dept="produce", dept_name=r["gname"], worker_name=r["name"],
            total=r["total"], done=r["done"], ontime=r["ontime"], over=r["over"],
            rate=round(r["ontime"] / r["done"] * 100) if r["done"] else None,
            avg_eff=round(sum(e) / len(e)) if e else None,
        ))
    stats.sort(key=lambda s: -s.total)
    return stats, overdue, total, done_cnt, ontime_cnt, effs


async def _load_produce_rows(db: AsyncSession, year: Optional[str] = None):
    """拉取所有有效（父单非作废、项目未删）的生产分组任务，连带父生产单与项目。"""
    q = (select(models.ProduceGroupTask, models.DeptOrder, models.Project)
         .join(models.DeptOrder, models.ProduceGroupTask.order_id == models.DeptOrder.id)
         .join(models.Project, models.ProduceGroupTask.project_id == models.Project.id)
         .where(models.Project.is_deleted == False,            # noqa: E712
                models.DeptOrder.status != "voided"))
    if year:
        q = q.where(models.Project.code.like(f"{year}-%"))
    rows = [(row[0], row[1], row[2]) for row in (await db.execute(q)).all()]
    wids = {gt.worker_id for gt, _, _ in rows if gt.worker_id}
    umap = {}
    if wids:
        ur = await db.execute(select(models.User).where(models.User.id.in_(wids)))
        umap = {u.id: u for u in ur.scalars().all()}
    return rows, umap


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

    # 🆕 生产部分组任务（C4 按父单下单月 created_at 归月）——供生产部卡片用分组口径
    prows, pumap = await _load_produce_rows(db)
    p_month = [(gt, parent, proj) for gt, parent, proj in prows
               if parent.created_at and parent.created_at.strftime("%Y-%m") == ym]
    _ps, p_over, p_total, p_done, p_ontime, p_effs = _agg_produce(p_month, pumap)

    # 部门概览
    dept_cards = []
    for dept, cfg in DEPTS.items():
        if dept == "produce":  # 生产部用钣金组/装配组分组任务统计，而非父生产单
            dept_cards.append({
                "dept": dept, "name": cfg["name"], "total": p_total, "done": p_done,
                "over": len(p_over),
                "rate": round(p_ontime / p_done * 100) if p_done else None,
                "avg_eff": round(sum(p_effs) / len(p_effs)) if p_effs else None,
            })
            continue
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
    year: Optional[str] = Query(None),
    month: Optional[str] = Query(None, description="YYYY-MM；按派单月份过滤，缺省=全年"),
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cfg = DEPTS.get(dept)
    if not cfg:
        raise HTTPException(400, "未知部门")
    if not current.has_role("admin", "manager", cfg["lead_role"]):
        raise HTTPException(403, "仅本部门负责人或管理层可看部门报表")

    # 🆕 生产部走分组任务（钣金组+装配组）统计，而非父生产单 worker_id（恒为空）
    if dept == "produce":
        rows, umap = await _load_produce_rows(db, year)
        stats, overdue_items, total, done_cnt, ontime_cnt, effs = _agg_produce(rows, umap, month)
        return DeptReport(
            dept="produce", dept_name=cfg["name"], total=total, done=done_cnt,
            overdue=len(overdue_items),
            ontime_rate=round(ontime_cnt / done_cnt * 100) if done_cnt else None,
            avg_eff=round(sum(effs) / len(effs)) if effs else None,
            workers=stats, overdue_items=overdue_items,
        )

    q = (select(models.DeptOrder)
         .join(models.Project, models.DeptOrder.project_id == models.Project.id)
         .where(models.DeptOrder.dept == dept, models.Project.is_deleted == False))  # noqa: E712
    if year:
        q = q.where(models.Project.code.like(f"{year}-%"))
    if month:
        # 按派单（接单开始）月份过滤：start_date 或 created_at 前缀匹配
        q = q.where(
            (models.DeptOrder.start_date.like(f"{month}%")) |
            (models.DeptOrder.start_date.is_(None) & models.DeptOrder.created_at.like(f"{month}%"))
        )
    r = await db.execute(q)
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


# ==================== 🆕 盈利改善第一档：项目毛利红黑榜(1a) + 成本黑洞审计(1b) ====================
# 依据《盈利改善功能规划.md》。纯只读聚合（两个修复动作除外），无新表。
# ⚠️ 口径（页面须醒目标注）：毛利 = 合同额 − 材料领料成本(加权均价) − 直发/外协采购
#   − 安装/售后费用(已审批)，**不含人工/运费** ——"材料边际贡献"口径。
# 防双算：收货已生成 wh_txn 的采购明细，其成本经「领料出库×加权均价」走仓库腿；
#   未经仓库过账的（直发/外协加工费）按 project_code 直接计入采购腿。两腿互斥不重叠。

_PNL_ROLES = ("finance", "finance_lead")
_PNL_NOTE = ("口径：合同额 − 材料领料(加权均价) − 直发/外协采购 − 安装/售后费用 − 物料运输费(我方)；"
             "不含人工工资等系统外成本，排名供比较用，绝对值≠净利")


def _code_year(code: str) -> str:
    m = re.search(r"(20\d{2})", code or "")
    return m.group(1) if m else "其他"


@router.get("/project-pnl")
async def project_pnl(
    _: models.User = Depends(require_roles(*_PNL_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """1a 项目毛利榜：逐项目 收入−三腿成本，带成本完整度标签；亏损最多的排最前。"""
    from .warehouse_router import _avg_price_map
    avg = await _avg_price_map(db)

    # ── 腿1：材料领料成本(仓库口径) + 完整度(领料中无均价的物料行数)
    r = await db.execute(
        select(models.WhTxn.project_id, models.WhTxn.material_id, func.sum(models.WhTxn.qty))
        .where(models.WhTxn.direction == "out", models.WhTxn.project_id.isnot(None),
               models.WhTxn.is_reversal == False,  # noqa: E712
               models.WhTxn.reversed == False)  # noqa: E712
        .group_by(models.WhTxn.project_id, models.WhTxn.material_id))
    mat_cost: dict = defaultdict(float)
    noprice: dict = defaultdict(int)
    for pid, mid, qty in r.all():
        price = avg.get(mid)
        if price is not None:
            mat_cost[pid] += (qty or 0) * price
        elif qty:
            noprice[pid] += 1

    # ── 腿2：直发/外协采购——收货金额>0 且收货未生成 wh_txn(未经仓库)，按 project_code
    linked = select(models.WhTxn.purchase_item_id).where(models.WhTxn.purchase_item_id.isnot(None))
    r = await db.execute(
        select(models.PurchaseItem.project_code, func.sum(models.PurchaseItem.received_amount))
        .where(models.PurchaseItem.received_amount > 0,
               models.PurchaseItem.project_code.isnot(None),
               models.PurchaseItem.id.not_in(linked))
        .group_by(models.PurchaseItem.project_code))
    direct_by_code: dict = {}
    for code, amt in r.all():
        c = (code or "").strip()
        if c:
            direct_by_code[c] = direct_by_code.get(c, 0) + (amt or 0)

    # ── 腿3：安装/售后费用(已审批)
    r = await db.execute(
        select(models.AfterSales.project_id, func.sum(models.AfterSales.cost))
        .where(models.AfterSales.status == "approved", models.AfterSales.project_id.isnot(None))
        .group_by(models.AfterSales.project_id))
    as_cost = {pid: (amt or 0) for pid, amt in r.all()}

    # ── 腿4：🆕 #201 物料运输费(我方承担的,按项目)
    r = await db.execute(
        select(models.Shipment.project_id, func.sum(models.Shipment.freight_cost))
        .where(models.Shipment.freight_cost > 0, models.Shipment.project_id.isnot(None),
               (models.Shipment.freight_payer == "我方") | (models.Shipment.freight_payer.is_(None)))
        .group_by(models.Shipment.project_id))
    freight_cost = {pid: (amt or 0) for pid, amt in r.all()}

    # ── 收入(销售台账,一项目一行) × 项目主数据
    projs = (await db.execute(select(models.Project).where(
        models.Project.is_deleted == False))).scalars().all()  # noqa: E712
    leds = (await db.execute(select(models.SalesLedger))).scalars().all()
    led_by_pid = {l.project_id: l for l in leds}

    rows = []
    for p in projs:
        led = led_by_pid.get(p.id)
        amount = round((led.amount or 0), 2) if led else 0.0
        mc = round(mat_cost.get(p.id, 0), 2)
        dc = round(direct_by_code.get((p.code or "").strip(), 0), 2)
        ac = round(as_cost.get(p.id, 0), 2)
        fc = round(freight_cost.get(p.id, 0), 2)   # 🆕 #201 运费腿(我方)
        total = round(mc + dc + ac + fc, 2)
        if not amount and not total:
            continue   # 没有任何钱数据的项目不进榜
        profit = round(amount - total, 2)
        flags = []
        if not led:
            flags.append("无销售台账")
        elif not amount:
            flags.append("合同额为0")
        if noprice.get(p.id):
            flags.append(f"领料缺价{noprice[p.id]}项")
        rows.append({
            "project_id": p.id, "code": p.code, "name": p.name, "status": p.status,
            "customer": (led.customer if led else None) or "未填",
            "cust_type": (led.cust_type if led else None) or "未填",
            "sales_name": ((led.sales_user.full_name or led.sales_user.username)
                           if led and led.sales_user else "未填"),
            "order_type": (led.order_type if led else None) or "未填",
            "year": _code_year(p.code),
            "amount": amount, "mat_cost": mc, "direct_cost": dc, "as_cost": ac, "freight_cost": fc,
            "total_cost": total, "profit": profit,
            "margin": (round(profit / amount * 100, 1) if amount else None),
            "flags": flags,
        })
    rows.sort(key=lambda x: x["profit"])   # 红榜在上：亏得最多的排最前
    total_amount = round(sum(x["amount"] for x in rows), 2)
    total_cost = round(sum(x["total_cost"] for x in rows), 2)
    return {
        "note": _PNL_NOTE,
        "rows": rows,
        "summary": {"projects": len(rows), "amount": total_amount, "cost": total_cost,
                    "profit": round(total_amount - total_cost, 2),
                    "loss_count": sum(1 for x in rows if x["profit"] < 0),
                    "incomplete_count": sum(1 for x in rows if x["flags"])},
    }


@router.get("/cost-audit")
async def cost_audit(
    _: models.User = Depends(require_roles(*_PNL_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """1b 成本黑洞审计：①无主领料 ②无价入库 ③孤儿采购 + 双口径对账 + 本月未归集合计。
    审计先行——这三张清单不清零，毛利榜就系统性虚高。"""
    from .warehouse_router import _avg_price_map
    avg = await _avg_price_map(db)
    this_month = date.today().isoformat()[:7]

    # ── ① 无主领料：出库未挂项目(排除冲红对；排除明确勾过「非项目领用」的) × 加权均价
    r = await db.execute(select(models.WhTxn).where(
        models.WhTxn.direction == "out", models.WhTxn.project_id.is_(None),
        models.WhTxn.is_reversal == False,  # noqa: E712
        models.WhTxn.reversed == False)  # noqa: E712
        .order_by(models.WhTxn.biz_date.desc(), models.WhTxn.id.desc()))
    orphan_out = []
    oo_total = oo_month = 0.0
    for t in r.scalars().all():
        if (t.source or "") == "非项目领用":
            continue   # 明确声明过的非项目领用不算漏归集
        val = t.amount
        if val is None and t.material_id in avg:
            val = (t.qty or 0) * avg[t.material_id]
        v2 = round(val, 2) if val is not None else None
        if v2:
            oo_total += v2
            if (t.biz_date or "")[:7] == this_month:
                oo_month += v2
        orphan_out.append({
            "id": t.id, "ref_no": t.ref_no, "biz_date": t.biz_date,
            "name": t.material.name if t.material else "",
            "spec": t.material.spec if t.material else None,
            "qty": t.qty, "source": t.source, "party": t.party, "value": v2})
    orphan_out = orphan_out[:500]

    # ── ② 无价入库：采购收货生成的流水没金额(含配对的「采购领用」出库)；采购侧已补价的可一键回填
    r = await db.execute(
        select(models.WhTxn, models.PurchaseItem)
        .join(models.PurchaseItem, models.PurchaseItem.id == models.WhTxn.purchase_item_id)
        .where(models.WhTxn.amount.is_(None),
               models.WhTxn.is_reversal == False)  # noqa: E712
        .order_by(models.WhTxn.id.desc()))
    unpriced_in = []
    fillable_cnt = 0
    for t, pi in r.all():
        fillable = pi.unit_price is not None
        if fillable:
            fillable_cnt += 1
        unpriced_in.append({
            "id": t.id, "ref_no": t.ref_no, "biz_date": t.biz_date, "direction": t.direction,
            "name": t.material.name if t.material else "",
            "spec": t.material.spec if t.material else None,
            "qty": t.qty, "po_no": pi.po_no,
            "supplier": pi.supplier.name if pi.supplier else None,
            "item_price": pi.unit_price, "fillable": fillable})
    unpriced_in = unpriced_in[:500]

    # ── ③ 孤儿采购：project_code 为空，或既不是项目编号、也不是字典里的非项目「订单编号」
    valid_codes = {(c or "").strip() for (c,) in (await db.execute(select(models.Project.code))).all()}
    dict_codes = {(v or "").strip() for (v,) in (await db.execute(
        select(models.MaterialDict.value).where(models.MaterialDict.dtype == "order_no"))).all()}
    r = await db.execute(select(models.PurchaseItem).order_by(models.PurchaseItem.id.desc()))
    orphan_purchase = []
    op_total = op_month = 0.0
    for i in r.scalars().all():
        code = (i.project_code or "").strip()
        if code and (code in valid_codes or code in dict_codes):
            continue
        amt = round(i.received_amount or 0, 2)
        op_total += amt
        if (i.arrival_date or i.delivery_date or "")[:7] == this_month:
            op_month += amt
        orphan_purchase.append({
            "id": i.id, "po_no": i.po_no,
            "supplier": i.supplier.name if i.supplier else None,
            "item_name": i.item_name, "spec": i.spec, "project_code": i.project_code,
            "received_amount": amt, "arrival_date": i.arrival_date,
            "buyer": (i.buyer.full_name or i.buyer.username) if i.buyer else None})
    orphan_purchase = orphan_purchase[:500]

    # ── ④ 双口径对账：逐项目 采购口径(全部收货金额) vs 仓库口径(领料×均价)，差异倒序
    r = await db.execute(
        select(models.PurchaseItem.project_code, func.sum(models.PurchaseItem.received_amount))
        .where(models.PurchaseItem.received_amount > 0,
               models.PurchaseItem.project_code.isnot(None))
        .group_by(models.PurchaseItem.project_code))
    purch_by_code: dict = {}
    for c, a in r.all():
        cc = (c or "").strip()
        if cc:
            purch_by_code[cc] = purch_by_code.get(cc, 0) + (a or 0)
    r = await db.execute(
        select(models.WhTxn.project_id, models.WhTxn.material_id, func.sum(models.WhTxn.qty))
        .where(models.WhTxn.direction == "out", models.WhTxn.project_id.isnot(None),
               models.WhTxn.is_reversal == False,  # noqa: E712
               models.WhTxn.reversed == False)  # noqa: E712
        .group_by(models.WhTxn.project_id, models.WhTxn.material_id))
    wh_by_pid: dict = defaultdict(float)
    for pid, mid, qty in r.all():
        price = avg.get(mid)
        if price is not None:
            wh_by_pid[pid] += (qty or 0) * price
    projs = (await db.execute(
        select(models.Project.id, models.Project.code, models.Project.name))).all()
    recon = []
    for pid, code, name in projs:
        pu = round(purch_by_code.get((code or "").strip(), 0), 2)
        wh = round(wh_by_pid.get(pid, 0), 2)
        if not pu and not wh:
            continue
        recon.append({"project_id": pid, "code": code, "name": name,
                      "purchase": pu, "warehouse": wh, "diff": round(pu - wh, 2)})
    recon.sort(key=lambda x: -abs(x["diff"]))
    recon = recon[:200]

    return {
        "month": this_month,
        "month_unallocated": round(oo_month + op_month, 2),
        "total_unallocated": round(oo_total + op_total, 2),
        "fillable_count": fillable_cnt,
        "orphan_out": orphan_out, "unpriced_in": unpriced_in,
        "orphan_purchase": orphan_purchase, "recon": recon,
    }


class _AssignProjectIn(BaseModel):
    project_id: int


@router.patch("/cost-audit/txns/{tid}/project")
async def audit_assign_project(
    tid: int,
    body: _AssignProjectIn,
    current: models.User = Depends(require_roles(*_PNL_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """无主领料修复动作：事后补选项目——出库流水挂回项目，材料成本归集进项目毛利。"""
    t = (await db.execute(select(models.WhTxn).where(models.WhTxn.id == tid))).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "流水不存在")
    if t.direction != "out" or t.is_reversal or t.reversed:
        raise HTTPException(400, "仅未冲红的出库流水可补选项目")
    if t.project_id:
        raise HTTPException(400, "该流水已挂项目")
    p = (await db.execute(select(models.Project).where(
        models.Project.id == body.project_id,
        models.Project.is_deleted == False))).scalar_one_or_none()  # noqa: E712
    if not p:
        raise HTTPException(404, "项目不存在")
    t.project_id = p.id
    await db.commit()
    await write_audit(db, user=current, action="audit_assign_project", target_type="wh_txn",
                      target_id=tid, detail=f"{t.ref_no} 补挂项目 {p.code}")
    return {"message": f"已把 {t.ref_no} 归集到 {p.code}"}


@router.post("/cost-audit/backfill-prices")
async def audit_backfill_prices(
    current: models.User = Depends(require_roles(*_PNL_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """无价入库修复动作：一键回填——凡采购明细已补价、而其收货流水仍无价的，按采购单价回写
    (含配对的「采购领用」出库)。新产生的无价入库已由「补价回写 wh_txn」修复堵住，此接口清存量。"""
    r = await db.execute(
        select(models.WhTxn, models.PurchaseItem.unit_price)
        .join(models.PurchaseItem, models.PurchaseItem.id == models.WhTxn.purchase_item_id)
        .where(models.WhTxn.amount.is_(None),
               models.WhTxn.is_reversal == False,  # noqa: E712
               models.PurchaseItem.unit_price.isnot(None)))
    n = 0
    for t, price in r.all():
        t.unit_price = price
        t.amount = round((t.qty or 0) * price, 4)
        n += 1
    if n:
        await db.commit()
        await write_audit(db, user=current, action="audit_backfill_prices", target_type="wh_txn",
                          detail=f"一键回填 {n} 条无价收货流水")
    return {"message": f"已回填 {n} 条流水价格", "fixed": n}


# ==================== 🆕 盈利改善第二档：资金面板（应收/应付/呆滞，纯现有数据） ====================
# 依据《盈利改善功能规划.md》第二档："不赚钱的企业,现金断裂比利润难看死得更快。"
# 口径备注：
# - 应收：四段款是合同条款数字非到账流水(规划§四)——尾款按 balance_date、发货款按 ship_date 计龄;
#   预付/发货前付无约定日期字段，不进账龄（真实回款率要靠第三档收款流水）。
# - 应付到期日 = 到货日期 + Supplier.credit_days（月结口径如与财务口径不符，调这里）。
# - 13周现金排程的流出侧：已批未付请款与"应付到期"会重叠——已进请款单的明细从应付排程里剔除。


def _aging_bucket(days: int) -> str:
    if days <= 30:
        return "1-30天"
    if days <= 60:
        return "31-60天"
    if days <= 90:
        return "61-90天"
    return "90天以上"


@router.get("/fund-panel")
async def fund_panel(
    _: models.User = Depends(require_roles(*_PNL_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """资金面板：逾期应收账龄 / 预付敞口 / 应付账期利用 / 呆滞库存 / 未来13周现金排程。"""
    today = date.today()
    today_s = today.isoformat()

    # ───────── ① 逾期应收账龄（尾款按 balance_date；发货款按 ship_date） ─────────
    leds = (await db.execute(select(models.SalesLedger))).scalars().all()
    balance_rows, ship_rows = [], []
    buckets: dict = defaultdict(float)
    by_customer: dict = defaultdict(float)
    by_sales: dict = defaultdict(float)
    for led in leds:
        p = led.project
        if not p or p.is_deleted:
            continue
        code, name = p.code, p.name
        sales = (led.sales_user.full_name or led.sales_user.username) if led.sales_user else "未填"
        cust = led.customer or "未填"
        # 尾款逾期
        if (led.balance or 0) > 0 and led.balance_date:
            try:
                due = date.fromisoformat(led.balance_date)
            except ValueError:
                due = None
            if due and due < today:
                over = (today - due).days
                balance_rows.append({
                    "ledger_id": led.id, "code": code, "name": name, "customer": cust,
                    "sales_name": sales, "kind": "尾款", "amount": led.balance,
                    "due_date": led.balance_date, "over_days": over,
                    "bucket": _aging_bucket(over), "shipped": bool(led.ship_date)})
                buckets[_aging_bucket(over)] += led.balance
                by_customer[cust] += led.balance
                by_sales[sales] += led.balance
        # 发货款（已发货即应收；无收讫字段，>0 视为未收）
        if (led.ship_receivable or 0) > 0 and led.ship_date:
            try:
                sd = date.fromisoformat(led.ship_date)
            except ValueError:
                sd = None
            if sd and sd < today:
                over = (today - sd).days
                ship_rows.append({
                    "ledger_id": led.id, "code": code, "name": name, "customer": cust,
                    "sales_name": sales, "kind": "发货款", "amount": led.ship_receivable,
                    "due_date": led.ship_date, "over_days": over,
                    "bucket": _aging_bucket(over), "shipped": True})
                buckets[_aging_bucket(over)] += led.ship_receivable
                by_customer[cust] += led.ship_receivable
                by_sales[sales] += led.ship_receivable
    balance_rows.sort(key=lambda x: -x["over_days"])
    ship_rows.sort(key=lambda x: -x["over_days"])
    recv_total = round(sum(buckets.values()), 2)
    receivables = {
        "total": recv_total,
        "buckets": [{"bucket": b, "amount": round(buckets.get(b, 0), 2)}
                    for b in ("1-30天", "31-60天", "61-90天", "90天以上")],
        "balance_rows": balance_rows[:300], "ship_rows": ship_rows[:300],
        "by_customer": sorted(
            [{"key": k, "amount": round(v, 2)} for k, v in by_customer.items()],
            key=lambda x: -x["amount"])[:10],
        "by_sales": sorted(
            [{"key": k, "amount": round(v, 2)} for k, v in by_sales.items()],
            key=lambda x: -x["amount"])[:10],
    }

    # ───────── ② 预付敞口（已付款、货未到——押在供应商那里的钱） ─────────
    r = await db.execute(
        select(models.PurchaseItem)
        .where(models.PurchaseItem.paid_amount > 0,
               models.PurchaseItem.arrival_date.is_(None)))
    pre_by_sup: dict = {}
    for i in r.scalars().all():
        sup = i.supplier.name if i.supplier else "未知供应商"
        g = pre_by_sup.setdefault(sup, {"supplier": sup, "amount": 0.0, "items": 0,
                                        "oldest_paid": None, "days": None})
        g["amount"] += i.paid_amount or 0
        g["items"] += 1
        if i.paid_date and (g["oldest_paid"] is None or i.paid_date < g["oldest_paid"]):
            g["oldest_paid"] = i.paid_date
    for g in pre_by_sup.values():
        g["amount"] = round(g["amount"], 2)
        if g["oldest_paid"]:
            try:
                g["days"] = (today - date.fromisoformat(g["oldest_paid"])).days
            except ValueError:
                pass
    prepay_rows = sorted(pre_by_sup.values(), key=lambda x: -x["amount"])
    prepay = {"total": round(sum(g["amount"] for g in prepay_rows), 2), "rows": prepay_rows[:100]}

    # ───────── ③ 应付账期利用（到期日=到货+credit_days） ─────────
    r = await db.execute(
        select(models.PurchaseItem).where(models.PurchaseItem.received_amount > 0))
    pay_items = list(r.scalars().all())
    # 已进「已批未付」请款单的明细：13周排程剔除（这笔钱已按请款单金额计入 W0 流出，防双算）。
    # 待审批(pending)的不剔——其请款金额不计入流出，明细仍按应付到期排周。
    pr_linked = {iid for (iid,) in (await db.execute(
        select(models.PaymentRequestItem.item_id)
        .join(models.PaymentRequest,
              models.PaymentRequest.id == models.PaymentRequestItem.request_id)
        .where(models.PaymentRequest.status == "approved"))).all()}
    overdue_by_sup: dict = {}
    due_soon_by_sup: dict = {}
    early_rows = []
    early_total = 0.0
    early_days_sum = 0.0
    missing_credit: dict = defaultdict(float)
    due_schedule: list = []   # (due_date, outstanding, item_id) → 13周排程流出
    for i in pay_items:
        sup = i.supplier
        sup_name = sup.name if sup else "未知供应商"
        outstanding = round((i.received_amount or 0) - (i.paid_amount or 0), 2)
        due = None
        if i.arrival_date and sup and sup.credit_days is not None:
            try:
                due = date.fromisoformat(i.arrival_date) + timedelta(days=sup.credit_days)
            except ValueError:
                due = None
        if outstanding > 0.01 and i.arrival_date and (sup is None or sup.credit_days is None):
            missing_credit[sup_name] += outstanding   # 有应付但没维护账期 → 补主数据
        if due is None:
            continue
        if outstanding > 0.01:
            due_schedule.append((due, outstanding, i.id))
            if due < today:
                g = overdue_by_sup.setdefault(sup_name, {"supplier": sup_name, "amount": 0.0,
                                                         "items": 0, "worst_days": 0})
                g["amount"] += outstanding
                g["items"] += 1
                g["worst_days"] = max(g["worst_days"], (today - due).days)
            elif (due - today).days <= 14:
                g = due_soon_by_sup.setdefault(sup_name, {"supplier": sup_name, "amount": 0.0,
                                                          "items": 0, "nearest_due": due.isoformat()})
                g["amount"] += outstanding
                g["items"] += 1
                g["nearest_due"] = min(g["nearest_due"], due.isoformat())
        # 提前付：有账期却在到期日前就付了 → 白白放弃的免息天数
        if (i.paid_amount or 0) > 0 and i.paid_date and sup and (sup.credit_days or 0) > 0:
            try:
                pd = date.fromisoformat(i.paid_date)
            except ValueError:
                pd = None
            if pd and pd < due:
                wasted = (due - pd).days
                early_total += i.paid_amount or 0
                early_days_sum += wasted * (i.paid_amount or 0)
                early_rows.append({"po_no": i.po_no, "supplier": sup_name,
                                   "item_name": i.item_name, "paid_amount": i.paid_amount,
                                   "paid_date": i.paid_date, "due_date": due.isoformat(),
                                   "wasted_days": wasted})
    for g in overdue_by_sup.values():
        g["amount"] = round(g["amount"], 2)
    for g in due_soon_by_sup.values():
        g["amount"] = round(g["amount"], 2)
    early_rows.sort(key=lambda x: -(x["paid_amount"] or 0))
    payables = {
        "overdue": sorted(overdue_by_sup.values(), key=lambda x: -x["amount"])[:100],
        "overdue_total": round(sum(g["amount"] for g in overdue_by_sup.values()), 2),
        "due_soon": sorted(due_soon_by_sup.values(), key=lambda x: x["nearest_due"])[:100],
        "due_soon_total": round(sum(g["amount"] for g in due_soon_by_sup.values()), 2),
        "early_paid": {"total": round(early_total, 2),
                       "avg_wasted_days": (round(early_days_sum / early_total, 1) if early_total else 0),
                       "rows": early_rows[:50]},
        "missing_credit": sorted(
            [{"supplier": k, "outstanding": round(v, 2)} for k, v in missing_credit.items()],
            key=lambda x: -x["outstanding"])[:50],
    }

    # ───────── ④ 呆滞库存（≥90天无出库动销的 现存×加权均价 = 锁死的现金） ─────────
    from .warehouse_router import _avg_price_map, _stock_map
    avg = await _avg_price_map(db)
    stock = await _stock_map(db)
    mats = (await db.execute(select(models.WhMaterial))).scalars().all()
    # 各物料最后一次出库日 / 最后一次入库日（判定从未动销的入库时点）
    lo = dict((await db.execute(
        select(models.WhTxn.material_id, func.max(models.WhTxn.biz_date))
        .where(models.WhTxn.direction == "out",
               models.WhTxn.is_reversal == False)  # noqa: E712
        .group_by(models.WhTxn.material_id))).all())
    li = dict((await db.execute(
        select(models.WhTxn.material_id, func.max(models.WhTxn.biz_date))
        .where(models.WhTxn.direction == "in",
               models.WhTxn.is_reversal == False)  # noqa: E712
        .group_by(models.WhTxn.material_id))).all())
    # 采购回溯：各物料最近一笔带采购来源的入库 → 谁为哪个项目买的
    trace: dict = {}
    tr = await db.execute(
        select(models.WhTxn.material_id, models.PurchaseItem.project_code,
               models.User.full_name, models.User.username, models.WhTxn.id)
        .join(models.PurchaseItem, models.PurchaseItem.id == models.WhTxn.purchase_item_id)
        .outerjoin(models.User, models.User.id == models.PurchaseItem.buyer_id)
        .where(models.WhTxn.direction == "in")
        .order_by(models.WhTxn.id))
    for mid, pcode, fn, un, _tid in tr.all():
        trace[mid] = {"project_code": pcode, "buyer": fn or un}   # 按 id 升序遍历,留下最新一笔
    # 近90天月均出库（安全库存体检用）
    d90 = (today - timedelta(days=90)).isoformat()
    out90 = dict((await db.execute(
        select(models.WhTxn.material_id, func.sum(models.WhTxn.qty))
        .where(models.WhTxn.direction == "out", models.WhTxn.biz_date >= d90,
               models.WhTxn.is_reversal == False)  # noqa: E712
        .group_by(models.WhTxn.material_id))).all())
    dead_rows = []
    dead_total = 0.0
    dead_buckets: dict = defaultdict(float)
    safety_rows = []
    for m in mats:
        st = stock.get(m.id, m.init_stock or 0)
        if st <= 0:
            continue
        last = lo.get(m.id)
        since = last or li.get(m.id)   # 从未出库 → 按最后入库日起算
        idle = None
        if since:
            try:
                idle = (today - date.fromisoformat(since[:10])).days
            except ValueError:
                idle = None
        price = avg.get(m.id)
        val = round(st * price, 2) if price is not None else None
        if idle is not None and idle >= 90:
            b = "365天以上" if idle >= 365 else ("180-365天" if idle >= 180 else "90-180天")
            dead_rows.append({
                "material_id": m.id, "name": m.name, "spec": m.spec, "unit": m.unit,
                "stock": st, "avg_price": price, "value": val,
                "last_out": last, "idle_days": idle, "never_out": last is None, "bucket": b,
                "trace_project": (trace.get(m.id) or {}).get("project_code"),
                "trace_buyer": (trace.get(m.id) or {}).get("buyer")})
            if val:
                dead_total += val
                dead_buckets[b] += val
        # 安全库存体检
        mo = round((out90.get(m.id, 0) or 0) / 3, 1)
        if (m.safety_stock or 0) > 0:
            if mo == 0 or (m.safety_stock or 0) > 2 * mo:
                safety_rows.append({"material_id": m.id, "name": m.name, "spec": m.spec,
                                    "safety_stock": m.safety_stock, "month_avg_out": mo,
                                    "stock": st, "verdict": "偏高压钱"})
            elif st < (m.safety_stock or 0):
                safety_rows.append({"material_id": m.id, "name": m.name, "spec": m.spec,
                                    "safety_stock": m.safety_stock, "month_avg_out": mo,
                                    "stock": st, "verdict": "低于安全库存(常报警)"})
    dead_rows.sort(key=lambda x: -(x["value"] or 0))
    dead_stock = {
        "total_value": round(dead_total, 2),
        "buckets": [{"bucket": b, "value": round(dead_buckets.get(b, 0), 2)}
                    for b in ("90-180天", "180-365天", "365天以上")],
        "rows": dead_rows[:200], "safety": safety_rows[:100],
    }

    # ───────── ⑤ 未来13周现金排程 ─────────
    # 流入：尾款(有约定日)按周挂；已逾期未收进 W0。发货款/预付等无日期 → undated 提示。
    # 流出：已批未付请款(随时可付,W0) + 应付到期按周(剔除已进请款单的明细,防双算) + OA待付款(W0)。
    weeks = [{"idx": i,
              "label": ("已到期/随时" if i == 0 else
                        f"{(today + timedelta(days=(i - 1) * 7 + 1)).strftime('%m-%d')}~"
                        f"{(today + timedelta(days=i * 7)).strftime('%m-%d')}"),
              "inflow": 0.0, "outflow": 0.0} for i in range(14)]
    inflow_later = outflow_later = 0.0
    undated_inflow = 0.0
    for led in leds:
        p = led.project
        if not p or p.is_deleted:
            continue
        if (led.balance or 0) > 0 and led.balance_date:
            try:
                due = date.fromisoformat(led.balance_date)
            except ValueError:
                continue
            dd = (due - today).days
            if dd <= 0:
                weeks[0]["inflow"] += led.balance
            elif dd <= 13 * 7:
                weeks[(dd + 6) // 7]["inflow"] += led.balance
            else:
                inflow_later += led.balance
        if (led.ship_receivable or 0) > 0 and led.ship_date:
            undated_inflow += led.ship_receivable   # 已发货应收但无约定日 → 无法排周
    apr = (await db.execute(
        select(func.coalesce(func.sum(models.PaymentRequest.requested_amount), 0))
        .where(models.PaymentRequest.status == "approved"))).scalar() or 0
    weeks[0]["outflow"] += apr
    for due, outstanding, iid in due_schedule:
        if iid in pr_linked:
            continue   # 已进请款单 → 金额算在请款侧
        dd = (due - today).days
        if dd <= 0:
            weeks[0]["outflow"] += outstanding
        elif dd <= 13 * 7:
            weeks[(dd + 6) // 7]["outflow"] += outstanding
        else:
            outflow_later += outstanding
    oa_pend = (await db.execute(
        select(func.coalesce(func.sum(
            func.coalesce(models.OaRequest.settle_amount, models.OaRequest.amount)), 0))
        .where(models.OaRequest.status == "pending_payment"))).scalar() or 0
    weeks[0]["outflow"] += oa_pend
    cum = 0.0
    for w in weeks:
        w["inflow"] = round(w["inflow"], 2)
        w["outflow"] = round(w["outflow"], 2)
        w["net"] = round(w["inflow"] - w["outflow"], 2)
        cum += w["net"]
        w["cum"] = round(cum, 2)
    cashgap = {
        "weeks": weeks,
        "undated_inflow": round(undated_inflow, 2),
        "inflow_later": round(inflow_later, 2), "outflow_later": round(outflow_later, 2),
        "note": ("流入只含有约定日期的尾款(已逾期计入W0)；发货款等无约定日不排周。"
                 "流出=已批未付请款+应付到期(已进请款单的明细不重复计)+OA待付款。"
                 "累计净额为负的周=现金缺口预警。实际到账精度依赖第三档「收款流水登记」。"),
    }

    return {"as_of": today_s, "receivables": receivables, "prepay": prepay,
            "payables": payables, "dead_stock": dead_stock, "cashgap": cashgap}
