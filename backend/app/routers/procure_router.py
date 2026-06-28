"""🆕 采购模块（一期）：供应商档案 + 采购明细（唯一录入）+ 账目一览/单家报表/月季年汇总。

设计要点（对齐 docs/采购模块设计）：
- 录一次「采购明细」→ 供应商账目一览 / 每家供应商报表 / 月季年汇总 全部自动派生。
- 欠款 = 收货金额 − 付款金额；待开票 = 收货金额 − 开票金额。
- 账期是供应商关键属性（现金/月结N月/无账期）。
- 行级隔离：采购员（buyer）仅见 buyer_uid=本人 的明细与其聚合；admin/manager 全量。
- 开票/付款支持「按勾选批量登记」，自动回填选中明细行（全额结）。
"""
from datetime import datetime, timezone, date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from .. import models, schemas
from ..deps import require_roles
from ..utils import write_audit

router = APIRouter(prefix="/api/procure", tags=["采购模块"])

SUPPLIER_CATEGORIES = ["外协加工", "标准件", "不锈钢原料", "激光件", "电气件", "运输", "其他"]
SETTLE_TYPES = ["现金", "月结", "无账期"]
RECON_STATUSES = ["待对账", "已对账"]


def _all_view(u: models.User) -> bool:
    """全量视角：admin/manager（采购主管/老板看全部）。"""
    return u.has_role("admin", "manager")


def _uname(u: Optional[models.User]) -> Optional[str]:
    return (u.full_name or u.username) if u else None


def _norm_date(s: Optional[str]) -> Optional[str]:
    """把 2026/6/10、2026.6.10、2026年6月10日 等统一成 2026-06-10；解析不了原样保留。"""
    s = (s or "").strip()
    if not s:
        return None
    t = s.replace("/", "-").replace(".", "-").replace("年", "-").replace("月", "-").replace("日", "")
    parts = [p for p in t.split("-") if p != ""]
    if len(parts) < 3:
        return s
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        return f"{y:04d}-{m:02d}-{d:02d}"
    except ValueError:
        return s


def _ym(s: Optional[str]) -> Optional[str]:
    d = _norm_date(s)
    return d[:7] if d and len(d) >= 7 and d[4] == "-" else None


async def _name_map(db: AsyncSession, uids: set[int]) -> dict[int, Optional[str]]:
    uids = {u for u in uids if u}
    if not uids:
        return {}
    res = await db.execute(select(models.User).where(models.User.id.in_(uids)))
    return {u.id: _uname(u) for u in res.scalars().all()}


async def _supplier_or_404(db: AsyncSession, sid: int) -> models.Supplier:
    res = await db.execute(select(models.Supplier).where(models.Supplier.id == sid))
    s = res.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "供应商不存在")
    return s


async def _item_or_404(db: AsyncSession, iid: int) -> models.PurchaseItem:
    res = await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.id == iid))
    it = res.scalar_one_or_none()
    if not it:
        raise HTTPException(404, "采购明细不存在")
    return it


async def _visible_items(db: AsyncSession, current: models.User) -> list[models.PurchaseItem]:
    """当前用户可见的全部采购明细（行级隔离）。聚合/报表的统一数据源。"""
    q = select(models.PurchaseItem)
    if not _all_view(current):
        q = q.where(models.PurchaseItem.buyer_uid == current.id)
    res = await db.execute(q)
    return list(res.scalars().all())


def _amts(it: models.PurchaseItem) -> tuple[float, float, float, float, float]:
    """(收货, 已开票, 待开票, 已付款, 欠款)。"""
    recv = it.recv_amount or 0.0
    inv = it.invoice_amount or 0.0
    paid = it.pay_amount or 0.0
    return recv, inv, recv - inv, paid, recv - paid


# ============================ 供应商 ============================
@router.get("/suppliers", response_model=schemas.SupplierListOut)
async def list_suppliers(
    category: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    kw: Optional[str] = Query(None),
    current: models.User = Depends(require_roles("buyer")),
    db: AsyncSession = Depends(get_db),
):
    """供应商列表（含账目一览口径聚合：送货单金额/已开票/待开票/已付款/欠款）。"""
    q = select(models.Supplier)
    if category:
        q = q.where(models.Supplier.category == category)
    if status:
        q = q.where(models.Supplier.status == status)
    res = await db.execute(q.order_by(models.Supplier.id.desc()))
    suppliers = list(res.scalars().all())
    if kw:
        k = kw.strip()
        suppliers = [s for s in suppliers if k in (s.name or "") or k in (s.contact or "")]

    # 聚合（按可见范围）
    agg: dict[int, list[float]] = {}      # sid -> [recv, inv, paid, count]
    for it in await _visible_items(db, current):
        recv, inv, to_inv, paid, owed = _amts(it)
        a = agg.setdefault(it.supplier_id, [0.0, 0.0, 0.0, 0])
        a[0] += recv; a[1] += inv; a[2] += paid; a[3] += 1

    rows = []
    for s in suppliers:
        a = agg.get(s.id, [0.0, 0.0, 0.0, 0])
        recv, inv, paid, cnt = a
        rows.append(schemas.SupplierRow(
            id=s.id, name=s.name, category=s.category, contact=s.contact, phone=s.phone,
            address=s.address, tax_no=s.tax_no, bank_name=s.bank_name, bank_account=s.bank_account,
            settle_type=s.settle_type, settle_days=s.settle_days, note=s.note, status=s.status,
            recv_total=round(recv, 2), invoiced=round(inv, 2), to_invoice=round(recv - inv, 2),
            paid=round(paid, 2), owed=round(recv - paid, 2), item_count=cnt,
        ))
    return schemas.SupplierListOut(rows=rows, total=len(rows))


@router.get("/suppliers/options", response_model=list[schemas.SupplierOption])
async def supplier_options(
    current: models.User = Depends(require_roles("buyer")),
    db: AsyncSession = Depends(get_db),
):
    """录入明细时的供应商下拉（仅启用的）。"""
    res = await db.execute(
        select(models.Supplier).where(models.Supplier.status == "active")
        .order_by(models.Supplier.name))
    return [schemas.SupplierOption(id=s.id, name=s.name, category=s.category,
                                   settle_type=s.settle_type, settle_days=s.settle_days)
            for s in res.scalars().all()]


@router.post("/suppliers", response_model=schemas.SupplierRow)
async def create_supplier(
    data: schemas.SupplierIn,
    current: models.User = Depends(require_roles("buyer")),
    db: AsyncSession = Depends(get_db),
):
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "供应商名称必填")
    res = await db.execute(select(models.Supplier).where(models.Supplier.name == name))
    if res.scalar_one_or_none():
        raise HTTPException(400, "该供应商已存在")
    st = (data.settle_type or "月结").strip()
    if st not in SETTLE_TYPES:
        st = "月结"
    s = models.Supplier(
        name=name, category=(data.category or "").strip() or None,
        contact=(data.contact or "").strip() or None, phone=(data.phone or "").strip() or None,
        address=(data.address or "").strip() or None, tax_no=(data.tax_no or "").strip() or None,
        bank_name=(data.bank_name or "").strip() or None,
        bank_account=(data.bank_account or "").strip() or None,
        settle_type=st, settle_days=data.settle_days,
        note=(data.note or "").strip() or None,
        status=(data.status or "active").strip() or "active",
        created_by=current.id,
    )
    db.add(s)
    await db.commit()
    await write_audit(db, user=current, action="create", target_type="supplier",
                      target_id=s.id, detail=name)
    return schemas.SupplierRow(id=s.id, name=s.name, category=s.category, contact=s.contact,
                               phone=s.phone, address=s.address, tax_no=s.tax_no,
                               bank_name=s.bank_name, bank_account=s.bank_account,
                               settle_type=s.settle_type, settle_days=s.settle_days,
                               note=s.note, status=s.status)


@router.post("/suppliers/import", response_model=schemas.Msg)
async def import_suppliers(
    data: schemas.SupplierImportIn,
    current: models.User = Depends(require_roles("buyer")),
    db: AsyncSession = Depends(get_db),
):
    """从「供应商名单」批量导入（每行一个名称，已存在的跳过）。"""
    res = await db.execute(select(models.Supplier.name))
    have = {(n or "").strip() for (n,) in res.all()}
    cat = (data.category or "").strip() or None
    created = 0
    for raw in data.names:
        name = (raw or "").strip()
        if not name or name in have:
            continue
        db.add(models.Supplier(name=name, category=cat, settle_type="月结",
                               status="active", created_by=current.id))
        have.add(name)
        created += 1
    if created:
        await db.commit()
        await write_audit(db, user=current, action="import", target_type="supplier",
                          target_id=0, detail=f"批量导入 {created} 家")
    return schemas.Msg(message=f"已导入 {created} 家供应商（跳过重复 {len(data.names) - created} 条）")


@router.put("/suppliers/{sid}", response_model=schemas.Msg)
async def update_supplier(
    sid: int, data: schemas.SupplierIn,
    current: models.User = Depends(require_roles("buyer")),
    db: AsyncSession = Depends(get_db),
):
    s = await _supplier_or_404(db, sid)
    name = data.name.strip()
    if name and name != s.name:
        res = await db.execute(select(models.Supplier).where(
            models.Supplier.name == name, models.Supplier.id != sid))
        if res.scalar_one_or_none():
            raise HTTPException(400, "该供应商名称已被占用")
        s.name = name
    for f in ("category", "contact", "phone", "address", "tax_no", "bank_name", "bank_account", "note"):
        v = getattr(data, f)
        if v is not None:
            setattr(s, f, v.strip() or None)
    if data.settle_type is not None:
        s.settle_type = data.settle_type.strip() if data.settle_type.strip() in SETTLE_TYPES else s.settle_type
    if data.settle_days is not None:
        s.settle_days = data.settle_days
    if data.status is not None and data.status.strip() in ("active", "inactive"):
        s.status = data.status.strip()
    await db.commit()
    await write_audit(db, user=current, action="update", target_type="supplier", target_id=sid)
    return schemas.Msg(message="已保存")


@router.delete("/suppliers/{sid}", response_model=schemas.Msg)
async def delete_supplier(
    sid: int,
    current: models.User = Depends(require_roles("buyer")),
    db: AsyncSession = Depends(get_db),
):
    """删除供应商；已有采购明细的不可删除（改为停用）。"""
    s = await _supplier_or_404(db, sid)
    res = await db.execute(select(models.PurchaseItem.id).where(
        models.PurchaseItem.supplier_id == sid).limit(1))
    if res.first():
        raise HTTPException(400, "该供应商已有采购明细，不能删除；可改为「停用」")
    name = s.name
    await db.delete(s)
    await db.commit()
    await write_audit(db, user=current, action="delete", target_type="supplier",
                      target_id=sid, detail=name)
    return schemas.Msg(message="供应商已删除")


# ============================ 采购明细 ============================
def _to_row(it: models.PurchaseItem, names: dict[int, Optional[str]]) -> schemas.PurchaseItemRow:
    recv, inv, to_inv, paid, owed = _amts(it)
    return schemas.PurchaseItemRow(
        id=it.id, recon_status=it.recon_status, delivery_date=it.delivery_date,
        supplier_id=it.supplier_id, supplier_name=it.supplier_name,
        contract_no=it.contract_no, project_no=it.project_no, delivery_no=it.delivery_no,
        item_name=it.item_name, spec=it.spec, qty=it.qty, unit_price=it.unit_price,
        recv_amount=round(recv, 2), invoice_date=it.invoice_date, tax_rate=it.tax_rate,
        invoice_amount=round(inv, 2), to_invoice=round(to_inv, 2),
        pay_date=it.pay_date, pay_amount=round(paid, 2), owed=round(owed, 2),
        buyer_uid=it.buyer_uid, buyer_name=names.get(it.buyer_uid) if it.buyer_uid else None,
        note=it.note, created_at=it.created_at,
    )


@router.get("/items", response_model=schemas.PurchaseItemListOut)
async def list_items(
    supplier_id: Optional[int] = Query(None),
    project_no: Optional[str] = Query(None),
    buyer_uid: Optional[int] = Query(None),
    recon_status: Optional[str] = Query(None),
    month: Optional[str] = Query(None),        # YYYY-MM 按送货时间
    kw: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    current: models.User = Depends(require_roles("buyer")),
    db: AsyncSession = Depends(get_db),
):
    """采购明细列表（=年度采购明细表）。可按供应商/项目/采购员/对账状态/月份/关键词筛选。"""
    q = select(models.PurchaseItem)
    if not _all_view(current):
        q = q.where(models.PurchaseItem.buyer_uid == current.id)
    elif buyer_uid:
        q = q.where(models.PurchaseItem.buyer_uid == buyer_uid)
    if supplier_id:
        q = q.where(models.PurchaseItem.supplier_id == supplier_id)
    if recon_status:
        q = q.where(models.PurchaseItem.recon_status == recon_status)
    res = await db.execute(q.order_by(models.PurchaseItem.delivery_date.desc(),
                                      models.PurchaseItem.id.desc()))
    items = list(res.scalars().all())
    if project_no:
        p = project_no.strip()
        items = [it for it in items if p in (it.project_no or "")]
    if month and month.strip():
        m = month.strip()
        items = [it for it in items if _ym(it.delivery_date) == m]
    if kw:
        k = kw.strip()
        items = [it for it in items if any(
            k in (v or "") for v in (it.item_name, it.spec, it.delivery_no, it.project_no,
                                     it.supplier_name, it.contract_no))]

    recv_t = sum((it.recv_amount or 0) for it in items)
    inv_t = sum((it.invoice_amount or 0) for it in items)
    paid_t = sum((it.pay_amount or 0) for it in items)
    total = len(items)
    start = (page - 1) * page_size
    page_items = items[start:start + page_size]
    names = await _name_map(db, {it.buyer_uid for it in page_items})
    return schemas.PurchaseItemListOut(
        rows=[_to_row(it, names) for it in page_items], total=total,
        recv_total=round(recv_t, 2), invoiced=round(inv_t, 2),
        to_invoice=round(recv_t - inv_t, 2), paid=round(paid_t, 2), owed=round(recv_t - paid_t, 2),
    )


@router.post("/items", response_model=schemas.PurchaseItemRow)
async def create_item(
    data: schemas.PurchaseItemIn,
    current: models.User = Depends(require_roles("buyer")),
    db: AsyncSession = Depends(get_db),
):
    s = await _supplier_or_404(db, data.supplier_id)
    # 采购员录入默认归属本人；admin/manager 可指定
    buyer_uid = current.id
    if _all_view(current) and data.buyer_uid is not None:
        buyer_uid = data.buyer_uid or None
    recv = data.recv_amount
    if recv is None:
        recv = (data.qty or 0) * (data.unit_price or 0) if (data.qty and data.unit_price) else 0.0
    rs = (data.recon_status or "待对账").strip()
    it = models.PurchaseItem(
        recon_status=rs if rs in RECON_STATUSES else "待对账",
        delivery_date=_norm_date(data.delivery_date),
        supplier_id=s.id, supplier_name=s.name,
        contract_no=(data.contract_no or "").strip() or None,
        project_no=(data.project_no or "").strip() or None,
        delivery_no=(data.delivery_no or "").strip() or None,
        item_name=(data.item_name or "").strip() or None,
        spec=(data.spec or "").strip() or None,
        qty=data.qty, unit_price=data.unit_price, recv_amount=recv or 0.0,
        invoice_date=_norm_date(data.invoice_date), tax_rate=(data.tax_rate or "").strip() or None,
        invoice_amount=data.invoice_amount or 0.0,
        pay_date=_norm_date(data.pay_date), pay_amount=data.pay_amount or 0.0,
        buyer_uid=buyer_uid, note=(data.note or "").strip() or None, created_by=current.id,
    )
    db.add(it)
    await db.commit()
    await write_audit(db, user=current, action="create", target_type="purchase_item",
                      target_id=it.id, detail=f"{s.name} {it.item_name or ''}")
    names = await _name_map(db, {it.buyer_uid})
    return _to_row(it, names)


@router.put("/items/{iid}", response_model=schemas.PurchaseItemRow)
async def update_item(
    iid: int, data: schemas.PurchaseItemIn,
    current: models.User = Depends(require_roles("buyer")),
    db: AsyncSession = Depends(get_db),
):
    it = await _item_or_404(db, iid)
    if not _all_view(current) and it.buyer_uid != current.id:
        raise HTTPException(403, "只能编辑本人录入的采购明细")
    if data.supplier_id and data.supplier_id != it.supplier_id:
        s = await _supplier_or_404(db, data.supplier_id)
        it.supplier_id = s.id
        it.supplier_name = s.name
    it.delivery_date = _norm_date(data.delivery_date)
    it.invoice_date = _norm_date(data.invoice_date)
    it.pay_date = _norm_date(data.pay_date)
    for f in ("contract_no", "project_no", "delivery_no", "item_name", "spec", "tax_rate", "note"):
        v = getattr(data, f)
        it.__setattr__(f, (v.strip() or None) if isinstance(v, str) else v)
    it.qty = data.qty
    it.unit_price = data.unit_price
    if data.recv_amount is not None:
        it.recv_amount = data.recv_amount
    elif data.qty and data.unit_price:
        it.recv_amount = data.qty * data.unit_price
    if data.invoice_amount is not None:
        it.invoice_amount = data.invoice_amount
    if data.pay_amount is not None:
        it.pay_amount = data.pay_amount
    if data.recon_status and data.recon_status.strip() in RECON_STATUSES:
        it.recon_status = data.recon_status.strip()
    if _all_view(current) and data.buyer_uid is not None:
        it.buyer_uid = data.buyer_uid or None
    await db.commit()
    await write_audit(db, user=current, action="update", target_type="purchase_item", target_id=iid)
    names = await _name_map(db, {it.buyer_uid})
    return _to_row(it, names)


@router.delete("/items/{iid}", response_model=schemas.Msg)
async def delete_item(
    iid: int,
    current: models.User = Depends(require_roles("buyer")),
    db: AsyncSession = Depends(get_db),
):
    it = await _item_or_404(db, iid)
    if not _all_view(current) and it.buyer_uid != current.id:
        raise HTTPException(403, "只能删除本人录入的采购明细")
    await db.delete(it)
    await db.commit()
    await write_audit(db, user=current, action="delete", target_type="purchase_item", target_id=iid)
    return schemas.Msg(message="已删除")


async def _batch_load(db: AsyncSession, ids: list[int], current: models.User) -> list[models.PurchaseItem]:
    if not ids:
        raise HTTPException(400, "未选择明细")
    res = await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.id.in_(ids)))
    items = list(res.scalars().all())
    if not _all_view(current):
        items = [it for it in items if it.buyer_uid == current.id]
    return items


@router.post("/items/batch-invoice", response_model=schemas.Msg)
async def batch_invoice(
    data: schemas.BatchSettleIn,
    current: models.User = Depends(require_roles("buyer")),
    db: AsyncSession = Depends(get_db),
):
    """按勾选批量「全额开票」：开票金额=收货金额，回填开票日期。"""
    items = await _batch_load(db, data.ids, current)
    d = _norm_date(data.date) or date.today().strftime("%Y-%m-%d")
    n = 0
    for it in items:
        it.invoice_amount = it.recv_amount or 0.0
        it.invoice_date = d
        n += 1
    await db.commit()
    return schemas.Msg(message=f"已登记开票 {n} 条")


@router.post("/items/batch-pay", response_model=schemas.Msg)
async def batch_pay(
    data: schemas.BatchSettleIn,
    current: models.User = Depends(require_roles("buyer")),
    db: AsyncSession = Depends(get_db),
):
    """按勾选批量「全额付款」：付款金额=收货金额，回填付款日期，并标记已对账。"""
    items = await _batch_load(db, data.ids, current)
    d = _norm_date(data.date) or date.today().strftime("%Y-%m-%d")
    n = 0
    for it in items:
        it.pay_amount = it.recv_amount or 0.0
        it.pay_date = d
        it.recon_status = "已对账"
        n += 1
    await db.commit()
    return schemas.Msg(message=f"已登记付款 {n} 条")


@router.post("/items/batch-reconcile", response_model=schemas.Msg)
async def batch_reconcile(
    data: schemas.BatchSettleIn,
    current: models.User = Depends(require_roles("buyer")),
    db: AsyncSession = Depends(get_db),
):
    """按勾选批量标记「已对账」。"""
    items = await _batch_load(db, data.ids, current)
    for it in items:
        it.recon_status = "已对账"
    await db.commit()
    return schemas.Msg(message=f"已对账 {len(items)} 条")


# ============================ 汇总报表 ============================
@router.get("/summary", response_model=schemas.ProcureSummary)
async def summary(
    year: Optional[str] = Query(None),        # 趋势/排名所用年度，默认当年
    current: models.User = Depends(require_roles("buyer")),
    db: AsyncSession = Depends(get_db),
):
    """汇总报表：本月/本季/本年采购额 + 应付总额 + 当年月度趋势 + 按采购员 + Top供应商。"""
    items = await _visible_items(db, current)
    today = date.today()
    yr = (year or "").strip() or f"{today.year}"
    cur_month = today.strftime("%Y-%m")
    cur_q = (today.month - 1) // 3
    q_months = {f"{today.year}-{m:02d}" for m in range(cur_q * 3 + 1, cur_q * 3 + 4)}

    month_total = quarter_total = year_total = recv_total = owed_total = 0.0
    monthly: dict[str, list[float]] = {f"{yr}-{m:02d}": [0.0, 0.0, 0.0] for m in range(1, 13)}
    by_buyer: dict[Optional[int], list[float]] = {}     # uid -> [recv, paid, owed, count]
    by_supplier: dict[str, list[float]] = {}            # name -> [recv, owed]

    for it in items:
        recv, inv, to_inv, paid, owed = _amts(it)
        ym = _ym(it.delivery_date)
        recv_total += recv
        owed_total += owed
        if ym == cur_month:
            month_total += recv
        if ym in q_months:
            quarter_total += recv
        if ym and ym[:4] == yr:
            year_total += recv
            b = monthly.get(ym)
            if b:
                b[0] += recv; b[1] += inv; b[2] += paid
        # 按采购员（当年）
        if ym and ym[:4] == yr:
            bb = by_buyer.setdefault(it.buyer_uid, [0.0, 0.0, 0.0, 0])
            bb[0] += recv; bb[1] += paid; bb[2] += owed; bb[3] += 1
            sname = it.supplier_name or "(未命名)"
            sa = by_supplier.setdefault(sname, [0.0, 0.0])
            sa[0] += recv; sa[1] += owed

    names = await _name_map(db, {uid for uid in by_buyer if uid})
    monthly_rows = [schemas.TrendBucket(key=k, recv=round(v[0], 2), invoiced=round(v[1], 2),
                                        paid=round(v[2], 2), owed=round(v[0] - v[2], 2))
                    for k, v in sorted(monthly.items())]
    buyer_rows = [schemas.BuyerStat(key=(names.get(uid) or "(未指派)"),
                                    recv=round(v[0], 2), paid=round(v[1], 2),
                                    owed=round(v[2], 2), count=v[3])
                  for uid, v in by_buyer.items()]
    buyer_rows.sort(key=lambda x: -x.recv)
    supp_rows = [schemas.SupplierStat(key=k, recv=round(v[0], 2), owed=round(v[1], 2))
                 for k, v in by_supplier.items()]
    supp_rows.sort(key=lambda x: -x.recv)
    return schemas.ProcureSummary(
        month_total=round(month_total, 2), quarter_total=round(quarter_total, 2),
        year_total=round(year_total, 2), recv_total=round(recv_total, 2),
        owed_total=round(owed_total, 2), monthly=monthly_rows,
        by_buyer=buyer_rows, top_suppliers=supp_rows[:10],
    )
