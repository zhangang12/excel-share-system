"""🆕 v3 销售部：销售台账（§十三 19 列）+ 销售下单 + 上传合同 + 开票审批流（M02/M03）。

- 行级隔离：销售员仅本人（sales_uid）；销售主管/管理层全量 + 合计
- 销售下单 = 同一事务建 项目(预置模板表+全员成员) + 台账 + 发货待办 + 各部门待派任务，
  提交后推送各部门负责人角色池 + 物流角色池
- 下单日期 = 合同签订日期：上传合同时填签订/交货日期并回写项目一览（__o__ + alias __h__）
- 开票状态机：None 未申请 → applying 待主管审批 → pending_invoice 待财务开票 → invoiced 已开票
  （驳回回到 None 并清申请文件）；发货日期由物流回传只读（M08）
"""
import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

log = logging.getLogger("sales_router")

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
from ..config import settings
from .attachments_router import save_upload, delete_attachment_file, copy_attachment
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


# 🆕 税票口径(2026-06-18)：不开票由 "/" 改为 "0"；历史 "/" 仍按不开票兼容，迁移统一为 "0"
def _is_no_invoice(tax: Optional[str]) -> bool:
    return (tax or "").strip() in ("/", "0")


# 🆕 项目编号自然排序键（与前端 sortByCode 同序）：标准编号 YYYY-NNN[后缀] 按 年→序号→后缀 在前，其余按字符串在后
_STD_CODE_RE = re.compile(r"^(\d{4})-0*(\d+)([A-Za-z]*)$")
def _ledger_sort_key(l: "models.SalesLedger"):
    code = (l.project.code if l.project else "") or ""
    m = _STD_CODE_RE.match(code)
    if m:
        return (0, int(m.group(1)), int(m.group(2)), m.group(3), "")
    return (1, 0, 0, "", code)


async def _ledger_or_404(db: AsyncSession, lid: int) -> models.SalesLedger:
    res = await db.execute(select(models.SalesLedger).where(models.SalesLedger.id == lid))
    led = res.scalar_one_or_none()
    if not led:
        raise HTTPException(404, "台账行不存在")
    return led


def _uname(u: Optional[models.User]) -> Optional[str]:
    return (u.full_name or u.username) if u else None


# 🆕 解析一览「数量」单元格文本(如 "2台")→ (数量, 单位)，供台账行/草稿预填回带
_QTY_RE = re.compile(r"^\s*(\d+)\s*(台|套)?")
def _parse_qty(s):
    m = _QTY_RE.match(s or "")
    if not m:
        return (None, None)
    return (int(m.group(1)), m.group(2) or "台")


# 🆕 数量+单位(台/套)→ 一览「数量」单元格文本，如 "2台"
def _qty_str(qty, unit) -> str:
    u = unit if unit in ("台", "套") else "台"
    try:
        n = int(qty)
    except (TypeError, ValueError):
        n = 1
    if n < 1:
        n = 1
    return f"{n}{u}"


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
        po = extra.get("__pending_order__") or None  # 🆕 待审批/草稿派单信息(供前端预填)
        _qn, _qu = _parse_qty(extra.get(f"{OVERVIEW_KEY_PREFIX}数量"))
        rows.append(schemas.SalesLedgerRow(
            qty=_qn, unit=_qu,
            order_state=l.order_state,
            order_reject_reason=(po or {}).get("reject_reason") if po else None,
            pending_order={k: po[k] for k in ("depts", "req_text", "receiver") if k in po} if po else None,
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
            order_type=l.order_type,
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
    year: Optional[str] = Query(None),             # 🆕 年份筛选(YYYY)
    page: int = Query(1, ge=1),                   # 🆕 分页（性能优化：只构建当前页的附件名等重数据）
    page_size: int = Query(50, ge=1, le=200),
    current: models.User = Depends(require_roles("sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    q = select(models.SalesLedger).join(
        models.Project, models.SalesLedger.project_id == models.Project.id
    ).where(models.Project.is_deleted == False)  # noqa: E712

    if year:
        q = q.where(models.Project.code.like(f"{year}-%"))
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

    # 取全部匹配实体(含 joined project，轻量)→关键词过滤→自然排序→分页切片；
    # 重数据(_ledger_rows 的附件名查询)只对当前页构建，避免整表 500 行的开销（性能优化）。
    res = await db.execute(q)
    ledgers = list(res.scalars().all())
    if kw:
        k = kw.strip()
        ledgers = [l for l in ledgers if l.project and (
            k in l.project.code or k in l.project.name or k in (l.customer or ""))]
    ledgers.sort(key=_ledger_sort_key)
    total = len(ledgers)

    # 合计针对全集(全部页)，非当前页。销售员看本人合计、主管/管理层看全量合计（口径同各自可见范围）
    totals = schemas.SalesLedgerTotals(
        count=total,
        amount=sum(l.amount or 0 for l in ledgers),
        uninvoiced=sum(l.amount or 0 for l in ledgers if l.invoice_state != "invoiced"),
        prepay=sum(l.prepay or 0 for l in ledgers),
        before_ship=sum(l.before_ship or 0 for l in ledgers),
        ship_receivable=sum(l.ship_receivable or 0 for l in ledgers),
        balance=sum(l.balance or 0 for l in ledgers),
    )

    start = (page - 1) * page_size
    page_ledgers = ledgers[start:start + page_size]
    rows = await _ledger_rows(db, page_ledgers)
    return schemas.SalesLedgerListOut(rows=rows, totals=totals, total=total)


async def _compute_next_code(db: AsyncSession, year: Optional[str] = None) -> str:
    """指定年份(YYYY，留空=当年)的下一个项目编号：{年}-NNN，扫全部项目取该年最大序号 +1。"""
    y = (year or "").strip() or datetime.now(timezone.utc).strftime("%Y")
    res = await db.execute(select(models.Project.code))
    mx = 0
    for (code,) in res.all():
        m = _CODE_RE.match(code or "")
        if m and m.group(1) == y:
            mx = max(mx, int(m.group(2)))
    return f"{y}-{mx + 1:03d}"


@router.get("/next-code", response_model=schemas.NextCodeOut)
async def next_code(
    year: Optional[str] = Query(None),   # 🆕 指定年度(YYYY)；留空=当年。供销售下单选年份后预生成编号
    _: models.User = Depends(require_roles("sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """自动编号：{年}-NNN（3 位补零），扫描全部项目编号取该年最大序号 +1（P-21）。"""
    return schemas.NextCodeOut(code=await _compute_next_code(db, year))


async def _distribute_pending_files(db: AsyncSession, project_id: int,
                                    order_ids: list[int], user_id) -> int:
    """把待审批期间暂存的下单资料(order_input + biz_id NULL)审批通过后转挂到每个部门任务单：
    各部门单独立复制一份(删除互不影响)，分发完删除暂存件。返回分发份数。"""
    if not order_ids:
        return 0
    res = await db.execute(select(models.Attachment).where(
        models.Attachment.biz_type == "order_input",
        models.Attachment.project_id == project_id,
        models.Attachment.biz_id.is_(None),
    ))
    holdings = list(res.scalars().all())
    if not holdings:
        return 0
    n = 0
    for src in holdings:
        for oid in order_ids:
            await copy_attachment(db, src, biz_type="order_input", biz_id=oid,
                                  project_id=project_id, user_id=user_id)
            n += 1
    for src in holdings:
        await delete_attachment_file(db, src)  # 暂存件已分发，清理(物理+行)
    return n


async def _materialize_order_downstream(db: AsyncSession, p: models.Project, depts: list[str],
                                        req_text: str, receiver: tuple, creator: models.User) -> list[int]:
    """下单生效：补全员成员 + 建发货待办 + 各部门待派任务 + 转挂暂存下单资料。返回 order_ids。
    不 commit、不推送。供「主管/管理层直接下单」与「销售员下单经主管审批通过」共用。
    调货订单（depts 为空）：不建发货待办、不派部门任务，只在销售部留档。"""
    await _add_all_active_users_as_members(db, p.id)
    order_ids = []
    if depts:  # 调货订单 depts=[] 时跳过发货待办和部门任务
        name_, phone_, addr_ = receiver
        db.add(models.Shipment(
            project_id=p.id,
            receiver_name=(name_ or "").strip() or None,
            receiver_phone=(phone_ or "").strip() or None,
            receiver_addr=(addr_ or "").strip() or None,
        ))
        for d in depts:
            o = await create_order_internal(db, project=p, dept=d, req_text=req_text, created_by=creator.id)
            order_ids.append(o.id)
    # 待审批暂存的下单资料 → 转挂到各部门任务单（主管/管理层直接下单时无暂存件，自动跳过）
    await _distribute_pending_files(db, p.id, order_ids, creator.id)
    return order_ids


async def _push_order_dispatched(db: AsyncSession, p: models.Project, depts: list[str], creator_name) -> None:
    """下单生效后推送各部门负责人 + 物流（事务提交后调用）。调货订单 depts=[] 时仅跳过推送。"""
    for d in depts:
        await push_message(db, to_role=DEPTS[d]["lead_role"], kind="info",
                           text=f"【销售下单】{p.code} {p.name} 新{DEPTS[d]['name']}任务待分派（销售：{creator_name}）。",
                           biz_type="project", biz_id=p.id)
    if depts:  # 调货订单不通知物流
        await push_message(db, to_role="logistics", kind="info",
                           text=f"【新项目】{p.code} {p.name} 已创建发货待办。",
                           biz_type="project", biz_id=p.id)


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
    # 允许不选生产部门：调货订单不涉及生产，仅建发货待办同步发货部
    depts = [d for d in data.depts if d in DEPTS]
    if data.cust_type not in ("经销商", "终端客户"):
        raise HTTPException(400, "客户分类必须是 经销商/终端客户")

    # 🆕 项目编号：优先人工输入（前端已改为必填、取消自动生成）；
    #    留空时回退旧的自动编号 + 可选后缀（向后兼容，仅作安全网，UI 不会走此分支）。
    code = (data.code or "").strip()
    if code:
        if len(code) > 64:
            raise HTTPException(400, "项目编号过长（≤64 字符）")
    else:
        base = await _compute_next_code(db, None)
        suffix = (data.code_suffix or "").strip().upper()[:2]
        if suffix and not suffix.isalpha():
            raise HTTPException(400, "编号后缀只能是字母")
        code = base + suffix
    res = await db.execute(select(models.Project).where(models.Project.code == code))
    if res.scalar_one_or_none():
        raise HTTPException(409, f"项目编号 {code} 已存在")

    # 1) 项目（预置模板表 + 一览销售名回写）
    p = models.Project(code=code, name=name, status="进行中", manager_id=None)
    db.add(p)
    await db.flush()
    await create_default_template_sheets(db, p.id)
    _writeback_overview(p, "销售", _uname(current))
    _writeback_overview(p, "数量", _qty_str(data.qty, data.unit))  # 🆕 同步一览「数量」单元格

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
        # 🆕 尾款为0时尾款日期自动留空(显示横杠)
        balance_date=(normalize_date_str(data.balance_date) or None) if (data.balance or 0) else None,
        order_type="调货订单" if not depts else "工厂制作订单",
    )
    db.add(led)

    rcv = data.receiver or schemas.SalesReceiverIn()
    req = data.req_text.strip() or f"（销售下单）{name}"

    # 🆕 下单审批流(2026-06-18)：仅销售员下单需主管审批，审批通过后才建各部门任务/发货单/成员；
    #    销售主管/管理层下单直接生效（免审批）。待审批期间项目无成员/无任务，仅销售本人+主管/管理层可见。
    #    可逆开关 sales_order_approval（默认关=下单即生效，与现状一致；生产置 true 开启）。
    if settings.sales_order_approval and not _all_view(current):
        led.order_state = "pending"
        extra = dict(p.extra or {})
        extra["__pending_order__"] = {
            "depts": depts, "req_text": req,
            "receiver": {"name": rcv.name.strip(), "phone": rcv.phone.strip(), "addr": rcv.addr.strip()},
        }
        p.extra = extra
        await db.commit()
        await push_message(db, to_role="sales_lead", kind="info",
                           text=f"【下单待审批】{code} {name} 待销售主管审批（销售：{_uname(current)}）。",
                           biz_type="project", biz_id=p.id)
        await write_audit(db, user=current, action="create_pending", target_type="sales_order",
                          target_id=p.id, detail=f"{code} 待审批 派往{','.join(depts)}")
        return schemas.SalesOrderOut(project_id=p.id, code=code, order_ids=[], ledger_id=led.id)

    # 主管/管理层下单：直接生效
    order_ids = await _materialize_order_downstream(
        db, p, depts, req, (rcv.name, rcv.phone, rcv.addr), current)
    await db.commit()
    await _push_order_dispatched(db, p, depts, _uname(current))
    await write_audit(db, user=current, action="create", target_type="sales_order",
                      target_id=p.id, detail=f"{code} 派往{','.join(depts)}")
    return schemas.SalesOrderOut(project_id=p.id, code=code, order_ids=order_ids, ledger_id=led.id)


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
    # 🆕 尾款为0时尾款日期自动清空(显示横杠)；放在金额/日期赋值之后统一兜底
    if (led.balance or 0) == 0:
        led.balance_date = None
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
    note_val = (data.note or "").strip() or None
    setattr(led, col, note_val)
    # 🆕 发货前付批注=货款已在发货前收讫 → 发货款应收清零；删除批注=未收 → 应收恢复为发货前付金额
    if data.field == "before_ship":
        led.ship_receivable = 0 if note_val else (led.before_ship or 0)
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
    # 🆕 #1/#104 不开票项目(税票=0/历史"/")不应进入开票流（只拦显式不开票，不误伤遗留空税率）
    if _is_no_invoice(led.tax_rate):
        raise HTTPException(400, "该项目税票为「不开票」，无需开票申请")
    if led.invoice_state == "invoiced":
        raise HTTPException(400, "该项目已开票")
    if led.invoice_state in ("applying", "pending_invoice"):
        raise HTTPException(400, "开票申请已在流程中")
    a = await save_upload(db, file, biz_type="invoice_apply", biz_id=led.id,
                          project_id=led.project_id, user=current)
    led.invoice_apply_file_id = a.id
    p = led.project
    # 管理层/销售主管提交时直接进入待开票，无需再自行审批
    if _all_view(current):
        led.invoice_state = "pending_invoice"
        await db.commit()
        await push_message(db, to_role="finance", kind="info",
                           text=f"【待开票】{p.code} {p.name} 金额 ¥{led.amount:,.0f} 税票 {led.tax_rate or '—'}，请财务开票。",
                           biz_type="sales_ledger", biz_id=led.id)
        await write_audit(db, user=current, action="invoice_apply",
                          target_type="sales_ledger", target_id=lid)
        return schemas.Msg(message="开票申请已提交，已同步至财务部待开票")
    else:
        led.invoice_state = "applying"
        await db.commit()
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
    att_to_del = None
    if led.invoice_apply_file_id:
        res = await db.execute(select(models.Attachment).where(
            models.Attachment.id == led.invoice_apply_file_id))
        att_to_del = res.scalar_one_or_none()
    led.invoice_apply_file_id = None   # 先清 FK
    led.invoice_state = None
    await db.flush()                   # flush FK 清除，再删附件行
    if att_to_del:
        await delete_attachment_file(db, att_to_del)
    await db.commit()
    try:
        if led.sales_uid:
            p = led.project
            await push_message(db, to_user_id=led.sales_uid, kind="warn",
                               text=f"【开票驳回】{p.code} 开票申请被销售主管驳回，可修改后重新申请。",
                               biz_type="sales_ledger", biz_id=led.id)
        await write_audit(db, user=current, action="invoice_reject",
                          target_type="sales_ledger", target_id=lid)
    except Exception as e:
        log.warning("invoice_reject 通知/审计失败（主流程已提交）: %s", e)
    return schemas.Msg(message="已驳回")


@router.post("/ledger/{lid}/invoice-void", response_model=schemas.Msg)
async def invoice_void(
    lid: int,
    current: models.User = Depends(require_roles("admin", "manager")),
    db: AsyncSession = Depends(get_db),
):
    """管理员/主管作废待开票申请，退回未申请状态，销售可重新提交。"""
    led = await _ledger_or_404(db, lid)
    if led.invoice_state != "pending_invoice":
        raise HTTPException(400, "只能作废「待开票」状态的申请")
    if led.invoice_batch_id is not None:
        raise HTTPException(400, "合并开票批次请使用批量作废")
    att_to_del = None
    if led.invoice_apply_file_id:
        res = await db.execute(select(models.Attachment).where(
            models.Attachment.id == led.invoice_apply_file_id))
        att_to_del = res.scalar_one_or_none()
    led.invoice_apply_file_id = None
    led.invoice_state = None
    await db.flush()
    if att_to_del:
        await delete_attachment_file(db, att_to_del)
    await db.commit()
    p = led.project
    if led.sales_uid:
        await push_message(db, to_user_id=led.sales_uid, kind="warn",
                           text=f"【开票作废】{p.code} 开票申请已被管理员作废，如需开票请重新提交申请。",
                           biz_type="sales_ledger", biz_id=led.id)
    await write_audit(db, user=current, action="invoice_void",
                      target_type="sales_ledger", target_id=lid)
    return schemas.Msg(message="已作废，退回未申请状态")


@router.post("/ledger/{lid}/admin-mark-invoiced", response_model=schemas.Msg)
async def admin_mark_invoiced(
    lid: int,
    current: models.User = Depends(require_roles("admin", "manager")),
    db: AsyncSession = Depends(get_db),
):
    """管理员直接标记已开票（历史存量数据补录，跳过正常开票流程）。"""
    led = await _ledger_or_404(db, lid)
    if led.invoice_state == "invoiced":
        raise HTTPException(400, "该订单已是开票状态")
    if _is_no_invoice(led.tax_rate):
        raise HTTPException(400, "该项目税票为「不开票」，无需标记")
    # 清除可能残留的开票流状态，直接置为已开票
    led.invoice_apply_file_id = None
    led.invoice_batch_id = None
    led.invoice_state = "invoiced"
    await db.commit()
    await write_audit(db, user=current, action="admin_mark_invoiced",
                      target_type="sales_ledger", target_id=lid,
                      detail=f"管理员直接标记已开票")
    return schemas.Msg(message="已标记为已开票")


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
    # 先取到附件对象，再清 FK 引用，flush 后才删除行——避免 ORM flush 先 DELETE 再 UPDATE 触发 FK 约束
    att_to_del = None
    if led.invoice_file_id:
        res = await db.execute(select(models.Attachment).where(
            models.Attachment.id == led.invoice_file_id))
        att_to_del = res.scalar_one_or_none()
    led.invoice_file_id = None
    led.invoice_state = "pending_invoice"
    await db.flush()   # FK 引用已清，现在可以安全删附件行
    if att_to_del:
        await delete_attachment_file(db, att_to_del)
    await db.commit()
    # 通知/审计失败不影响主流程（push_message 内部有独立 commit，异常不应回滚已提交的发票状态变更）
    p = led.project
    try:
        if led.sales_uid:
            await push_message(db, to_user_id=led.sales_uid, kind="warn",
                               text=f"【发票作废】{p.code} 财务作废了原发票并将重新开票，请留意。",
                               biz_type="sales_ledger", biz_id=led.id)
        await write_audit(db, user=current, action="invoice_revoke",
                          target_type="sales_ledger", target_id=lid)
    except Exception as e:
        log.warning("invoice_revoke 通知/审计失败（主流程已提交）: %s", e)
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
        if _is_no_invoice(l.tax_rate):
            raise HTTPException(400, "所选项目含「不开票」(税票=0)，不能合并开票")
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
    att_to_del = None
    if apply_fid:
        res = await db.execute(select(models.Attachment).where(models.Attachment.id == apply_fid))
        att_to_del = res.scalar_one_or_none()
    sales_uid = leds[0].sales_uid
    codes = _codes(leds)
    for l in leds:        # 先清所有 FK
        l.invoice_state = None
        l.invoice_batch_id = None
        l.invoice_apply_file_id = None
    await db.flush()      # flush FK 清除，再删附件行
    if att_to_del:
        await delete_attachment_file(db, att_to_del)
    await db.commit()
    try:
        if sales_uid:
            await push_message(db, to_user_id=sales_uid, kind="warn",
                               text=f"【合并开票驳回】项目 {codes} 的合并开票申请被销售主管驳回，可修改后重新申请。",
                               biz_type="invoice_batch", biz_id=batch_id)
        await write_audit(db, user=current, action="invoice_batch_reject",
                          target_type="invoice_batch", target_id=batch_id)
    except Exception as e:
        log.warning("invoice_batch_reject 通知/审计失败（主流程已提交）: %s", e)
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
    """作废前置校验：已发货禁止；开票流程进行中先处理；已开票允许作废。"""
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


# ==================== 🆕 销售下单审批（仅销售员下单需主管审批；通过后才建各部门任务/发货单） ====================
# 口径(用户 2026-06-18)：仅销售员下单需审批，主管/管理层下单直接生效；审批通过后才创建并推送
# 各部门任务+物流发货待办；驳回=退回草稿，销售可修改后重新提交。
def _pending_payload(p: models.Project) -> dict:
    return ((p.extra or {}).get("__pending_order__") or {}) if p else {}


@router.get("/order-approvals", response_model=schemas.SalesLedgerListOut)
async def order_approvals(
    current: models.User = Depends(require_roles("sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(models.SalesLedger).where(models.SalesLedger.order_state == "pending")
        .order_by(models.SalesLedger.id.desc())
    )
    return schemas.SalesLedgerListOut(rows=await _ledger_rows(db, list(res.scalars().all())))


@router.post("/ledger/{lid}/order-approve", response_model=schemas.Msg)
async def order_approve(
    lid: int,
    current: models.User = Depends(require_roles("sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    led = await _ledger_or_404(db, lid)
    if led.order_state != "pending":
        raise HTTPException(400, "该下单不在待审批状态")
    p = led.project
    payload = _pending_payload(p)
    depts = [d for d in (payload.get("depts") or []) if d in DEPTS]  # 可为空(调货订单)
    req = payload.get("req_text") or f"（销售下单）{p.name if p else ''}"
    rcv = payload.get("receiver") or {}
    order_ids = await _materialize_order_downstream(
        db, p, depts, req, (rcv.get("name"), rcv.get("phone"), rcv.get("addr")),
        led.sales_user or current)
    led.order_state = None
    led.order_type = "调货订单" if not depts else "工厂制作订单"
    extra = dict(p.extra or {})
    extra.pop("__pending_order__", None)
    p.extra = extra
    await db.commit()
    await _push_order_dispatched(db, p, depts, _uname(led.sales_user))
    if led.sales_uid:
        await push_message(db, to_user_id=led.sales_uid, kind="info",
                           text=f"【下单已通过】{p.code} {p.name} 已通过审批，各部门任务已派发。",
                           biz_type="project", biz_id=p.id)
    await write_audit(db, user=current, action="order_approve",
                      target_type="sales_order", target_id=p.id, detail=f"派往{','.join(depts)}")
    return schemas.Msg(message=f"已通过，已派发 {len(order_ids)} 个部门任务")


@router.post("/ledger/{lid}/order-reject", response_model=schemas.Msg)
async def order_reject(
    lid: int,
    data: schemas.OrderRejectIn,
    current: models.User = Depends(require_roles("sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    led = await _ledger_or_404(db, lid)
    if led.order_state != "pending":
        raise HTTPException(400, "该下单不在待审批状态")
    reason = (data.reason or "").strip()
    led.order_state = "draft"
    p = led.project
    extra = dict(p.extra or {})
    po = dict(extra.get("__pending_order__") or {})
    po["reject_reason"] = reason
    extra["__pending_order__"] = po
    p.extra = extra
    await db.commit()
    if led.sales_uid:
        await push_message(db, to_user_id=led.sales_uid, kind="warn",
                           text=f"【下单退回】{p.code} 下单被主管退回{('：' + reason) if reason else ''}，可修改后重新提交。",
                           biz_type="project", biz_id=p.id)
    await write_audit(db, user=current, action="order_reject",
                      target_type="sales_order", target_id=p.id, detail=reason)
    return schemas.Msg(message="已退回销售修改")


@router.put("/orders/{lid}/draft-resubmit", response_model=schemas.SalesOrderOut)
async def order_draft_resubmit(
    lid: int,
    data: schemas.SalesOrderCreate,
    current: models.User = Depends(require_roles("sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """销售修改被退回的下单(draft)并重新提交审批(→pending)。也可在 pending 状态下改后再交。"""
    led = await _ledger_or_404(db, lid)
    if led.order_state not in ("draft", "pending"):
        raise HTTPException(400, "仅待审批/已退回的下单可修改重提")
    if _is_sales(current) and not _all_view(current) and led.sales_uid != current.id:
        raise HTTPException(403, "只能修改自己的下单")
    p = led.project
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "请填写设备名称")
    # 允许不选生产部门：调货订单不涉及生产，仅建发货待办同步发货部
    depts = [d for d in data.depts if d in DEPTS]
    if data.cust_type not in ("经销商", "终端客户"):
        raise HTTPException(400, "客户分类必须是 经销商/终端客户")
    if p:
        p.name = name
        _writeback_overview(p, "数量", _qty_str(data.qty, data.unit))  # 🆕 同步一览「数量」
    led.customer = data.customer.strip() or None
    led.cust_type = data.cust_type
    led.contract = data.contract if data.contract in ("有", "无") else "无"
    led.amount = data.amount or 0
    led.tax_rate = data.tax_rate
    led.prepay = data.prepay or 0
    led.before_ship = data.before_ship or 0
    led.prepay_note = (data.prepay_note or "").strip() or None
    led.before_ship_note = (data.before_ship_note or "").strip() or None
    led.ship_receivable = data.ship_receivable or 0
    led.balance = data.balance or 0
    led.balance_date = (normalize_date_str(data.balance_date) or None) if (data.balance or 0) else None
    req = data.req_text.strip() or f"（销售下单）{name}"
    rcv = data.receiver or schemas.SalesReceiverIn()
    extra = dict(p.extra or {}) if p else {}
    extra["__pending_order__"] = {
        "depts": depts, "req_text": req,
        "receiver": {"name": rcv.name.strip(), "phone": rcv.phone.strip(), "addr": rcv.addr.strip()},
    }
    if p:
        p.extra = extra
    led.order_state = "pending"
    await db.commit()
    await push_message(db, to_role="sales_lead", kind="info",
                       text=f"【下单待审批】{p.code if p else ''} {name} 已重新提交，待销售主管审批（销售：{_uname(current)}）。",
                       biz_type="project", biz_id=led.project_id)
    await write_audit(db, user=current, action="order_resubmit",
                      target_type="sales_order", target_id=led.project_id)
    return schemas.SalesOrderOut(project_id=led.project_id, code=p.code if p else "", order_ids=[], ledger_id=led.id)


@router.post("/ledger/{lid}/pending-files", response_model=schemas.Msg)
async def upload_pending_files(
    lid: int,
    files: List[UploadFile] = File(...),
    current: models.User = Depends(require_roles("sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """🆕 待审批/草稿下单的下单资料暂存：存为 order_input + biz_id NULL（各部门任务此时不可见），
    审批通过时由 _distribute_pending_files 转挂到新建的各部门任务单。"""
    led = await _ledger_or_404(db, lid)
    if _is_sales(current) and not _all_view(current) and led.sales_uid != current.id:
        raise HTTPException(403, "只能操作自己的下单")
    if led.order_state not in ("pending", "draft"):
        raise HTTPException(400, "仅待审批/已退回的下单可暂存资料")
    n = 0
    for f in files:
        await save_upload(db, f, biz_type="order_input", biz_id=None,
                          project_id=led.project_id, user=current)
        n += 1
    await db.commit()
    await write_audit(db, user=current, action="pending_files",
                      target_type="sales_order", target_id=led.project_id, detail=f"暂存{n}个")
    return schemas.Msg(message=f"已暂存 {n} 个下单资料，审批通过后自动随附各部门任务")


@router.post("/ledger/{lid}/order-discard", response_model=schemas.Msg)
async def order_discard(
    lid: int,
    current: models.User = Depends(require_roles("sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """销售放弃尚未生效的下单(待审批/草稿)→软删项目(无下游任务可清)。"""
    led = await _ledger_or_404(db, lid)
    if led.order_state not in ("draft", "pending"):
        raise HTTPException(400, "仅待审批/已退回的下单可放弃")
    if _is_sales(current) and not _all_view(current) and led.sales_uid != current.id:
        raise HTTPException(403, "只能放弃自己的下单")
    from .projects_router import soft_delete_project
    p = led.project
    led.order_state = None
    # 清理暂存的下单资料(避免孤儿文件)
    hres = await db.execute(select(models.Attachment).where(
        models.Attachment.biz_type == "order_input",
        models.Attachment.project_id == led.project_id,
        models.Attachment.biz_id.is_(None),
    ))
    for a in hres.scalars().all():
        await delete_attachment_file(db, a)
    if p and not p.is_deleted:
        await soft_delete_project(db, p, void_dept_orders=False)
    await db.commit()
    await write_audit(db, user=current, action="order_discard",
                      target_type="sales_order", target_id=lid)
    return schemas.Msg(message="已放弃该下单")
