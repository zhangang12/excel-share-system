"""🆕 v3 M12：项目详单「部门协作」聚合 + 装配前置三表状态 + 数据表完成标记。

- GET /api/projects/{pid}/workflow：全流程 DAG 数据（销售/三部门任务/下游产物/发货闸门），
  供项目详情协作 tab 与销售全览复用
- GET /api/assembly/sheet-status：装配前置三表(钣金装配/标准件清单/外协外购)完成情况，
  供生产部工作台展示（§十七）
- PUT /api/datasheets/{did}/done-flag：标记/取消装配前置表"已完成"（管理层/生产主管/设计师）
"""
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user, require_can_view_detail
from ..dept_config import DEPTS, compute_efficiency
from ..sheet_templates import ASSEMBLY_PRECHECK_SHEETS
from ..utils import write_audit

router = APIRouter(prefix="/api", tags=["部门协作"])


# ==================== 工作流聚合 ====================
class WfDept(BaseModel):
    dept: str
    name: str
    status: str            # none/pending_assign/assigned/in_progress/done/voided
    worker_name: Optional[str] = None
    due_date: Optional[str] = None
    done_date: Optional[str] = None
    eff_pct: Optional[int] = None


class WfOut(BaseModel):
    project_id: int
    code: str
    name: str
    status: str
    sales_name: Optional[str] = None
    sign_date: Optional[str] = None
    deliver_date: Optional[str] = None
    depts: list[WfDept]
    sheetpkg_count: int = 0       # 设计图纸包数（→钣金）
    purchase_list_count: int = 0  # 电工采购清单数（→采购）
    ship_list_count: int = 0      # 仓库发货清单数（→物流）
    ship_status: str = "pending"
    can_ship: bool = False
    gate_missing: list[str] = []


@router.get("/projects/{pid}/workflow", response_model=WfOut)
async def project_workflow(
    pid: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """全流程节点聚合。销售可查自己项目的全览（不经详单闸门），其余按可见性。"""
    res = await db.execute(select(models.Project).where(
        models.Project.id == pid, models.Project.is_deleted == False))  # noqa: E712
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")

    # 销售员仅能看自己台账的项目；其余角色经详单闸门（销售例外，用于全览）
    from ..menus import user_can_view_detail
    if not user_can_view_detail(current):
        is_sales = current.role and current.role.code in ("sales", "sales_lead")
        if is_sales:
            r2 = await db.execute(select(models.SalesLedger).where(
                models.SalesLedger.project_id == pid))
            led = r2.scalar_one_or_none()
            if current.role.code == "sales" and (not led or led.sales_uid != current.id):
                raise HTTPException(403, "只能查看自己的项目")
        else:
            raise HTTPException(403, "无权查看")

    extra = p.extra or {}
    # 销售
    r2 = await db.execute(select(models.SalesLedger).where(models.SalesLedger.project_id == pid))
    led = r2.scalar_one_or_none()
    sales_name = None
    if led and led.sales_user:
        sales_name = led.sales_user.full_name or led.sales_user.username

    # 三部门任务
    r2 = await db.execute(select(models.DeptOrder).where(models.DeptOrder.project_id == pid))
    orders = list(r2.scalars().all())
    depts_out = []
    active_orders = [o for o in orders if o.status != "voided"]
    for dept, cfg in DEPTS.items():
        ds = [o for o in orders if o.dept == dept and o.status != "voided"]
        if not ds:
            depts_out.append(WfDept(dept=dept, name=cfg["name"], status="none"))
            continue
        # 取最新一单代表（一般一个项目一部门一单）
        o = ds[-1]
        eff, _ot, _od = compute_efficiency(o.start_date, o.due_date, o.done_date)
        depts_out.append(WfDept(
            dept=dept, name=cfg["name"], status=o.status,
            worker_name=(o.worker.full_name or o.worker.username) if o.worker else None,
            due_date=o.due_date, done_date=o.done_date,
            eff_pct=eff if o.status == "done" else None,
        ))

    # 下游产物计数
    async def _count(biz_type: str, kind: Optional[str] = None) -> int:
        q = select(models.Attachment).where(
            models.Attachment.project_id == pid, models.Attachment.biz_type == biz_type)
        if kind:
            q = q.where(models.Attachment.kind == kind)
        r3 = await db.execute(q)
        return len(r3.scalars().all())

    sheetpkg = await _count("order_start_output", "sheetpkg")
    plist = await _count("order_start_output", "plist")
    shiplist = await _count("ship_list")

    # 发货闸门
    from .logistics_router import _gate
    r2 = await db.execute(select(models.Shipment).where(models.Shipment.project_id == pid))
    sh = r2.scalar_one_or_none()
    can, missing = _gate(active_orders)

    return WfOut(
        project_id=pid, code=p.code, name=p.name, status=p.status,
        sales_name=sales_name,
        sign_date=extra.get("__o__签订日期"), deliver_date=extra.get("__o__交货日期"),
        depts=depts_out,
        sheetpkg_count=sheetpkg, purchase_list_count=plist, ship_list_count=shiplist,
        ship_status=sh.status if sh else "pending",
        can_ship=bool(sh and sh.status == "pending" and can),
        gate_missing=missing if (sh and sh.status == "pending") else [],
    )


# ==================== 装配前置三表状态（§十七） ====================
class SheetStatusRow(BaseModel):
    project_id: int
    code: str
    name: str
    sheets: dict[str, bool]   # {钣金装配: True, 标准件清单: False, 外协外购: ...}


@router.get("/assembly/sheet-status", response_model=List[SheetStatusRow])
async def assembly_sheet_status(
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """装配前置三表完成情况：装配工人看被派任务的项目；生产主管/管理层看全部有生产任务的项目。"""
    code = current.role.code if current.role else ""
    # 范围：装配工人 = 自己被派produce任务的项目；pm_lead/manager/admin = 有produce任务的项目
    q = select(models.DeptOrder.project_id).where(models.DeptOrder.dept == "produce",
                                                   models.DeptOrder.status != "voided")
    if code == "assembler":
        q = q.where(models.DeptOrder.worker_id == current.id)
    elif code in ("pm_lead", "manager", "admin"):
        pass
    else:
        return []
    r = await db.execute(q.distinct())
    pids = [x[0] for x in r.all()]
    if not pids:
        return []

    r = await db.execute(select(models.Project).where(
        models.Project.id.in_(pids), models.Project.is_deleted == False))  # noqa: E712
    projs = {p.id: p for p in r.scalars().all()}

    r = await db.execute(select(models.Datasheet).where(
        models.Datasheet.project_id.in_(pids),
        models.Datasheet.name.in_(ASSEMBLY_PRECHECK_SHEETS)))
    done_by_pid: dict[int, dict[str, bool]] = {}
    for d in r.scalars().all():
        done_by_pid.setdefault(d.project_id, {})[d.name] = bool(d.done_flag)

    rows = []
    for pid in pids:
        p = projs.get(pid)
        if not p:
            continue
        sheets = {s: done_by_pid.get(pid, {}).get(s, False) for s in ASSEMBLY_PRECHECK_SHEETS}
        rows.append(SheetStatusRow(project_id=pid, code=p.code, name=p.name, sheets=sheets))
    rows.sort(key=lambda x: x.code)
    return rows


class DoneFlagIn(BaseModel):
    done: bool


@router.put("/datasheets/{did}/done-flag", response_model=schemas.Msg)
async def set_done_flag(
    did: int, data: DoneFlagIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """标记/取消装配前置表"已完成"（§十七：管理层/生产主管/设计师可标记）。"""
    code = current.role.code if current.role else ""
    if code not in ("admin", "manager", "pm_lead", "designer", "design_lead"):
        raise HTTPException(403, "无权标记数据表完成状态")
    res = await db.execute(select(models.Datasheet).where(models.Datasheet.id == did))
    d = res.scalar_one_or_none()
    if not d:
        raise HTTPException(404, "数据表不存在")
    if d.name not in ASSEMBLY_PRECHECK_SHEETS:
        raise HTTPException(400, "仅装配前置三表（钣金装配/标准件清单/外协外购）支持完成标记")
    d.done_flag = data.done
    d.done_at = datetime.now(timezone.utc) if data.done else None
    await db.commit()
    await write_audit(db, user=current, action="sheet_done_flag", target_type="datasheet",
                      target_id=did, detail=f"{d.name}={'完成' if data.done else '进行中'}")
    return schemas.Msg(message="已标记完成" if data.done else "已取消完成")
