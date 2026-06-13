"""🆕 v3 销售部：销售台账（§十三 19 列）+ 销售下单 + 上传合同 + 开票审批流（M02/M03）。

- 行级隔离：销售员仅本人（sales_uid）；销售主管/管理层全量 + 合计
- 销售下单 = 同一事务建 项目(预置模板表+全员成员) + 台账 + 发货待办 + 各部门待派任务，
  提交后推送各部门负责人角色池 + 物流角色池
- 下单日期 = 合同签订日期：上传合同时填签订/交货日期并回写项目一览（__o__ + alias __h__）
- 开票状态机：None 未申请 → applying 待主管审批 → pending_invoice 待财务开票 → invoiced 已开票
  （驳回回到 None 并清申请文件）；发货日期由物流回传只读（M08）
"""
import re
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user, require_roles
from ..notify import push_message
from ..utils import write_audit
from ..dept_config import DEPTS
from ..sheet_templates import normalize_date_str
from .attachments_router import save_upload, delete_attachment_file
from .orders_router import create_order_internal, _writeback_overview
from .projects_router import (
    create_default_template_sheets, _add_all_active_users_as_members,
    OVERVIEW_KEY_PREFIX,
)

router = APIRouter(prefix="/api/sales", tags=["销售部"])

_CODE_RE = re.compile(r"^(\d{4})-0*(\d+)")


def _is_mgr(u: models.User) -> bool:
    return bool(u.role and u.role.code in ("admin", "manager"))


def _is_sales_lead(u: models.User) -> bool:
    return bool(u.role and u.role.code == "sales_lead")


def _is_sales(u: models.User) -> bool:
    return bool(u.role and u.role.code == "sales")


def _all_view(u: models.User) -> bool:
    return _is_mgr(u) or _is_sales_lead(u)


async def _ledger_or_404(db: AsyncSession, lid: int) -> models.SalesLedger:
    res = await db.execute(select(models.SalesLedger).where(models.SalesLedger.id == lid))
    led = res.scalar_one_or_none()
    if not led:
        raise HTTPException(404, "台账行不存在")
    return led


def _uname(u: Optional[models.User]) -> Optional[str]:
    return (u.full_name or u.username) if u else None


async def _ledger_rows(db: AsyncSession, ledgers: list[models.SalesLedger]) -> list[schemas.SalesLedgerRow]:
    # 附件名批量取（合同/开票申请/发票）
    att_ids = set()
    for l in ledgers:
        att_ids.update(x for x in (l.contract_file_id, l.invoice_apply_file_id, l.invoice_file_id) if x)
    names: dict[int, str] = {}
    if att_ids:
        res = await db.execute(select(models.Attachment).where(models.Attachment.id.in_(att_ids)))
        names = {a.id: a.name for a in res.scalars().all()}

    rows = []
    for l in ledgers:
        p = l.project
        extra = (p.extra or {}) if p else {}
        rows.append(schemas.SalesLedgerRow(
            id=l.id, project_id=l.project_id,
            code=p.code if p else "", name=p.name if p else "",
            status=p.status if p else "",
            sales_uid=l.sales_uid, sales_name=_uname(l.sales_user),
            customer=l.customer, cust_type=l.cust_type,
            sign_date=extra.get(f"{OVERVIEW_KEY_PREFIX}签订日期"),
            deliver_date=extra.get(f"{OVERVIEW_KEY_PREFIX}交货日期"),
            contract=l.contract,
            contract_file_id=l.contract_file_id,
            contract_file_name=names.get(l.contract_file_id),
            amount=l.amount or 0, tax_rate=l.tax_rate,
            invoice_state=l.invoice_state,
            invoice_apply_file_id=l.invoice_apply_file_id,
            invoice_apply_file_name=names.get(l.invoice_apply_file_id),
            invoice_file_id=l.invoice_file_id,
            invoice_file_name=names.get(l.invoice_file_id),
            prepay=l.prepay or 0, before_ship=l.before_ship or 0,
            ship_receivable=l.ship_receivable or 0, balance=l.balance or 0,
            balance_date=l.balance_date, ship_date=l.ship_date,
        ))
    return rows


# ==================== 台账 ====================
@router.get("/ledger", response_model=schemas.SalesLedgerListOut)
async def list_ledger(
    kw: Optional[str] = Query(None),
    cust_type: Optional[str] = Query(None),
    contract: Optional[str] = Query(None),
    sales_uid: Optional[int] = Query(None),
    current: models.User = Depends(require_roles("sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    q = select(models.SalesLedger).join(
        models.Project, models.SalesLedger.project_id == models.Project.id
    ).where(models.Project.is_deleted == False)  # noqa: E712

    if not _all_view(current):
        q = q.where(models.SalesLedger.sales_uid == current.id)  # 销售员仅本人
    elif sales_uid:
        q = q.where(models.SalesLedger.sales_uid == sales_uid)
    if cust_type:
        q = q.where(models.SalesLedger.cust_type == cust_type)
    if contract:
        q = q.where(models.SalesLedger.contract == contract)

    res = await db.execute(q.order_by(models.SalesLedger.id.desc()).limit(500))
    ledgers = list(res.scalars().all())
    if kw:
        k = kw.strip()
        ledgers = [l for l in ledgers if l.project and (
            k in l.project.code or k in l.project.name or k in (l.customer or ""))]

    rows = await _ledger_rows(db, ledgers)
    totals = None
    if _all_view(current):
        totals = schemas.SalesLedgerTotals(
            count=len(rows),
            amount=sum(r.amount for r in rows),
            uninvoiced=sum(r.amount for r in rows if r.invoice_state != "invoiced"),
            prepay=sum(r.prepay for r in rows),
            before_ship=sum(r.before_ship for r in rows),
            ship_receivable=sum(r.ship_receivable for r in rows),
            balance=sum(r.balance for r in rows),
        )
    return schemas.SalesLedgerListOut(rows=rows, totals=totals)


@router.get("/next-code", response_model=schemas.NextCodeOut)
async def next_code(
    _: models.User = Depends(require_roles("sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """自动编号：当年-NNN（3 位补零），扫描全部项目编号取当年最大序号 +1（P-21）。"""
    year = datetime.now(timezone.utc).strftime("%Y")
    res = await db.execute(select(models.Project.code))
    mx = 0
    for (code,) in res.all():
        m = _CODE_RE.match(code or "")
        if m and m.group(1) == year:
            mx = max(mx, int(m.group(2)))
    return schemas.NextCodeOut(code=f"{year}-{mx + 1:03d}")


@router.post("/orders", response_model=schemas.SalesOrderOut)
async def create_sales_order(
    data: schemas.SalesOrderCreate,
    current: models.User = Depends(require_roles("sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """销售下单：同一事务建 项目 + 台账 + 发货待办 + 各部门待派任务。"""
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "请填写设备名称")
    depts = [d for d in data.depts if d in DEPTS]
    if not depts:
        raise HTTPException(400, "请至少选择一个派往部门")
    if data.cust_type not in ("经销商", "终端客户"):
        raise HTTPException(400, "客户分类必须是 经销商/终端客户")

    # 编号：自动 + 可选后缀字母；后端唯一校验（P-21 双保险：DB unique + 此处查重）
    base = (await next_code(_=current, db=db)).code
    suffix = (data.code_suffix or "").strip().upper()[:2]
    if suffix and not suffix.isalpha():
        raise HTTPException(400, "编号后缀只能是字母")
    code = base + suffix
    res = await db.execute(select(models.Project).where(models.Project.code == code))
    if res.scalar_one_or_none():
        raise HTTPException(409, f"项目编号 {code} 已存在")

    # 1) 项目（预置模板表 + 全员成员，与目录新建一致）
    p = models.Project(code=code, name=name, status="进行中", manager_id=None)
    db.add(p)
    await db.flush()
    await create_default_template_sheets(db, p.id)
    await _add_all_active_users_as_members(db, p.id)
    _writeback_overview(p, "销售", _uname(current))

    # 2) 台账（未开票金额=合同金额，由 invoice_state 推导不另存）
    led = models.SalesLedger(
        project_id=p.id, sales_uid=current.id,
        customer=data.customer.strip() or None, cust_type=data.cust_type,
        contract=data.contract if data.contract in ("有", "无") else "无",
        amount=data.amount or 0, tax_rate=data.tax_rate,
        prepay=data.prepay or 0, before_ship=data.before_ship or 0,
        ship_receivable=data.ship_receivable or 0, balance=data.balance or 0,
        balance_date=normalize_date_str(data.balance_date) or None,
    )
    db.add(led)

    # 3) 发货待办（E1 一项目一单；E2 收货信息销售录入为权威初值）
    rcv = data.receiver or schemas.SalesReceiverIn()
    db.add(models.Shipment(
        project_id=p.id,
        receiver_name=rcv.name.strip() or None,
        receiver_phone=rcv.phone.strip() or None,
        receiver_addr=rcv.addr.strip() or None,
    ))

    # 4) 各部门待派任务
    order_ids = []
    req = data.req_text.strip() or f"（销售下单）{name}"
    for d in depts:
        o = await create_order_internal(
            db, project=p, dept=d, req_text=req, created_by=current.id)
        order_ids.append(o.id)

    await db.commit()

    # 推送（事务提交后）
    for d in depts:
        await push_message(db, to_role=DEPTS[d]["lead_role"], kind="info",
                           text=f"【销售下单】{code} {name} 新{DEPTS[d]['name']}任务待分派（销售：{_uname(current)}）。",
                           biz_type="project", biz_id=p.id)
    await push_message(db, to_role="logistics", kind="info",
                       text=f"【新项目】{code} {name} 已创建发货待办。",
                       biz_type="project", biz_id=p.id)
    await write_audit(db, user=current, action="create", target_type="sales_order",
                      target_id=p.id, detail=f"{code} 派往{','.join(depts)}")
    return schemas.SalesOrderOut(project_id=p.id, code=code, order_ids=order_ids)


@router.put("/ledger/{lid}", response_model=schemas.Msg)
async def update_ledger(
    lid: int, data: schemas.SalesLedgerUpdate,
    current: models.User = Depends(require_roles("sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """台账编辑（仅销售主管/管理层）；编号与发货日期不可改。"""
    led = await _ledger_or_404(db, lid)
    # 🆕 #105 已进入开票流程(待财务开票/已开票)后，禁改金额/税票——否则与已开发票及推送财务的快照金额脱节
    if led.invoice_state in ("pending_invoice", "invoiced"):
        amt_chg = data.amount is not None and data.amount != led.amount
        tax_chg = data.tax_rate is not None and (data.tax_rate or "").strip() != (led.tax_rate or "")
        if amt_chg or tax_chg:
            raise HTTPException(400, "已进入开票流程，金额/税票不可修改（如需更正请先驳回/作废发票）")
    if data.name is not None and led.project:
        led.project.name = data.name.strip() or led.project.name
    for f in ("customer", "cust_type", "contract", "tax_rate"):
        v = getattr(data, f)
        if v is not None:
            setattr(led, f, v.strip() if isinstance(v, str) else v)
    for f in ("amount", "prepay", "before_ship", "ship_receivable", "balance"):
        v = getattr(data, f)
        if v is not None:
            setattr(led, f, v)
    if data.balance_date is not None:
        led.balance_date = normalize_date_str(data.balance_date) or None
    await db.commit()
    await write_audit(db, user=current, action="update", target_type="sales_ledger", target_id=lid)
    return schemas.Msg(message="已保存")


@router.post("/ledger/{lid}/contract", response_model=schemas.Msg)
async def upload_contract(
    lid: int,
    sign_date: str = Form(...),
    deliver_date: str = Form(...),
    file: UploadFile = File(...),
    current: models.User = Depends(require_roles("sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """上传合同：附件 + 合同签订日期(=下单日期)/交货日期回写台账与项目一览。"""
    led = await _ledger_or_404(db, lid)
    if _is_sales(current) and led.sales_uid != current.id:
        raise HTTPException(403, "只能操作自己的订单")
    sd, dd = normalize_date_str(sign_date), normalize_date_str(deliver_date)
    if not sd or not dd:
        raise HTTPException(400, "请填写合同签订日期与交货日期")

    a = await save_upload(db, file, biz_type="contract", biz_id=led.id,
                          project_id=led.project_id, user=current)
    led.contract = "有"
    led.contract_file_id = a.id
    p = led.project
    _writeback_overview(p, "签订日期", sd)   # alias 自动双写 __h__下单日期
    _writeback_overview(p, "交货日期", dd)
    await db.commit()
    await write_audit(db, user=current, action="upload", target_type="sales_ledger",
                      target_id=lid, detail=f"合同:{a.name} 签订{sd} 交货{dd}")
    return schemas.Msg(message="合同已上传，下单/交货日期已回写台账")


# ==================== 开票审批流（M03） ====================
@router.post("/ledger/{lid}/invoice-apply", response_model=schemas.Msg)
async def invoice_apply(
    lid: int,
    file: UploadFile = File(...),
    current: models.User = Depends(require_roles("sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    led = await _ledger_or_404(db, lid)
    if _is_sales(current) and led.sales_uid != current.id:
        raise HTTPException(403, "只能操作自己的订单")
    if led.invoice_state == "invoiced":
        raise HTTPException(400, "该项目已开票")
    if led.invoice_state in ("applying", "pending_invoice"):
        raise HTTPException(400, "开票申请已在流程中")
    a = await save_upload(db, file, biz_type="invoice_apply", biz_id=led.id,
                          project_id=led.project_id, user=current)
    led.invoice_state = "applying"
    led.invoice_apply_file_id = a.id
    await db.commit()
    p = led.project
    await push_message(db, to_role="sales_lead", kind="info",
                       text=f"【开票申请】{p.code} {p.name} 待销售主管审批：{a.name}",
                       biz_type="sales_ledger", biz_id=led.id)
    await write_audit(db, user=current, action="invoice_apply",
                      target_type="sales_ledger", target_id=lid)
    return schemas.Msg(message="开票申请已提交，等待销售主管审批")


@router.get("/invoice-approvals", response_model=schemas.SalesLedgerListOut)
async def invoice_approvals(
    current: models.User = Depends(require_roles("sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(models.SalesLedger).where(models.SalesLedger.invoice_state == "applying")
        .order_by(models.SalesLedger.id.desc())
    )
    return schemas.SalesLedgerListOut(rows=await _ledger_rows(db, list(res.scalars().all())))


@router.post("/ledger/{lid}/invoice-approve", response_model=schemas.Msg)
async def invoice_approve(
    lid: int,
    current: models.User = Depends(require_roles("sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    led = await _ledger_or_404(db, lid)
    if led.invoice_state != "applying":
        raise HTTPException(400, "该申请不在待审批状态")
    led.invoice_state = "pending_invoice"
    await db.commit()
    p = led.project
    await push_message(db, to_role="finance", kind="info",
                       text=f"【待开票】{p.code} {p.name} 金额 ¥{led.amount:,.0f} 税票 {led.tax_rate or '—'}，请财务开票。",
                       biz_type="sales_ledger", biz_id=led.id)
    await write_audit(db, user=current, action="invoice_approve",
                      target_type="sales_ledger", target_id=lid)
    return schemas.Msg(message="已通过，已推送财务部开票")


@router.post("/ledger/{lid}/invoice-reject", response_model=schemas.Msg)
async def invoice_reject(
    lid: int,
    current: models.User = Depends(require_roles("sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    led = await _ledger_or_404(db, lid)
    if led.invoice_state != "applying":
        raise HTTPException(400, "该申请不在待审批状态")
    # 清申请文件（含磁盘），状态退回未申请
    if led.invoice_apply_file_id:
        res = await db.execute(select(models.Attachment).where(
            models.Attachment.id == led.invoice_apply_file_id))
        a = res.scalar_one_or_none()
        if a:
            await delete_attachment_file(db, a)
    led.invoice_apply_file_id = None
    led.invoice_state = None
    await db.commit()
    if led.sales_uid:
        p = led.project
        await push_message(db, to_user_id=led.sales_uid, kind="warn",
                           text=f"【开票驳回】{p.code} 开票申请被销售主管驳回，可修改后重新申请。",
                           biz_type="sales_ledger", biz_id=led.id)
    await write_audit(db, user=current, action="invoice_reject",
                      target_type="sales_ledger", target_id=lid)
    return schemas.Msg(message="已驳回")


@router.post("/ledger/{lid}/invoice-upload", response_model=schemas.Msg)
async def invoice_upload(
    lid: int,
    file: UploadFile = File(...),
    current: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    """财务上传发票：状态→已开票，发票回传销售订单（发票情况→0）。"""
    led = await _ledger_or_404(db, lid)
    if led.invoice_state != "pending_invoice":
        raise HTTPException(400, "该项目不在待开票状态")
    a = await save_upload(db, file, biz_type="invoice", biz_id=led.id,
                          project_id=led.project_id, user=current)
    led.invoice_state = "invoiced"
    led.invoice_file_id = a.id
    await db.commit()
    p = led.project
    if led.sales_uid:
        await push_message(db, to_user_id=led.sales_uid, kind="wx",
                           text=f"【发票已开】{p.code} 财务已开票并回传你的销售订单：{a.name}",
                           biz_type="sales_ledger", biz_id=led.id)
    await write_audit(db, user=current, action="invoice_upload",
                      target_type="sales_ledger", target_id=lid)
    return schemas.Msg(message="发票已上传，已回传销售订单（发票情况→0）")
