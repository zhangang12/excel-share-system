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
from sqlalchemy import select, or_, func

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
    return u.has_role("admin", "manager")


def _is_sales_lead(u: models.User) -> bool:
    return u.has_role("sales_lead")


def _is_sales(u: models.User) -> bool:
    return u.has_role("sales")


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


# ==================== 销售员名单（台账编辑下拉用） ====================
@router.get("/salespeople")
async def list_salespeople(
    current: models.User = Depends(require_roles("sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """可分配的销售员名单：拥有 sales/sales_lead 角色的在职用户。

    修复「新建销售员未挂台账前，编辑弹窗下拉看不到」——下拉应来自真实用户名单，
    而非已有台账行聚合。多角色感知：锚点角色或多角色关联命中任一即算销售员。
    """
    SALES_CODES = ("sales", "sales_lead")
    rres = await db.execute(select(models.Role.id).where(models.Role.code.in_(SALES_CODES)))
    role_ids = [r[0] for r in rres.all()]
    if not role_ids:
        return []
    sub = select(models.UserRole.user_id).where(models.UserRole.role_id.in_(role_ids))
    res = await db.execute(
        select(models.User).where(
            models.User.is_active == True,  # noqa: E712
            or_(models.User.role_id.in_(role_ids), models.User.id.in_(sub)),
        ).order_by(models.User.id)
    )
    return [{"id": u.id, "name": _uname(u)} for u in res.scalars().all()]


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
            invoice_batch_id=l.invoice_batch_id,
            void_state=l.void_state, void_reason=l.void_reason,
            invoice_apply_file_id=l.invoice_apply_file_id,
            invoice_apply_file_name=names.get(l.invoice_apply_file_id),
            invoice_file_id=l.invoice_file_id,
            invoice_file_name=names.get(l.invoice_file_id),
            prepay=l.prepay or 0, before_ship=l.before_ship or 0,
            prepay_note=l.prepay_note, before_ship_note=l.before_ship_note,
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
    balance_month: Optional[str] = Query(None),   # 🆕 尾款日期筛选(YYYY-MM)
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
    if balance_month:  # 🆕 尾款日期按月筛选(YYYY-MM)；balance_date 存 'YYYY-MM-DD'
        q = q.where(models.SalesLedger.balance_date.like(f"{balance_month.strip()}%"))

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

    # 🆕 项目编号：优先人工输入（前端已改为必填、取消自动生成）；
    #    留空时回退旧的自动编号 + 可选后缀（向后兼容，仅作安全网，UI 不会走此分支）。
    code = (data.code or "").strip()
    if code:
        if len(code) > 64:
            raise HTTPException(400, "项目编号过长（≤64 字符）")
    else:
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
        prepay_note=(data.prepay_note or "").strip() or None,
        before_ship_note=(data.before_ship_note or "").strip() or None,
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
    for f in ("prepay_note", "before_ship_note"):
        v = getattr(data, f)
        if v is not None:
            setattr(led, f, v.strip() or None)
    if data.balance_date is not None:
        led.balance_date = normalize_date_str(data.balance_date) or None
    # 🆕 销售员改派（重新指定台账归属销售）
    if data.sales_uid is not None:
        led.sales_uid = data.sales_uid or None
    # 🆕 下单日期(=合同签订日期)/交货日期 维护：回写项目一览(__o__ + alias __h__下单日期)，
    #    与「上传合同」同源，免去只能靠上传合同才能改日期
    if data.sign_date is not None and led.project:
        _writeback_overview(led.project, "签订日期", normalize_date_str(data.sign_date) or "")
    if data.deliver_date is not None and led.project:
        _writeback_overview(led.project, "交货日期", normalize_date_str(data.deliver_date) or "")
    await db.commit()
    await write_audit(db, user=current, action="update", target_type="sales_ledger", target_id=lid)
    return schemas.Msg(message="已保存")


@router.put("/ledger/{lid}/payment-note", response_model=schemas.Msg)
async def update_payment_note(
    lid: int, data: schemas.PaymentNoteUpdate,
    current: models.User = Depends(require_roles("sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """🆕 收款批注（预付 / 发货前付）独立更新——销售本人即可记录收款时间等，
    不受开票流程金额锁限制。行级隔离：销售员仅本人台账行。"""
    if data.field not in ("prepay", "before_ship"):
        raise HTTPException(400, "field 仅支持 prepay / before_ship")
    led = await _ledger_or_404(db, lid)
    # 销售员仅可批注本人台账行（主管/管理层全量）
    if not _all_view(current) and led.sales_uid != current.id:
        raise HTTPException(403, "只能批注本人负责的台账行")
    col = "prepay_note" if data.field == "prepay" else "before_ship_note"
    setattr(led, col, (data.note or "").strip() or None)
    await db.commit()
    await write_audit(db, user=current, action="update", target_type="sales_ledger",
                      target_id=lid, detail=f"{data.field}_note")
    return schemas.Msg(message="批注已保存")


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
    # 🆕 #1/#104 不开票项目(税票="/")不应进入开票流（只拦显式不开票，不误伤遗留空税率）
    if (led.tax_rate or "").strip() == "/":
        raise HTTPException(400, "该项目税票为「不开票」，无需开票申请")
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
    if led.invoice_batch_id is not None:
        raise HTTPException(400, "该项目属于合并开票批次，请使用合并审批")
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
    if led.invoice_batch_id is not None:
        raise HTTPException(400, "该项目属于合并开票批次，请使用合并驳回")
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
    if led.invoice_batch_id is not None:
        raise HTTPException(400, "该项目属于合并开票批次，请使用合并开票上传")
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


@router.post("/ledger/{lid}/invoice-revoke", response_model=schemas.Msg)
async def invoice_revoke(
    lid: int,
    current: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    """🆕 #2 财务开票纠错出口：已开票(invoiced)退回待开票(pending_invoice)，
    删除错误发票文件，便于重新上传正确发票（开错票的闭环）。"""
    led = await _ledger_or_404(db, lid)
    if led.invoice_batch_id is not None:
        raise HTTPException(400, "该项目属于合并开票批次，暂不支持单项目作废")
    if led.invoice_state != "invoiced":
        raise HTTPException(400, "仅已开票项目可作废重开")
    if led.invoice_file_id:
        res = await db.execute(select(models.Attachment).where(
            models.Attachment.id == led.invoice_file_id))
        a = res.scalar_one_or_none()
        if a:
            await delete_attachment_file(db, a)
    led.invoice_file_id = None
    led.invoice_state = "pending_invoice"
    await db.commit()
    p = led.project
    if led.sales_uid:
        await push_message(db, to_user_id=led.sales_uid, kind="warn",
                           text=f"【发票作废】{p.code} 财务作废了原发票并将重新开票，请留意。",
                           biz_type="sales_ledger", biz_id=led.id)
    await write_audit(db, user=current, action="invoice_revoke",
                      target_type="sales_ledger", target_id=lid)
    return schemas.Msg(message="已作废原发票，退回待开票，可重新上传")


# ==================== 🆕 合并开票（同客户多项目，一份申请 + 一张合并发票，整组一次审批/开票） ====================
# 口径(用户 2026-06-17 确认)：必须同一客户才能合并；一份开票申请、一张合并发票，主管/财务整组一次处理。
# 沿用逐项目开票状态机(applying→pending_invoice→invoiced)，仅以 invoice_batch_id 把多条 ledger 绑为一批。
def _parse_ids(raw: str) -> list[int]:
    """解析逗号分隔的台账 id（容错中文逗号），去重保序。"""
    out: list[int] = []
    for tok in (raw or "").replace("，", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(int(tok))
        except ValueError:
            raise HTTPException(400, f"非法的项目 id：{tok}")
    return list(dict.fromkeys(out))


async def _batch_ledgers(db: AsyncSession, batch_id: int,
                         state: Optional[str] = None) -> list[models.SalesLedger]:
    q = select(models.SalesLedger).where(models.SalesLedger.invoice_batch_id == batch_id)
    if state is not None:
        q = q.where(models.SalesLedger.invoice_state == state)
    res = await db.execute(q.order_by(models.SalesLedger.id))
    return list(res.scalars().all())


def _codes(leds: list[models.SalesLedger]) -> str:
    return "、".join(sorted((l.project.code if l.project else f"#{l.project_id}") for l in leds))


@router.post("/invoice-apply-merge", response_model=schemas.Msg)
async def invoice_apply_merge(
    ledger_ids: str = Form(...),         # 逗号分隔的台账 id，如 "12,15"
    file: UploadFile = File(...),
    current: models.User = Depends(require_roles("sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """合并开票申请：勾选同一客户的多个项目(≥2)，上传一份合并开票申请表，
    生成一个 invoice_batch_id，整组进入「待主管审批」。"""
    ids = _parse_ids(ledger_ids)
    if len(ids) < 2:
        raise HTTPException(400, "合并开票至少选择 2 个项目")
    res = await db.execute(select(models.SalesLedger).where(models.SalesLedger.id.in_(ids)))
    leds = list(res.scalars().all())
    if len(leds) != len(ids):
        raise HTTPException(404, "部分台账行不存在")
    # 销售本人只能合并自己的订单（主管/管理层不受限）
    if _is_sales(current) and not _all_view(current):
        if any(l.sales_uid != current.id for l in leds):
            raise HTTPException(403, "只能合并自己的订单")
    # 必须同一客户（非空且一致）
    customers = {(l.customer or "").strip() for l in leds}
    if len(customers) != 1 or "" in customers:
        raise HTTPException(400, "合并开票要求所选项目为同一客户")
    # 状态校验：均未进入开票流、均非「不开票」
    for l in leds:
        if (l.tax_rate or "").strip() == "/":
            raise HTTPException(400, "所选项目含「不开票」(税票=/)，不能合并开票")
        if l.invoice_state == "invoiced":
            raise HTTPException(400, "所选项目中有已开票，不能再次申请")
        if l.invoice_state in ("applying", "pending_invoice"):
            raise HTTPException(400, "所选项目中有开票申请已在流程中")
    # 生成批次号（单容器低并发，max+1 足够）
    res = await db.execute(select(func.max(models.SalesLedger.invoice_batch_id)))
    batch_id = (res.scalar() or 0) + 1
    # 一份合并申请文件，挂在批次首个台账上，组内共享其 id
    a = await save_upload(db, file, biz_type="invoice_apply", biz_id=leds[0].id,
                          project_id=leds[0].project_id, user=current)
    for l in leds:
        l.invoice_state = "applying"
        l.invoice_batch_id = batch_id
        l.invoice_apply_file_id = a.id
    await db.commit()
    cust = customers.pop()
    total = sum(l.amount or 0 for l in leds)
    await push_message(db, to_role="sales_lead", kind="info",
                       text=f"【合并开票申请】{cust} 项目 {_codes(leds)} 共 {len(leds)} 个，"
                            f"合计 ¥{total:,.0f}，待销售主管审批：{a.name}",
                       biz_type="invoice_batch", biz_id=batch_id)
    await write_audit(db, user=current, action="invoice_apply_merge",
                      target_type="invoice_batch", target_id=batch_id)
    return schemas.Msg(message=f"合并开票申请已提交（{len(leds)} 个项目），等待销售主管审批")


@router.post("/invoice-batch/{batch_id}/approve", response_model=schemas.Msg)
async def invoice_batch_approve(
    batch_id: int,
    current: models.User = Depends(require_roles("sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    leds = await _batch_ledgers(db, batch_id, "applying")
    if not leds:
        raise HTTPException(400, "该合并批次不在待审批状态")
    for l in leds:
        l.invoice_state = "pending_invoice"
    await db.commit()
    cust = (leds[0].customer or "").strip()
    total = sum(l.amount or 0 for l in leds)
    await push_message(db, to_role="finance", kind="info",
                       text=f"【待开票·合并】{cust} 项目 {_codes(leds)} 共 {len(leds)} 个，"
                            f"合计 ¥{total:,.0f}，请财务合并开具一张发票。",
                       biz_type="invoice_batch", biz_id=batch_id)
    await write_audit(db, user=current, action="invoice_batch_approve",
                      target_type="invoice_batch", target_id=batch_id)
    return schemas.Msg(message=f"已通过合并开票（{len(leds)} 个项目），已推送财务部")


@router.post("/invoice-batch/{batch_id}/reject", response_model=schemas.Msg)
async def invoice_batch_reject(
    batch_id: int,
    current: models.User = Depends(require_roles("sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    leds = await _batch_ledgers(db, batch_id, "applying")
    if not leds:
        raise HTTPException(400, "该合并批次不在待审批状态")
    # 组内共享的申请文件只删一次
    apply_fid = next((l.invoice_apply_file_id for l in leds if l.invoice_apply_file_id), None)
    if apply_fid:
        res = await db.execute(select(models.Attachment).where(models.Attachment.id == apply_fid))
        a = res.scalar_one_or_none()
        if a:
            await delete_attachment_file(db, a)
    sales_uid = leds[0].sales_uid
    codes = _codes(leds)
    for l in leds:
        l.invoice_state = None
        l.invoice_batch_id = None
        l.invoice_apply_file_id = None
    await db.commit()
    if sales_uid:
        await push_message(db, to_user_id=sales_uid, kind="warn",
                           text=f"【合并开票驳回】项目 {codes} 的合并开票申请被销售主管驳回，可修改后重新申请。",
                           biz_type="invoice_batch", biz_id=batch_id)
    await write_audit(db, user=current, action="invoice_batch_reject",
                      target_type="invoice_batch", target_id=batch_id)
    return schemas.Msg(message="已驳回合并开票申请")


@router.post("/invoice-batch/{batch_id}/invoice-upload", response_model=schemas.Msg)
async def invoice_batch_upload(
    batch_id: int,
    file: UploadFile = File(...),
    current: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    """财务上传一张合并发票，整组置为已开票，组内共享同一发票文件。"""
    leds = await _batch_ledgers(db, batch_id, "pending_invoice")
    if not leds:
        raise HTTPException(400, "该合并批次不在待开票状态")
    a = await save_upload(db, file, biz_type="invoice", biz_id=leds[0].id,
                          project_id=leds[0].project_id, user=current)
    for l in leds:
        l.invoice_state = "invoiced"
        l.invoice_file_id = a.id
    await db.commit()
    sales_uid = leds[0].sales_uid
    codes = _codes(leds)
    if sales_uid:
        await push_message(db, to_user_id=sales_uid, kind="wx",
                           text=f"【合并发票已开】项目 {codes} 财务已开具合并发票并回传：{a.name}",
                           biz_type="invoice_batch", biz_id=batch_id)
    await write_audit(db, user=current, action="invoice_batch_upload",
                      target_type="invoice_batch", target_id=batch_id)
    return schemas.Msg(message=f"合并发票已上传（{len(leds)} 个项目已开票）")


# ==================== 🆕 销售订单作废（销售员申请 → 销售负责人审批 → 软删项目+各部门流程） ====================
# 口径(用户 2026-06-18)：销售员申请、负责人审批(负责人可一键直接作废)；已开票/已发货禁止作废；
# 软删可追溯；必填原因；通过后仅通知管理层。项目软删后各列表(目录/详单/各部门工作台)按 is_deleted 自动隐藏。
async def _assert_voidable(db: AsyncSession, led: models.SalesLedger) -> None:
    """作废前置校验：已开票/已发货禁止；开票流程进行中先处理。"""
    if led.invoice_state == "invoiced":
        raise HTTPException(400, "该订单已开票，不可作废")
    if led.invoice_state in ("applying", "pending_invoice"):
        raise HTTPException(400, "该订单开票流程进行中，请先处理/驳回开票后再作废")
    sr = await db.execute(select(models.Shipment).where(
        models.Shipment.project_id == led.project_id,
        models.Shipment.status == "shipped"))
    if sr.scalar_one_or_none() is not None:
        raise HTTPException(400, "该订单已发货，不可作废")


async def _execute_void(db: AsyncSession, led: models.SalesLedger,
                        actor: models.User, reason: str) -> None:
    """执行作废：软删项目 + 清派生数据 + 各部门任务单置作废；台账记 voided。仅通知管理层。"""
    from .projects_router import soft_delete_project
    res = await db.execute(select(models.Project).where(models.Project.id == led.project_id))
    p = res.scalar_one_or_none()
    orig_code = p.code if p else f"#{led.project_id}"
    pname = p.name if p else ""
    led.void_state = "voided"
    led.void_reason = reason
    counts: dict = {}
    if p and not p.is_deleted:
        counts = await soft_delete_project(db, p, void_dept_orders=True)
    await db.commit()
    # 仅通知管理层（口径 2026-06-18）
    await push_message(
        db, to_role="manager", kind="warn",
        text=f"【订单已作废】{orig_code} {pname} 已作废（原因：{reason or '—'}），"
             f"项目目录/详单/各部门任务已同步移除。",
        biz_type="order_void", biz_id=led.id)
    await write_audit(db, user=actor, action="void_sales_order",
                      target_type="sales_ledger", target_id=led.id,
                      detail=f"{orig_code} · 原因:{reason} · {counts}")


@router.post("/ledger/{lid}/void-apply", response_model=schemas.Msg)
async def void_apply(
    lid: int,
    data: schemas.VoidApplyIn,
    current: models.User = Depends(require_roles("sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """销售员发起订单作废申请（→待负责人审批）；销售负责人/管理层调用则一键直接作废。"""
    led = await _ledger_or_404(db, lid)
    if _is_sales(current) and not _all_view(current) and led.sales_uid != current.id:
        raise HTTPException(403, "只能作废自己的订单")
    if led.void_state == "applying":
        raise HTTPException(400, "该订单作废申请已在审批中")
    if led.void_state == "voided":
        raise HTTPException(400, "该订单已作废")
    reason = (data.reason or "").strip()
    if not reason:
        raise HTTPException(400, "请填写作废原因")
    await _assert_voidable(db, led)
    # 销售负责人/管理层：一键直接作废，无需再审批
    if _all_view(current):
        await _execute_void(db, led, current, reason)
        return schemas.Msg(message="订单已作废，项目及各部门流程已同步移除")
    # 销售员：进入待审批
    led.void_state = "applying"
    led.void_reason = reason
    await db.commit()
    p = led.project
    await push_message(db, to_role="sales_lead", kind="warn",
                       text=f"【作废申请】{p.code if p else ''} {p.name if p else ''} "
                            f"申请作废，原因：{reason}，待销售负责人审批。",
                       biz_type="order_void", biz_id=led.id)
    await write_audit(db, user=current, action="void_apply",
                      target_type="sales_ledger", target_id=lid)
    return schemas.Msg(message="作废申请已提交，等待销售负责人审批")


@router.get("/void-approvals", response_model=schemas.SalesLedgerListOut)
async def void_approvals(
    current: models.User = Depends(require_roles("sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(models.SalesLedger).where(models.SalesLedger.void_state == "applying")
        .order_by(models.SalesLedger.id.desc())
    )
    return schemas.SalesLedgerListOut(rows=await _ledger_rows(db, list(res.scalars().all())))


@router.post("/ledger/{lid}/void-approve", response_model=schemas.Msg)
async def void_approve(
    lid: int,
    current: models.User = Depends(require_roles("sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    led = await _ledger_or_404(db, lid)
    if led.void_state != "applying":
        raise HTTPException(400, "该订单不在待作废审批状态")
    await _assert_voidable(db, led)  # 申请到审批之间状态可能变化，再校验一次
    await _execute_void(db, led, current, led.void_reason or "")
    return schemas.Msg(message="已通过，订单已作废，项目及各部门流程已同步移除")


@router.post("/ledger/{lid}/void-reject", response_model=schemas.Msg)
async def void_reject(
    lid: int,
    current: models.User = Depends(require_roles("sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    led = await _ledger_or_404(db, lid)
    if led.void_state != "applying":
        raise HTTPException(400, "该订单不在待作废审批状态")
    led.void_state = None
    led.void_reason = None
    await db.commit()
    if led.sales_uid:
        p = led.project
        await push_message(db, to_user_id=led.sales_uid, kind="info",
                           text=f"【作废驳回】{p.code if p else ''} 作废申请被销售负责人驳回，订单仍有效。",
                           biz_type="order_void", biz_id=led.id)
    await write_audit(db, user=current, action="void_reject",
                      target_type="sales_ledger", target_id=lid)
    return schemas.Msg(message="已驳回作废申请")
