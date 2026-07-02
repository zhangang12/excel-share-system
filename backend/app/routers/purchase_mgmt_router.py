"""🆕 采购管理模块：供应商档案 / 采购明细 / 账目一览 / 请款流程 / 汇总报表"""
from datetime import datetime, timezone
from typing import Optional, List
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from .. import models, schemas
from ..deps import require_roles, get_current_user

router = APIRouter(prefix="/api/purchase-mgmt", tags=["采购管理"])

_PURCHASE_ROLES = ("buyer", "buyer_lead", "finance", "buyer_standard", "buyer_outsource")
# 🆕 采购员（标准件/外协）也可新增/编辑/删除采购明细与供应商
_WRITE_ROLES = ("buyer", "buyer_lead", "buyer_standard", "buyer_outsource")
# 🆕 采购收货：仓库填送货单号/到货日期/后填价格；采购与管理层亦可
_RECEIVE_ROLES = ("warehouse", "warehouse_lead", "buyer", "buyer_lead", "buyer_standard", "buyer_outsource")


def _uname(u: Optional[models.User]) -> Optional[str]:
    return (u.full_name or u.username) if u else None


def _buyer_restricted(current: models.User) -> bool:
    """buyer 只看自己的（不含 buyer_lead / finance / admin / manager）"""
    return current.has_role("buyer") and not current.has_role(
        "buyer_lead", "finance", "admin", "manager"
    )


# ==================== 供应商 ====================

@router.get("/suppliers", response_model=List[schemas.SupplierOut])
async def list_suppliers(
    category: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    current: models.User = Depends(require_roles(*_PURCHASE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(models.Supplier).order_by(models.Supplier.name)
    if category:
        stmt = stmt.where(models.Supplier.category == category)
    if status:
        stmt = stmt.where(models.Supplier.status == status)
    if q:
        from sqlalchemy import or_
        stmt = stmt.where(
            or_(
                models.Supplier.name.ilike(f"%{q}%"),
                models.Supplier.code.ilike(f"%{q}%"),
                models.Supplier.contact.ilike(f"%{q}%"),
            )
        )
    r = await db.execute(stmt)
    return [_sup_out(s) for s in r.scalars().all()]


def _sup_out(s: models.Supplier) -> schemas.SupplierOut:
    return schemas.SupplierOut(
        id=s.id, name=s.name, code=s.code, category=s.category,
        contact=s.contact, phone=s.phone, address=s.address,
        tax_no=s.tax_no, bank_name=s.bank_name, bank_account=s.bank_account,
        settlement_type=s.settlement_type, credit_days=s.credit_days,
        status=s.status, notes=s.notes, created_at=s.created_at,
    )


@router.post("/suppliers", response_model=schemas.SupplierOut)
async def create_supplier(
    body: schemas.SupplierCreate,
    current: models.User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    s = models.Supplier(**body.model_dump())
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return _sup_out(s)


@router.put("/suppliers/{sid}", response_model=schemas.SupplierOut)
async def update_supplier(
    sid: int,
    body: schemas.SupplierUpdate,
    current: models.User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.Supplier).where(models.Supplier.id == sid))
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "供应商不存在")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(s, k, v)
    await db.commit()
    await db.refresh(s)
    return _sup_out(s)


@router.put("/suppliers/{sid}/toggle")
async def toggle_supplier(
    sid: int,
    current: models.User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.Supplier).where(models.Supplier.id == sid))
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "供应商不存在")
    s.status = "inactive" if s.status == "active" else "active"
    await db.commit()
    return {"status": s.status}


@router.delete("/suppliers/{sid}")
async def delete_supplier(
    sid: int,
    current: models.User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """删除供应商：仅当该供应商没有任何采购明细/请款记录时可硬删除（保护账务历史）。
    有交易记录的供应商请改用「停用」。"""
    r = await db.execute(select(models.Supplier).where(models.Supplier.id == sid))
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "供应商不存在")
    cnt_items = await db.scalar(
        select(func.count()).select_from(models.PurchaseItem).where(
            models.PurchaseItem.supplier_id == sid))
    if cnt_items:
        raise HTTPException(400, f"该供应商已有 {cnt_items} 条采购明细，不能删除；如不再使用请「停用」。")
    cnt_pr = await db.scalar(
        select(func.count()).select_from(models.PaymentRequest).where(
            models.PaymentRequest.supplier_id == sid))
    if cnt_pr:
        raise HTTPException(400, f"该供应商已有 {cnt_pr} 条请款记录，不能删除；如不再使用请「停用」。")
    # 无交易记录：连带清理期初余额后硬删除
    await db.execute(delete(models.SupplierOpeningBalance).where(
        models.SupplierOpeningBalance.supplier_id == sid))
    await db.delete(s)
    await db.commit()
    return {"message": "供应商已删除"}


@router.post("/suppliers/{sid}/opening-balance", response_model=schemas.SupplierOpeningBalanceOut)
async def set_opening_balance(
    sid: int,
    body: schemas.SupplierOpeningBalanceIn,
    current: models.User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        select(models.SupplierOpeningBalance).where(
            models.SupplierOpeningBalance.supplier_id == sid
        )
    )
    ob = r.scalar_one_or_none()
    if ob:
        ob.balance_date = body.balance_date
        ob.outstanding_amount = body.outstanding_amount
        ob.notes = body.notes
    else:
        ob = models.SupplierOpeningBalance(
            supplier_id=sid,
            balance_date=body.balance_date,
            outstanding_amount=body.outstanding_amount,
            notes=body.notes,
        )
        db.add(ob)
    await db.commit()
    await db.refresh(ob)
    return schemas.SupplierOpeningBalanceOut(
        id=ob.id, supplier_id=ob.supplier_id,
        balance_date=ob.balance_date,
        outstanding_amount=ob.outstanding_amount,
        notes=ob.notes,
    )


@router.get("/suppliers/{sid}/opening-balance", response_model=Optional[schemas.SupplierOpeningBalanceOut])
async def get_opening_balance(
    sid: int,
    current: models.User = Depends(require_roles(*_PURCHASE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        select(models.SupplierOpeningBalance).where(
            models.SupplierOpeningBalance.supplier_id == sid
        )
    )
    ob = r.scalar_one_or_none()
    if not ob:
        return None
    return schemas.SupplierOpeningBalanceOut(
        id=ob.id, supplier_id=ob.supplier_id,
        balance_date=ob.balance_date,
        outstanding_amount=ob.outstanding_amount,
        notes=ob.notes,
    )


# ==================== 采购明细 ====================

# 注意：summary 路由须在 /{iid} 之前，避免 "summary" 被解析为 id 参数
@router.get("/items/summary", response_model=schemas.PurchaseItemSummary)
async def items_summary(
    supplier_id: Optional[int] = Query(None),
    project_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    invoice_status: Optional[str] = Query(None),
    current: models.User = Depends(require_roles(*_PURCHASE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(models.PurchaseItem)
    if _buyer_restricted(current):
        stmt = stmt.where(models.PurchaseItem.buyer_id == current.id)
    if supplier_id:
        stmt = stmt.where(models.PurchaseItem.supplier_id == supplier_id)
    if project_code:
        stmt = stmt.where(models.PurchaseItem.project_code.ilike(f"%{project_code}%"))
    if month:
        stmt = stmt.where(models.PurchaseItem.delivery_date.startswith(month))
    if invoice_status:
        stmt = stmt.where(models.PurchaseItem.invoice_status == invoice_status)
    r = await db.execute(stmt)
    items = r.scalars().all()
    received = sum(i.received_amount or 0 for i in items)
    invoiced = sum(i.invoice_amount or 0 for i in items)
    paid = sum(i.paid_amount or 0 for i in items)
    return schemas.PurchaseItemSummary(
        received_total=received,
        uninvoiced=max(received - invoiced, 0),
        paid_total=paid,
        outstanding=max(received - paid, 0),
        count=len(items),
    )


@router.get("/items", response_model=List[schemas.PurchaseItemOut])
async def list_items(
    supplier_id: Optional[int] = Query(None),
    project_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    invoice_status: Optional[str] = Query(None),
    current: models.User = Depends(require_roles(*_PURCHASE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import desc, nulls_last
    stmt = select(models.PurchaseItem).order_by(
        models.PurchaseItem.delivery_date.desc(),
        models.PurchaseItem.id.desc(),
    )
    if _buyer_restricted(current):
        stmt = stmt.where(models.PurchaseItem.buyer_id == current.id)
    if supplier_id:
        stmt = stmt.where(models.PurchaseItem.supplier_id == supplier_id)
    if project_code:
        stmt = stmt.where(models.PurchaseItem.project_code.ilike(f"%{project_code}%"))
    if month:
        stmt = stmt.where(models.PurchaseItem.delivery_date.startswith(month))
    if invoice_status:
        stmt = stmt.where(models.PurchaseItem.invoice_status == invoice_status)
    r = await db.execute(stmt)
    return [_item_out(i) for i in r.scalars().all()]


def _item_out(i: models.PurchaseItem) -> schemas.PurchaseItemOut:
    return schemas.PurchaseItemOut(
        id=i.id, po_no=i.po_no, supplier_id=i.supplier_id,
        supplier_name=i.supplier.name if i.supplier else "",
        delivery_date=i.delivery_date, contract_no=i.contract_no,
        project_code=i.project_code, delivery_note_no=i.delivery_note_no,
        arrival_date=i.arrival_date,
        item_name=i.item_name, spec=i.spec, qty=i.qty, unit_price=i.unit_price,
        received_amount=i.received_amount or 0,
        invoice_date=i.invoice_date, tax_rate=i.tax_rate,
        invoice_amount=i.invoice_amount or 0,
        paid_amount=i.paid_amount or 0, paid_date=i.paid_date,
        invoice_status=i.invoice_status,
        buyer_id=i.buyer_id,
        buyer_name=_uname(i.buyer),
        notes=i.notes, created_at=i.created_at,
    )


@router.post("/items", response_model=schemas.PurchaseItemOut)
async def create_item(
    body: schemas.PurchaseItemCreate,
    current: models.User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    data = body.model_dump()
    if not data.get("received_amount") and data.get("qty") and data.get("unit_price"):
        data["received_amount"] = round((data["qty"] or 0) * (data["unit_price"] or 0), 4)
    data["buyer_id"] = current.id
    item = models.PurchaseItem(**data)
    db.add(item)
    await db.commit()
    r = await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.id == item.id))
    return _item_out(r.scalar_one())


async def _next_po_no(db: AsyncSession) -> str:
    """采购单号：CG{yyyymmdd}-{当日序号3位}。同一天多张单顺序递增。"""
    from datetime import date as _date
    prefix = f"CG{_date.today().strftime('%Y%m%d')}-"
    r = await db.execute(
        select(func.count(func.distinct(models.PurchaseItem.po_no)))
        .where(models.PurchaseItem.po_no.like(f"{prefix}%"))
    )
    n = (r.scalar() or 0) + 1
    return f"{prefix}{n:03d}"


@router.post("/orders", response_model=List[schemas.PurchaseItemOut])
async def create_purchase_order(
    body: schemas.PurchaseOrderCreate,
    current: models.User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """🆕 采购单：同一供应商一次录入多个零件行 → 生成一个采购单号(po_no)，批量建采购明细。

    - 表头共享：供应商 / 下单日期 / 合同编号 / 默认项目编号（行可覆盖项目编号）
    - 单价选填：后填价格流程留空即可（货到仓库再由仓库补送货单号/到货，采购/财务补价）
    """
    lines = [ln for ln in body.lines if (ln.item_name or "").strip()]
    if not lines:
        raise HTTPException(400, "请至少填写一行有效明细（名称必填）")
    po_no = await _next_po_no(db)
    for ln in lines:
        recv = ln.received_amount
        if not recv and ln.qty and ln.unit_price:
            recv = round((ln.qty or 0) * (ln.unit_price or 0), 4)
        db.add(models.PurchaseItem(
            po_no=po_no,
            supplier_id=body.supplier_id,
            delivery_date=body.delivery_date,
            contract_no=body.contract_no,
            project_code=(ln.project_code or body.project_code),
            item_name=ln.item_name.strip(),
            spec=ln.spec, qty=ln.qty, unit_price=ln.unit_price,
            received_amount=recv or 0,
            tax_rate=ln.tax_rate, notes=ln.notes,
            buyer_id=current.id,
        ))
    await db.commit()
    r = await db.execute(
        select(models.PurchaseItem).where(models.PurchaseItem.po_no == po_no)
        .order_by(models.PurchaseItem.id)
    )
    return [_item_out(x) for x in r.scalars().all()]


@router.put("/items/{iid}", response_model=schemas.PurchaseItemOut)
async def update_item(
    iid: int,
    body: schemas.PurchaseItemUpdate,
    current: models.User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.id == iid))
    item = r.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "明细不存在")
    if _buyer_restricted(current) and item.buyer_id != current.id:
        raise HTTPException(403, "无权编辑他人明细")
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(item, k, v)
    if ("qty" in data or "unit_price" in data) and "received_amount" not in data:
        if item.qty and item.unit_price:
            item.received_amount = round(item.qty * item.unit_price, 4)
    await db.commit()
    r = await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.id == iid))
    return _item_out(r.scalar_one())


@router.delete("/items/{iid}")
async def delete_item(
    iid: int,
    current: models.User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.id == iid))
    item = r.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "明细不存在")
    if _buyer_restricted(current) and item.buyer_id != current.id:
        raise HTTPException(403, "无权删除他人明细")
    if item.invoice_status in ("已对账", "已开票"):
        raise HTTPException(400, "已对账/已开票的明细不可删除")
    await db.delete(item)
    await db.commit()
    return {"ok": True}


# ==================== 采购收货（仓库）====================
@router.get("/receiving", response_model=List[schemas.PurchaseItemOut])
async def list_receiving(
    supplier_id: Optional[int] = Query(None),
    po_no: Optional[str] = Query(None),
    received: bool = Query(False, description="False=待收货(默认) / True=已收货"),
    current: models.User = Depends(require_roles(*_RECEIVE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """采购收货清单：默认列出「待收货」明细（到货日期为空）供仓库确认收货、补送货单号/后填价格。"""
    stmt = select(models.PurchaseItem).order_by(
        models.PurchaseItem.po_no.desc().nullslast(),
        models.PurchaseItem.id.desc(),
    )
    if received:
        stmt = stmt.where(models.PurchaseItem.arrival_date.isnot(None))
    else:
        stmt = stmt.where(models.PurchaseItem.arrival_date.is_(None))
    if supplier_id:
        stmt = stmt.where(models.PurchaseItem.supplier_id == supplier_id)
    if po_no:
        stmt = stmt.where(models.PurchaseItem.po_no.ilike(f"%{po_no}%"))
    r = await db.execute(stmt.limit(300))
    return [_item_out(i) for i in r.scalars().all()]


@router.put("/items/{iid}/receive", response_model=schemas.PurchaseItemOut)
async def receive_item(
    iid: int,
    body: schemas.PurchaseReceiveIn,
    current: models.User = Depends(require_roles(*_RECEIVE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """仓库收货：填送货单号 + 到货日期（后填价格流程一并补单价/收货金额）。
    到货日期一填即视为已收货。"""
    r = await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.id == iid))
    item = r.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "明细不存在")
    if not body.arrival_date:
        raise HTTPException(400, "请填写到货日期")
    item.delivery_note_no = body.delivery_note_no
    item.arrival_date = body.arrival_date
    if body.unit_price is not None:
        item.unit_price = body.unit_price
    if body.received_amount is not None:
        item.received_amount = body.received_amount
    elif item.qty and item.unit_price:
        item.received_amount = round(item.qty * item.unit_price, 4)
    await db.commit()
    r = await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.id == iid))
    return _item_out(r.scalar_one())


@router.post("/items/batch-invoice")
async def batch_invoice(
    body: schemas.BatchInvoiceIn,
    current: models.User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        select(models.PurchaseItem).where(models.PurchaseItem.id.in_(body.item_ids))
    )
    items = r.scalars().all()
    # 🆕 #100 对账后合计开票：合计开票金额按各明细「收货金额」比例分摊到 invoice_amount，
    #     收货额全为 0 时均摊；末条兜余数，保证分摊合计精确等于填入的合计开票金额。
    amt = body.invoice_amount
    total_received = sum(float(i.received_amount or 0) for i in items)
    n = len(items)
    allocated = 0.0
    for idx, item in enumerate(items):
        item.invoice_status = "已开票"
        if body.invoice_date:
            item.invoice_date = body.invoice_date
        if amt is not None:
            if idx == n - 1:
                item.invoice_amount = round(amt - allocated, 2)
            else:
                share = round(amt * float(item.received_amount or 0) / total_received, 2) if total_received > 0 else round(amt / n, 2)
                item.invoice_amount = share
                allocated += share
    await db.commit()
    return {"updated": len(items)}


# ==================== 供应商账目一览 ====================

@router.get("/statements", response_model=schemas.SupplierStatementList)
async def supplier_statements(
    current: models.User = Depends(require_roles(*_PURCHASE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.Supplier).order_by(models.Supplier.name))
    all_suppliers = r.scalars().all()

    ob_r = await db.execute(select(models.SupplierOpeningBalance))
    ob_map = {ob.supplier_id: ob.outstanding_amount for ob in ob_r.scalars().all()}

    item_r = await db.execute(select(models.PurchaseItem))
    items = item_r.scalars().all()

    grp: dict = defaultdict(lambda: {"received": 0.0, "invoice": 0.0, "paid": 0.0, "count": 0})
    for i in items:
        g = grp[i.supplier_id]
        g["received"] += i.received_amount or 0
        g["invoice"] += i.invoice_amount or 0
        g["paid"] += i.paid_amount or 0
        g["count"] += 1

    if _buyer_restricted(current):
        my_sids = {i.supplier_id for i in items if i.buyer_id == current.id}
    else:
        my_sids = None

    rows = []
    total_opening = total_received = total_paid = total_outstanding = 0.0
    for s in all_suppliers:
        if my_sids is not None and s.id not in my_sids:
            continue
        g = grp.get(s.id, {"received": 0.0, "invoice": 0.0, "paid": 0.0, "count": 0})
        ob = ob_map.get(s.id, 0.0)
        outstanding = ob + g["received"] - g["paid"]
        uninvoiced = g["received"] - g["invoice"]
        rows.append(schemas.SupplierStatementRow(
            supplier_id=s.id, supplier_name=s.name, category=s.category,
            opening_balance=ob, received_total=g["received"],
            invoice_total=g["invoice"], paid_total=g["paid"],
            outstanding=outstanding, uninvoiced=uninvoiced,
            item_count=g["count"],
        ))
        total_opening += ob
        total_received += g["received"]
        total_paid += g["paid"]
        total_outstanding += outstanding

    return schemas.SupplierStatementList(
        rows=rows, total_opening=total_opening,
        total_received=total_received, total_paid=total_paid,
        total_outstanding=total_outstanding,
    )


@router.get("/statements/{sid}/detail", response_model=List[schemas.PurchaseItemOut])
async def supplier_statement_detail(
    sid: int,
    current: models.User = Depends(require_roles(*_PURCHASE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(models.PurchaseItem).where(
        models.PurchaseItem.supplier_id == sid
    ).order_by(models.PurchaseItem.delivery_date.desc(), models.PurchaseItem.id.desc())
    if _buyer_restricted(current):
        stmt = stmt.where(models.PurchaseItem.buyer_id == current.id)
    r = await db.execute(stmt)
    return [_item_out(i) for i in r.scalars().all()]


# ==================== 请款流程 ====================

async def _pr_out(db: AsyncSession, pr_id: int) -> schemas.PaymentRequestOut:
    r = await db.execute(select(models.PaymentRequest).where(models.PaymentRequest.id == pr_id))
    pr = r.scalar_one()
    ri = await db.execute(
        select(models.PaymentRequestItem, models.PurchaseItem)
        .join(models.PurchaseItem, models.PaymentRequestItem.item_id == models.PurchaseItem.id)
        .where(models.PaymentRequestItem.request_id == pr_id)
    )
    item_rows = [
        {
            "item_id": pri.item_id,
            "item_name": pi.item_name,
            "allocated_amount": pri.allocated_amount,
        }
        for pri, pi in ri.all()
    ]
    return schemas.PaymentRequestOut(
        id=pr.id, supplier_id=pr.supplier_id,
        supplier_name=pr.supplier.name if pr.supplier else "",
        requested_amount=pr.requested_amount,
        requester_id=pr.requester_id,
        requester_name=_uname(pr.requester),
        status=pr.status, notes=pr.notes,
        finance_approver_id=pr.finance_approver_id,
        approver_name=_uname(pr.finance_approver),
        approved_at=pr.approved_at,
        paid_amount=pr.paid_amount, paid_date=pr.paid_date,
        payment_method=pr.payment_method,
        reject_reason=pr.reject_reason,
        created_at=pr.created_at,
        items=item_rows,
    )


@router.post("/payment-requests", response_model=schemas.PaymentRequestOut)
async def create_payment_request(
    body: schemas.PaymentRequestCreate,
    current: models.User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    pr = models.PaymentRequest(
        supplier_id=body.supplier_id,
        requested_amount=body.requested_amount,
        requester_id=current.id,
        notes=body.notes,
        status="pending",
    )
    db.add(pr)
    await db.flush()
    for item_in in body.items:
        db.add(models.PaymentRequestItem(
            request_id=pr.id,
            item_id=item_in.item_id,
            allocated_amount=item_in.allocated_amount,
        ))
    await db.commit()
    # notify finance users
    try:
        fin_r = await db.execute(
            select(models.User).join(models.Role, models.User.role_id == models.Role.id)
            .where(models.Role.code == "finance", models.User.is_active == True)  # noqa: E712
        )
        for u in fin_r.scalars().all():
            db.add(models.Message(
                to_user_id=u.id, kind="info",
                text=f"采购请款：{_uname(current)} 发起请款 ¥{body.requested_amount:.2f}，请审批",
                biz_type="payment_request", biz_id=pr.id,
            ))
        await db.commit()
    except Exception:
        pass
    return await _pr_out(db, pr.id)


@router.get("/payment-requests", response_model=List[schemas.PaymentRequestOut])
async def list_payment_requests(
    status: Optional[str] = Query(None),
    supplier_id: Optional[int] = Query(None),
    current: models.User = Depends(require_roles(*_PURCHASE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(models.PaymentRequest).order_by(models.PaymentRequest.created_at.desc())
    if status:
        stmt = stmt.where(models.PaymentRequest.status == status)
    if supplier_id:
        stmt = stmt.where(models.PaymentRequest.supplier_id == supplier_id)
    if _buyer_restricted(current):
        stmt = stmt.where(models.PaymentRequest.requester_id == current.id)
    r = await db.execute(stmt)
    return [await _pr_out(db, pr.id) for pr in r.scalars().all()]


@router.put("/payment-requests/{prid}/approve")
async def approve_payment_request(
    prid: int,
    current: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.PaymentRequest).where(models.PaymentRequest.id == prid))
    pr = r.scalar_one_or_none()
    if not pr:
        raise HTTPException(404, "请款单不存在")
    if pr.status != "pending":
        raise HTTPException(400, "只有待审状态的请款单可审批")
    pr.status = "approved"
    pr.finance_approver_id = current.id
    pr.approved_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True}


@router.put("/payment-requests/{prid}/reject")
async def reject_payment_request(
    prid: int,
    body: schemas.PaymentRejectIn,
    current: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.PaymentRequest).where(models.PaymentRequest.id == prid))
    pr = r.scalar_one_or_none()
    if not pr:
        raise HTTPException(404, "请款单不存在")
    if pr.status != "pending":
        raise HTTPException(400, "只有待审状态可驳回")
    pr.status = "rejected"
    pr.finance_approver_id = current.id
    pr.reject_reason = body.reason
    await db.commit()
    return {"ok": True}


@router.put("/payment-requests/{prid}/pay")
async def pay_payment_request(
    prid: int,
    body: schemas.PaymentPayIn,
    current: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.PaymentRequest).where(models.PaymentRequest.id == prid))
    pr = r.scalar_one_or_none()
    if not pr:
        raise HTTPException(404, "请款单不存在")
    if pr.status != "approved":
        raise HTTPException(400, "只有已审批的请款单可付款")
    pr.status = "paid"
    pr.paid_amount = body.paid_amount
    pr.paid_date = body.paid_date
    pr.payment_method = body.payment_method

    # 回写明细 paid_amount
    ri = await db.execute(
        select(models.PaymentRequestItem).where(models.PaymentRequestItem.request_id == prid)
    )
    pri_rows = ri.scalars().all()
    if pri_rows:
        total_alloc = sum(p.allocated_amount for p in pri_rows)
        for pri in pri_rows:
            ir = await db.execute(
                select(models.PurchaseItem).where(models.PurchaseItem.id == pri.item_id)
            )
            item = ir.scalar_one_or_none()
            if item:
                if total_alloc > 0:
                    ratio = pri.allocated_amount / total_alloc
                    add = round(body.paid_amount * ratio, 4)
                else:
                    add = round(body.paid_amount / len(pri_rows), 4)
                item.paid_amount = (item.paid_amount or 0) + add
                item.paid_date = body.paid_date

    await db.commit()
    return {"ok": True}


# ==================== 汇总报表 ====================

@router.get("/reports/overview", response_model=schemas.PurchaseKPI)
async def report_overview(
    current: models.User = Depends(require_roles("buyer_lead", "finance")),
    db: AsyncSession = Depends(get_db),
):
    from datetime import date
    today = date.today()
    this_month = today.strftime("%Y-%m")
    q_start_month = ((today.month - 1) // 3) * 3 + 1
    this_quarter_prefix = f"{today.year:04d}-{q_start_month:02d}"
    this_year = str(today.year)

    r = await db.execute(select(models.PurchaseItem))
    items = r.scalars().all()

    month_amount = sum(
        i.received_amount or 0 for i in items
        if (i.delivery_date or "").startswith(this_month)
    )
    quarter_amount = sum(
        i.received_amount or 0 for i in items
        if (i.delivery_date or "") >= f"{today.year:04d}-{q_start_month:02d}-01"
    )
    year_amount = sum(
        i.received_amount or 0 for i in items
        if (i.delivery_date or "").startswith(this_year)
    )

    ob_r = await db.execute(select(models.SupplierOpeningBalance))
    ob_total = sum(ob.outstanding_amount or 0 for ob in ob_r.scalars().all())
    total_received = sum(i.received_amount or 0 for i in items)
    total_paid = sum(i.paid_amount or 0 for i in items)
    total_outstanding = ob_total + total_received - total_paid

    pr_r = await db.execute(
        select(func.count(models.PaymentRequest.id)).where(
            models.PaymentRequest.status == "pending"
        )
    )
    pending_count = pr_r.scalar() or 0

    return schemas.PurchaseKPI(
        month_amount=month_amount,
        quarter_amount=quarter_amount,
        year_amount=year_amount,
        total_outstanding=max(total_outstanding, 0),
        pending_requests=pending_count,
    )


@router.get("/reports/monthly-trend", response_model=List[schemas.PurchaseMonthlyPoint])
async def report_monthly_trend(
    months: int = Query(12),
    current: models.User = Depends(require_roles("buyer_lead", "finance")),
    db: AsyncSession = Depends(get_db),
):
    from datetime import date
    r = await db.execute(select(models.PurchaseItem))
    items = r.scalars().all()

    by_month: dict = defaultdict(lambda: {"amount": 0.0, "paid": 0.0})
    for i in items:
        m = (i.delivery_date or "")[:7]
        if m:
            by_month[m]["amount"] += i.received_amount or 0
            by_month[m]["paid"] += i.paid_amount or 0

    today = date.today()
    result = []
    for delta in range(months - 1, -1, -1):
        total_months = today.year * 12 + today.month - 1 - delta
        y = total_months // 12
        m = total_months % 12 + 1
        key = f"{y:04d}-{m:02d}"
        g = by_month.get(key, {"amount": 0.0, "paid": 0.0})
        result.append(schemas.PurchaseMonthlyPoint(month=key, amount=g["amount"], paid=g["paid"]))
    return result


@router.get("/reports/by-buyer", response_model=List[schemas.PurchaseBuyerRow])
async def report_by_buyer(
    current: models.User = Depends(require_roles("buyer_lead", "finance")),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.PurchaseItem))
    items = r.scalars().all()
    by_buyer: dict = defaultdict(lambda: {"name": "未知", "amount": 0.0, "count": 0})
    for i in items:
        bid = i.buyer_id or 0
        by_buyer[bid]["amount"] += i.received_amount or 0
        by_buyer[bid]["count"] += 1
        if i.buyer:
            by_buyer[bid]["name"] = _uname(i.buyer) or "未知"
    return [
        schemas.PurchaseBuyerRow(
            buyer_id=bid if bid else None,
            buyer_name=v["name"],
            amount=v["amount"],
            count=v["count"],
        )
        for bid, v in sorted(by_buyer.items(), key=lambda x: -x[1]["amount"])
    ]


@router.get("/reports/by-project", response_model=List[schemas.PurchaseProjectRow])
async def report_by_project(
    q: Optional[str] = Query(None),
    current: models.User = Depends(require_roles("buyer_lead", "finance")),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(models.PurchaseItem)
    if q:
        stmt = stmt.where(models.PurchaseItem.project_code.ilike(f"%{q}%"))
    r = await db.execute(stmt)
    items = r.scalars().all()
    by_proj: dict = defaultdict(lambda: {"amount": 0.0, "count": 0})
    for i in items:
        code = i.project_code or "（无项目编号）"
        by_proj[code]["amount"] += i.received_amount or 0
        by_proj[code]["count"] += 1
    return [
        schemas.PurchaseProjectRow(project_code=code, amount=v["amount"], count=v["count"])
        for code, v in sorted(by_proj.items(), key=lambda x: -x[1]["amount"])
    ]


@router.get("/reports/top-suppliers", response_model=List[schemas.PurchaseTopSupplier])
async def report_top_suppliers(
    limit: int = Query(10),
    current: models.User = Depends(require_roles("buyer_lead", "finance")),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.PurchaseItem))
    items = r.scalars().all()
    by_sup: dict = defaultdict(lambda: {"name": "", "amount": 0.0, "count": 0})
    for i in items:
        by_sup[i.supplier_id]["amount"] += i.received_amount or 0
        by_sup[i.supplier_id]["count"] += 1
        if i.supplier:
            by_sup[i.supplier_id]["name"] = i.supplier.name
    top = sorted(by_sup.items(), key=lambda x: -x[1]["amount"])[:limit]
    return [
        schemas.PurchaseTopSupplier(
            supplier_id=sid, supplier_name=v["name"],
            amount=v["amount"], count=v["count"],
        )
        for sid, v in top
    ]
