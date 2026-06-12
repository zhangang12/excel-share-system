"""🆕 v3 物流发货部（M08）：发货看板 + D5 发货闸门 + 确认发货回传销售台账。

- 看板九列：项目/名称/设计资料/电工资料/生产状态(E3 无产物)/仓库发货清单/收货信息/状态/闸门
- D5 闸门：该项目「已下单(非作废)」任务全部完成且至少一单才可发；未下单部门不阻塞
- E1 一项目一次发货；E2 收货信息销售录入为权威、物流可修正（P-23 留痕）
- 确认发货：必传发货单 → status=shipped → 回写 sales_ledger.ship_date（销售台账只读列）
- 存量兜底：零任务单的存量项目闸门不通过，管理层可 force 强制发货
"""
from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user, require_roles
from ..dept_config import DEPTS
from ..notify import push_message
from ..utils import write_audit
from .attachments_router import save_upload

router = APIRouter(prefix="/api/logistics", tags=["物流发货部"])


class DeptState(BaseModel):
    state: str   # none 未下单 / doing 进行中 / done 已完成
    label: str


class BoardRow(BaseModel):
    id: int
    project_id: int
    code: str
    name: str
    status: str                      # pending / shipped
    design_files: list[schemas.AttachmentOut] = []
    electric_files: list[schemas.AttachmentOut] = []
    produce_state: DeptState
    design_state: DeptState
    electric_state: DeptState
    ship_list_files: list[schemas.AttachmentOut] = []
    receiver_name: Optional[str] = None
    receiver_phone: Optional[str] = None
    receiver_addr: Optional[str] = None
    ship_doc_name: Optional[str] = None
    ship_doc_id: Optional[int] = None
    shipped_at: Optional[datetime] = None
    can_ship: bool = False
    gate_missing: list[str] = []     # 闸门缺口部门名


class ReceiverIn(BaseModel):
    name: str = ""
    phone: str = ""
    addr: str = ""


def _dept_state(orders: list[models.DeptOrder], dept: str) -> DeptState:
    os_ = [o for o in orders if o.dept == dept and o.status != "voided"]
    if not os_:
        return DeptState(state="none", label="未下单")
    if all(o.status == "done" for o in os_):
        return DeptState(state="done", label="已完成")
    return DeptState(state="doing", label="进行中")


def _gate(orders: list[models.DeptOrder]) -> tuple[bool, list[str]]:
    """D5：已下单(非作废)任务全 done 且至少一单。返回 (可发, 缺口部门名)。"""
    active = [o for o in orders if o.status != "voided"]
    if not active:
        return False, ["未下任何任务单"]
    missing = []
    for dept, cfg in DEPTS.items():
        ds = [o for o in active if o.dept == dept]
        if ds and not all(o.status == "done" for o in ds):
            missing.append(cfg["name"])
    return (not missing), missing


@router.get("/board", response_model=List[BoardRow])
async def board(
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """发货看板（物流/管理层；其它角色只读不限制——数据无敏感金额）。"""
    res = await db.execute(
        select(models.Shipment).join(models.Project).where(
            models.Project.is_deleted == False)  # noqa: E712
        .order_by(models.Shipment.id.desc()).limit(300)
    )
    ships = list(res.scalars().all())
    pids = [s.project_id for s in ships]
    if not pids:
        return []

    # 任务单批量
    res = await db.execute(select(models.DeptOrder).where(
        models.DeptOrder.project_id.in_(pids)))
    orders_by_pid: dict[int, list[models.DeptOrder]] = {}
    order_ids = []
    order_dept: dict[int, str] = {}
    for o in res.scalars().all():
        orders_by_pid.setdefault(o.project_id, []).append(o)
        order_ids.append(o.id)
        order_dept[o.id] = o.dept

    # 产物附件批量（设计/电工完成产物 → 物流资料列）
    files_by_pid_dept: dict[tuple[int, str], list] = {}
    if order_ids:
        res = await db.execute(select(models.Attachment).where(
            models.Attachment.biz_type == "order_output",
            models.Attachment.biz_id.in_(order_ids)))
        for a in res.scalars().all():
            d = order_dept.get(a.biz_id)
            if d in ("design", "electric"):
                files_by_pid_dept.setdefault((a.project_id, d), []).append(
                    schemas.AttachmentOut.model_validate(a))

    # 仓库发货清单（M07 上传 biz_type=ship_list）
    res = await db.execute(select(models.Attachment).where(
        models.Attachment.biz_type == "ship_list",
        models.Attachment.project_id.in_(pids)))
    shiplist_by_pid: dict[int, list] = {}
    for a in res.scalars().all():
        shiplist_by_pid.setdefault(a.project_id, []).append(
            schemas.AttachmentOut.model_validate(a))

    # 发货单附件名
    doc_ids = [s.ship_doc_file_id for s in ships if s.ship_doc_file_id]
    doc_names: dict[int, str] = {}
    if doc_ids:
        res = await db.execute(select(models.Attachment).where(
            models.Attachment.id.in_(doc_ids)))
        doc_names = {a.id: a.name for a in res.scalars().all()}

    rows = []
    for s in ships:
        orders = orders_by_pid.get(s.project_id, [])
        can, missing = _gate(orders)
        rows.append(BoardRow(
            id=s.id, project_id=s.project_id,
            code=s.project.code, name=s.project.name, status=s.status,
            design_files=files_by_pid_dept.get((s.project_id, "design"), []),
            electric_files=files_by_pid_dept.get((s.project_id, "electric"), []),
            design_state=_dept_state(orders, "design"),
            electric_state=_dept_state(orders, "electric"),
            produce_state=_dept_state(orders, "produce"),
            ship_list_files=shiplist_by_pid.get(s.project_id, []),
            receiver_name=s.receiver_name, receiver_phone=s.receiver_phone,
            receiver_addr=s.receiver_addr,
            ship_doc_name=doc_names.get(s.ship_doc_file_id),
            ship_doc_id=s.ship_doc_file_id,
            shipped_at=s.shipped_at,
            can_ship=(s.status == "pending" and can),
            gate_missing=missing if s.status == "pending" else [],
        ))
    return rows


@router.get("/pending-count")
async def pending_count(
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(func.count(models.Shipment.id)).join(models.Project).where(
            models.Shipment.status == "pending",
            models.Project.is_deleted == False,  # noqa: E712
        )
    )
    return {"count": res.scalar() or 0}


@router.put("/{sid}/receiver", response_model=schemas.Msg)
async def update_receiver(
    sid: int, data: ReceiverIn,
    current: models.User = Depends(require_roles("logistics", "sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """收货信息维护：销售录入为权威初值，物流可补充修正（P-23 审计留痕）。"""
    res = await db.execute(select(models.Shipment).where(models.Shipment.id == sid))
    s = res.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "发货单不存在")
    old = f"{s.receiver_name or ''}/{s.receiver_phone or ''}/{s.receiver_addr or ''}"
    s.receiver_name = data.name.strip() or None
    s.receiver_phone = data.phone.strip() or None
    s.receiver_addr = data.addr.strip() or None
    await db.commit()
    await write_audit(db, user=current, action="update_receiver", target_type="shipment",
                      target_id=sid, detail=f"{old} → {data.name}/{data.phone}/{data.addr}")
    return schemas.Msg(message="收货信息已保存")


@router.post("/{sid}/ship", response_model=schemas.Msg)
async def confirm_ship(
    sid: int,
    file: UploadFile = File(...),
    force: bool = Form(False),
    current: models.User = Depends(require_roles("logistics")),
    db: AsyncSession = Depends(get_db),
):
    """确认发货：必传发货单；服务端重算 D5 闸门（防前端绕过）；
    回写 sales_ledger.ship_date；E1 重复发货拒绝。
    存量零任务单项目：仅管理层可 force 强制发货。"""
    res = await db.execute(select(models.Shipment).where(models.Shipment.id == sid))
    s = res.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "发货单不存在")
    if s.status == "shipped":
        raise HTTPException(400, "该项目已发货（E1 一项目一次发货）")

    res = await db.execute(select(models.DeptOrder).where(
        models.DeptOrder.project_id == s.project_id))
    can, missing = _gate(list(res.scalars().all()))
    is_mgr = current.role and current.role.code in ("admin", "manager")
    if not can and not (force and is_mgr):
        raise HTTPException(400, f"发货闸门未通过：{('、'.join(missing))} 未完成（D5：已下单任务须全部完成）")

    a = await save_upload(db, file, biz_type="ship_doc", biz_id=s.id,
                          project_id=s.project_id, user=current)
    today_s = date.today().isoformat()
    s.status = "shipped"
    s.ship_doc_file_id = a.id
    s.shipped_at = datetime.now(timezone.utc)
    s.shipped_by = current.id

    # 回写销售台账发货日期（只读列）
    res = await db.execute(select(models.SalesLedger).where(
        models.SalesLedger.project_id == s.project_id))
    led = res.scalar_one_or_none()
    sales_uid = None
    if led:
        led.ship_date = today_s
        sales_uid = led.sales_uid
    await db.commit()

    code = s.project.code
    if sales_uid:
        await push_message(db, to_user_id=sales_uid, kind="wx",
                           text=f"【已发货】{code} 已发货，发货日期 {today_s} 已回传你的销售台账。",
                           biz_type="shipment", biz_id=s.id)
    await push_message(db, to_role="sales_lead", kind="info",
                       text=f"【已发货】{code} 已发货（{today_s}）。",
                       biz_type="shipment", biz_id=s.id)
    await write_audit(db, user=current, action="ship", target_type="shipment",
                      target_id=sid, detail=f"{code} {today_s}{' FORCE' if force and not can else ''}")
    return schemas.Msg(message=f"{code} 已发货，发货日期已回传销售台账")
