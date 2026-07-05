"""🆕 采购管理模块：供应商档案 / 采购明细 / 账目一览 / 请款流程 / 汇总报表 / 历史导入"""
from datetime import datetime, timezone, date as _date, datetime as _dt
from io import BytesIO
from typing import Optional, List
from collections import defaultdict
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from .. import models, schemas
from ..deps import require_roles, get_current_user
from ..utils import write_audit

router = APIRouter(prefix="/api/purchase-mgmt", tags=["采购管理"])

_PURCHASE_ROLES = ("buyer", "buyer_lead", "finance", "buyer_standard", "buyer_outsource")
# 🆕 采购员（标准件/外协）也可新增/编辑/删除采购明细与供应商
_WRITE_ROLES = ("buyer", "buyer_lead", "buyer_standard", "buyer_outsource")
# 🆕 采购收货入库：仓库填送货单号/到货日期/后填价格 → 自动入库。
# 收货归仓库（采购只下单、不收货）；admin/manager 由 require_roles 自动放行
_RECEIVE_ROLES = ("warehouse", "warehouse_lead")

# 🆕 R4/A6：采购员按清单分工（沿用采购部项目目录「按人分表」的可见性）。
# 仅限这三名采购员各管自己的清单；其他采购员 + 采购主管 + admin/manager 不受限（看全部）。
_BUYER_SHEET_MAP: dict[str, set[str]] = {
    "lixinxin": {"standard", "elec_po"},   # 李新新：标准件清单 + 电工采购单
    "wangqin": {"material", "laser"},       # 王芹：不锈钢原料下料单 + 激光件清单
    "fangbusen": {"outsource"},             # 方步森：外协加工
}


def _allowed_sheet_keys(user: Optional[models.User]) -> Optional[set[str]]:
    """该采购员可下单的清单集合；None = 不限制（看全部清单）。"""
    if not user:
        return None
    return _BUYER_SHEET_MAP.get((user.username or "").lower())


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
    category: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=10, le=500),
    current: models.User = Depends(require_roles(*_PURCHASE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """采购明细（服务端分页：总条数用 /items/summary 的 count，随筛选联动）。"""
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
    if category:
        # 🆕 按供应商分类筛选（明细本身无分类，取供应商分类）
        stmt = stmt.where(models.PurchaseItem.supplier_id.in_(
            select(models.Supplier.id).where(models.Supplier.category == category)))
    r = await db.execute(stmt.limit(page_size).offset((page - 1) * page_size))
    return await _attach_pay_status(db, [_item_out(i) for i in r.scalars().all()])


@router.get("/orders/{po_no}", response_model=List[schemas.PurchaseItemOut])
async def get_order(
    po_no: str,
    current: models.User = Depends(require_roles(*_PURCHASE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """按采购单号取整单明细（补打印采购单用）。"""
    r = await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.po_no == po_no)
                         .order_by(models.PurchaseItem.id))
    rows = list(r.scalars().all())
    if not rows:
        raise HTTPException(404, "采购单不存在")
    return await _attach_pay_status(db, [_item_out(x) for x in rows])


async def _attach_pay_status(db: AsyncSession, outs: list) -> list:
    """按 已付金额 + 关联请款单状态 计算每条采购明细的付款状态（B1=a：记录付款才算已付）。
    优先级：已付款 > 部分付款 > 已批待付 > 已请款 > 未付款。"""
    if not outs:
        return outs
    ids = [o.id for o in outs]
    # 每条明细关联到的请款单状态集合
    r = await db.execute(
        select(models.PaymentRequestItem.item_id, models.PaymentRequest.status)
        .join(models.PaymentRequest, models.PaymentRequestItem.request_id == models.PaymentRequest.id)
        .where(models.PaymentRequestItem.item_id.in_(ids))
    )
    pr_by_item: dict[int, set] = {}
    for item_id, st in r.all():
        pr_by_item.setdefault(item_id, set()).add(st)
    for o in outs:
        prs = pr_by_item.get(o.id, set())
        recv = o.received_amount or 0
        paid = o.paid_amount or 0
        if recv > 0 and paid >= recv - 0.005:
            o.pay_status = "已付款"
        elif paid > 0:
            o.pay_status = "部分付款"
        elif "approved" in prs:
            o.pay_status = "已批待付"
        elif "pending" in prs:
            o.pay_status = "已请款"
        else:
            o.pay_status = "未付款"
    return outs


def _item_out(i: models.PurchaseItem) -> schemas.PurchaseItemOut:
    return schemas.PurchaseItemOut(
        id=i.id, po_no=i.po_no, supplier_id=i.supplier_id,
        supplier_name=i.supplier.name if i.supplier else "",
        delivery_date=i.delivery_date, contract_no=i.contract_no,
        project_code=i.project_code, delivery_note_no=i.delivery_note_no,
        arrival_date=i.arrival_date,
        item_name=i.item_name, spec=i.spec, brand=i.brand, qty=i.qty, unit_price=i.unit_price,
        received_amount=i.received_amount or 0,
        invoice_date=i.invoice_date, tax_rate=i.tax_rate,
        invoice_amount=i.invoice_amount or 0,
        paid_amount=i.paid_amount or 0, paid_date=i.paid_date,
        payment_method=i.payment_method, prepay_ratio=i.prepay_ratio,
        invoice_status=i.invoice_status,
        custom_values=i.custom_values or {},
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
    data["custom_values"] = await _clean_custom(db, data.get("custom_values"))
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
        cv = await _clean_custom(db, ln.custom_values)
        db.add(models.PurchaseItem(
            po_no=po_no,
            supplier_id=body.supplier_id,
            delivery_date=body.delivery_date,
            contract_no=body.contract_no,
            project_code=(ln.project_code or body.project_code),
            item_name=ln.item_name.strip(),
            spec=ln.spec, brand=ln.brand, qty=ln.qty, unit_price=ln.unit_price,
            received_amount=recv or 0,
            tax_rate=ln.tax_rate, notes=ln.notes,
            payment_method=body.payment_method, prepay_ratio=body.prepay_ratio,
            custom_values=cv,
            buyer_id=current.id,
        ))
    await db.commit()
    r = await db.execute(
        select(models.PurchaseItem).where(models.PurchaseItem.po_no == po_no)
        .order_by(models.PurchaseItem.id)
    )
    return [_item_out(x) for x in r.scalars().all()]


# ==================== 清单 → 采购下单 / 回写清单 ====================
_STD_SHEET = "标准件清单"

# 🆕 R1：可从清单下单的 5 张来源表 + 各表列名映射（列名各表不同）
# key: (表名, 名称列, 规格列, 数量列|None, 品牌列|None, 下单日期回写列, 到货日期回写列, 仓库签字回写列)
_PURCHASABLE_SHEETS = {
    "standard":  ("标准件清单",       "项目",     "规格型号", "数量", "品牌", "订购日期", "到货日期", "仓库签字"),
    "elec_po":   ("电工采购单",       "项目",     "规格型号", "数量", "品牌", "订购日期", "到货日期", "仓库签字"),
    "material":  ("不锈钢原料下料单", "材料类别", "规格型号", "数量", None,   "下单日期", "到料日期", "仓库"),
    "outsource": ("外协加工",         "名称",     "图纸名称", None,   None,   "发出日期", "到货日期", "仓库"),
    "laser":     ("激光件清单",       "名称",     "图纸名称", None,   None,   "发出日期", "到料日期", "仓库"),
}
# 回写清单时把所有可能的「下单日期/到货日期/仓库签字」列名都写一遍，只有该表存在的列会生效
_ALL_ORDER_DATE_COLS = ("订购日期", "下单日期", "发出日期")
_ALL_ARRIVAL_COLS = ("到货日期", "到料日期")
_ALL_WH_SIGN_COLS = ("仓库签字", "仓库")


async def _sheet_fieldmap(db: AsyncSession, sheet_id: int) -> dict:
    fr = await db.execute(select(models.Field).where(models.Field.datasheet_id == sheet_id))
    return {f.name: str(f.id) for f in fr.scalars().all()}


async def _writeback_sheet_row(db: AsyncSession, sheet_id: int, record_id: int, updates: dict) -> None:
    """把 {列名: 值} 写进某数据表某行 values（按列名匹配 field_id）；列不存在则跳过。"""
    if not sheet_id or not record_id:
        return
    name2id = await _sheet_fieldmap(db, sheet_id)
    rr = await db.execute(select(models.Record).where(models.Record.id == record_id))
    rec = rr.scalar_one_or_none()
    if not rec:
        return
    vals = dict(rec.values or {})
    hit = False
    for col, val in updates.items():
        fid = name2id.get(col)
        if fid and val is not None:
            vals[fid] = val
            hit = True
    if hit:
        rec.values = vals


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


# ==================== R6：采购自定义字段 ====================
_FIELD_ADMIN_ROLES = ("buyer_lead",)   # 配置字段：采购主管（admin/manager 由 require_roles 自动放行）


async def _custom_fields(db: AsyncSession, enabled_only: bool = False):
    q = select(models.PurchaseCustomField).order_by(
        models.PurchaseCustomField.sort_order, models.PurchaseCustomField.id)
    if enabled_only:
        q = q.where(models.PurchaseCustomField.enabled == True)  # noqa: E712
    r = await db.execute(q)
    return list(r.scalars().all())


async def _clean_custom(db: AsyncSession, custom_values: Optional[dict]) -> dict:
    """校验必填、净化自定义字段值（只保留启用字段、去空）。"""
    fields = await _custom_fields(db, enabled_only=True)
    cv = custom_values or {}
    clean: dict = {}
    missing: list[str] = []
    for f in fields:
        key = str(f.id)
        val = cv.get(key)
        sval = "" if val is None else str(val).strip()
        if f.required and not sval:
            missing.append(f.label)
        elif sval:
            clean[key] = val
    if missing:
        raise HTTPException(400, f"必填自定义字段未填写：{'、'.join(missing)}")
    return clean


@router.get("/custom-fields", response_model=List[schemas.PurchaseCustomFieldOut])
async def list_custom_fields(
    current: models.User = Depends(require_roles(*_PURCHASE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """采购自定义字段列表（所有采购/财务角色可读，用于渲染列与输入框）。"""
    return [schemas.PurchaseCustomFieldOut.model_validate(f) for f in await _custom_fields(db)]


@router.post("/custom-fields", response_model=schemas.PurchaseCustomFieldOut)
async def create_custom_field(
    body: schemas.PurchaseCustomFieldIn,
    current: models.User = Depends(require_roles(*_FIELD_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    f = models.PurchaseCustomField(**body.model_dump())
    db.add(f)
    await db.commit()
    await db.refresh(f)
    await write_audit(db, user=current, action="create", target_type="purchase_custom_field", target_id=f.id)
    return schemas.PurchaseCustomFieldOut.model_validate(f)


@router.put("/custom-fields/{fid}", response_model=schemas.PurchaseCustomFieldOut)
async def update_custom_field(
    fid: int,
    body: schemas.PurchaseCustomFieldIn,
    current: models.User = Depends(require_roles(*_FIELD_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.PurchaseCustomField).where(models.PurchaseCustomField.id == fid))
    f = r.scalar_one_or_none()
    if not f:
        raise HTTPException(404, "字段不存在")
    for k, v in body.model_dump().items():
        setattr(f, k, v)
    await db.commit()
    await db.refresh(f)
    return schemas.PurchaseCustomFieldOut.model_validate(f)


@router.delete("/custom-fields/{fid}", response_model=schemas.Msg)
async def delete_custom_field(
    fid: int,
    current: models.User = Depends(require_roles(*_FIELD_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """删除字段定义（已录入明细里的历史值保留在 custom_values 中，只是不再展示/校验）。"""
    r = await db.execute(select(models.PurchaseCustomField).where(models.PurchaseCustomField.id == fid))
    f = r.scalar_one_or_none()
    if not f:
        raise HTTPException(404, "字段不存在")
    await db.delete(f)
    await db.commit()
    await write_audit(db, user=current, action="delete", target_type="purchase_custom_field", target_id=fid)
    return schemas.Msg(message="已删除该自定义字段")


@router.get("/purchasable/{project_id}", response_model=List[schemas.PurchasableRow])
async def purchasable(
    project_id: int,
    sheet: str = Query("standard", description="清单类型: standard/elec_po/material/outsource/laser"),
    current: models.User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """取项目某来源清单的可采购行 + 采购状态 + 现有库存/建议采购量。
    5 张来源表列名各异（外协/激光无数量列），按 _PURCHASABLE_SHEETS 映射取数。"""
    conf = _PURCHASABLE_SHEETS.get(sheet)
    if not conf:
        raise HTTPException(400, "未知清单类型")
    # 🆕 R4/A6：采购员只能取自己负责清单的可采购行（管理员/主管/其他采购员不限）
    allowed = _allowed_sheet_keys(current)
    if allowed is not None and sheet not in allowed:
        raise HTTPException(403, "你没有该清单的采购权限")
    sheet_name, item_col, spec_col, qty_col, brand_col = conf[:5]
    r = await db.execute(select(models.Datasheet).where(
        models.Datasheet.project_id == project_id, models.Datasheet.name == sheet_name))
    ds = r.scalar_one_or_none()
    if not ds:
        return []
    name2id = await _sheet_fieldmap(db, ds.id)
    lr = await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.source_sheet_id == ds.id))
    by_rec: dict = {}
    for pi in lr.scalars().all():
        by_rec.setdefault(pi.source_record_id, []).append(pi)
    # 🆕 现有库存：按 名称+规格 匹配物料主数据，汇总实时库存
    from .warehouse_router import _stock_map

    def _k(nm, sp):
        return ((nm or "").strip(), (sp or "").strip())

    mr = await db.execute(select(models.WhMaterial))
    mats = list(mr.scalars().all())
    stock_by_id = await _stock_map(db) if mats else {}
    stock_by_key: dict = {}
    for m in mats:
        stock_by_key[_k(m.name, m.spec)] = stock_by_key.get(_k(m.name, m.spec), 0) + \
            stock_by_id.get(m.id, m.init_stock or 0)
    rr = await db.execute(select(models.Record).where(
        models.Record.datasheet_id == ds.id).order_by(models.Record.sort_order, models.Record.id))
    out = []
    for rec in rr.scalars().all():
        v = rec.values or {}

        def gv(col):
            if not col:
                return None
            fid = name2id.get(col)
            x = v.get(fid) if fid else None
            if isinstance(x, list):
                x = "、".join(str(i) for i in x)
            return str(x).strip() if x not in (None, "") else None

        name = gv(item_col)
        if not name:
            continue
        pis = by_rec.get(rec.id, [])
        if not pis:
            status = "未下单"
        elif all(p.arrival_date for p in pis):
            status = "已到货"
        else:
            status = "已下单"
        brand = gv(brand_col)
        extra = "，".join(f"{c}:{gv(c)}" for c in ("材质",) if gv(c))
        spec = gv(spec_col)
        qty = _num(gv(qty_col)) if qty_col else None
        stock = round(stock_by_key.get(_k(name, spec), 0), 4)
        suggest = round(max(0.0, (qty or 0) - stock), 4) if qty is not None else 0
        out.append(schemas.PurchasableRow(
            sheet_id=ds.id, record_id=rec.id, item_name=name, spec=spec, brand=brand,
            qty=qty, stock=stock, suggest_purchase=suggest,
            notes=(extra or gv("备注")), status=status))
    return out


@router.post("/orders/from-list", response_model=List[schemas.PurchaseItemOut])
async def create_order_from_list(
    body: schemas.OrderFromListCreate,
    current: models.User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """从清单选定行 + 选供应商 → 生成一张采购单；回写清单的订购日期/采购负责人。"""
    lines = [l for l in body.lines if (l.item_name or "").strip()]
    if not lines:
        raise HTTPException(400, "请至少选择一行")
    # 🆕 R4/A6：采购员只能对自己负责清单下单（管理员/主管/其他采购员不限）
    allowed = _allowed_sheet_keys(current)
    if allowed is not None:
        sheet_ids = {l.source_sheet_id for l in lines if l.source_sheet_id}
        if sheet_ids:
            name2key = {conf[0]: k for k, conf in _PURCHASABLE_SHEETS.items()}
            dsr = await db.execute(select(models.Datasheet).where(models.Datasheet.id.in_(sheet_ids)))
            for ds in dsr.scalars().all():
                key = name2key.get(ds.name)
                if key and key not in allowed:
                    raise HTTPException(403, f"你没有「{ds.name}」的采购权限")
    po_no = await _next_po_no(db)
    today = _date.today().isoformat()
    uname = current.full_name or current.username
    for l in lines:
        recv = None
        if l.qty and l.unit_price:
            recv = round((l.qty or 0) * (l.unit_price or 0), 4)
        db.add(models.PurchaseItem(
            po_no=po_no, source_sheet_id=l.source_sheet_id, source_record_id=l.source_record_id,
            supplier_id=body.supplier_id, delivery_date=body.delivery_date, project_code=body.project_code,
            item_name=l.item_name.strip(), spec=l.spec, brand=l.brand, qty=l.qty, unit_price=l.unit_price,
            received_amount=recv or 0, payment_method=(l.payment_method or body.payment_method),
            prepay_ratio=(l.prepay_ratio if l.prepay_ratio is not None else body.prepay_ratio),
            notes=l.notes, buyer_id=current.id))
        if l.source_sheet_id and l.source_record_id:
            # 各来源表的「下单日期」列名不同（订购/下单/发出日期），全写一遍只有存在的列生效
            wb = {"采购负责人": uname}
            for c in _ALL_ORDER_DATE_COLS:
                wb[c] = (body.delivery_date or today)
            await _writeback_sheet_row(db, l.source_sheet_id, l.source_record_id, wb)
    await db.commit()
    r = await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.po_no == po_no)
                         .order_by(models.PurchaseItem.id))
    return [_item_out(x) for x in r.scalars().all()]


async def _auto_stock_in(db: AsyncSession, item: models.PurchaseItem, current: models.User) -> None:
    """采购收货 → 自动生成一笔入库（带采购单价/金额）；幂等：同一采购明细只入库一次。"""
    if not item.qty or item.qty <= 0:
        return
    ex = await db.execute(select(models.WhTxn).where(
        models.WhTxn.purchase_item_id == item.id, models.WhTxn.is_reversal == False))  # noqa: E712
    if ex.scalars().first():
        return
    spec = (item.spec or "").strip() or None
    mq = select(models.WhMaterial).where(models.WhMaterial.name == item.item_name)
    mq = mq.where(models.WhMaterial.spec == spec) if spec else mq.where(models.WhMaterial.spec.is_(None))
    m = (await db.execute(mq)).scalar_one_or_none()
    if not m:
        m = models.WhMaterial(name=item.item_name, spec=spec, unit="个", category="采购入库")
        db.add(m)
        await db.flush()
    pid = None
    if item.project_code:
        pr = await db.execute(select(models.Project.id).where(models.Project.code == item.project_code))
        pid = pr.scalar_one_or_none()
    bd = item.arrival_date or _date.today().isoformat()
    from .warehouse_router import _next_ref
    ref = await _next_ref(db, "in", bd)
    up = item.unit_price
    amt = round((item.qty or 0) * up, 4) if up is not None else None
    db.add(models.WhTxn(
        material_id=m.id, biz_date=bd, direction="in", qty=item.qty,
        unit_price=up, amount=amt, purchase_item_id=item.id,
        source="采购入库", party=(item.supplier.name if item.supplier else None),
        project_id=pid, ref_no=ref, operator_id=current.id))


# ==================== 采购历史数据 一键导入 ====================
# 模板列（顺序即模板列顺序；* 为必填）。解析时按表头名匹配，允许调整列序。
_IMPORT_COLS = [
    "供应商名称*", "采购单号", "下单日期", "订单编号", "名称*", "规格型号",
    "数量", "单价", "合计金额", "送货单号", "到货日期", "付款方式",
    "开票日期", "开票金额", "税率", "对账状态", "备注",
]


def _norm_date(v) -> Optional[str]:
    if v is None or v == "":
        return None
    if isinstance(v, (_dt, _date)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip().replace(".", "-").replace("/", "-")
    return s[:10] if s else None


def _norm_num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("￥", "").replace("¥", "").strip())
    except (ValueError, TypeError):
        return None


def _norm_str(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


@router.get("/items/import-template")
async def import_template(
    current: models.User = Depends(require_roles(*_WRITE_ROLES)),
):
    """下载采购明细导入模板（.xlsx，含表头 + 示例行 + 填写说明）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook()
    ws = wb.active
    ws.title = "采购明细导入"
    ws.append(_IMPORT_COLS)
    hfill = PatternFill("solid", fgColor="E8F5EE")
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = hfill
        c.alignment = Alignment(horizontal="center", vertical="center")
    ws.append(["无锡示例金属制品有限公司", "CG20260101-001", "2026-01-05", "2026-046",
               "轴承座", "SKF-6205", 4, 35, 140, "SF1234", "2026-01-08", "转账",
               "", "", "13%", "待对账", "示例行，导入前请删除"])
    widths = [26, 16, 12, 12, 18, 16, 8, 10, 12, 14, 12, 10, 12, 12, 8, 10, 20]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "A2"

    ws2 = wb.create_sheet("填写说明")
    for line in [
        ["采购历史数据导入说明"],
        [""],
        ["1. 带 * 的列为必填：供应商名称、名称。"],
        ["2. 供应商名称若系统中不存在，会自动新建（仅带名称，其余资料后续在供应商档案补全）。"],
        ["3. 日期格式 YYYY-MM-DD（如 2026-01-05），也兼容 2026.1.5 / 2026/1/5。"],
        ["4. 单价、合计金额只填数字；合计金额留空且有数量与单价时，系统自动=数量×单价。"],
        ["5. 付款方式：现金 / 转账 / 月结 / 承兑 / 预付（也可自定义）。"],
        ["6. 对账状态：待对账 / 已对账 / 已开票（留空默认待对账）。"],
        ["7. 第 2 行为示例，导入前请删除。可一次导入多个供应商、多条明细。"],
    ]:
        ws2.append(line)
    ws2["A1"].font = Font(bold=True, size=13)
    ws2.column_dimensions["A"].width = 80

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    fname = "采购明细导入模板.xlsx"
    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


@router.post("/items/import", response_model=schemas.PurchaseImportResult)
async def import_items(
    file: UploadFile = File(...),
    current: models.User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """一键导入采购历史明细：按表头名匹配列；供应商按名称匹配，不存在则自动新建。"""
    from openpyxl import load_workbook
    try:
        wb = load_workbook(BytesIO(await file.read()), data_only=True, read_only=True)
    except Exception:
        raise HTTPException(400, "无法解析该文件，请使用模板导出的 .xlsx")
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        raise HTTPException(400, "文件为空或只有表头")
    header = [(_norm_str(c) or "").replace("*", "").strip() for c in rows[0]]
    col = {name: idx for idx, name in enumerate(header) if name}

    def cell(row, name):
        i = col.get(name)
        return row[i] if (i is not None and i < len(row)) else None

    if "供应商名称" not in col or "名称" not in col:
        raise HTTPException(400, "表头缺少必填列「供应商名称」或「名称」，请用模板")

    # 供应商名称 -> id 缓存（一次性载入现有）
    sres = await db.execute(select(models.Supplier.id, models.Supplier.name))
    sup_map = {n: i for i, n in sres.all()}

    created = 0
    suppliers_created = 0
    errors: list[str] = []
    for rn, row in enumerate(rows[1:], start=2):
        if row is None or all(c is None or str(c).strip() == "" for c in row):
            continue
        sup_name = _norm_str(cell(row, "供应商名称"))
        item_name = _norm_str(cell(row, "名称"))
        if not sup_name or not item_name:
            errors.append(f"第 {rn} 行：供应商名称/名称为空，已跳过")
            continue
        sid = sup_map.get(sup_name)
        if sid is None:
            s = models.Supplier(name=sup_name, status="active")
            db.add(s)
            await db.flush()
            sid = s.id
            sup_map[sup_name] = sid
            suppliers_created += 1
        qty = _norm_num(cell(row, "数量"))
        unit_price = _norm_num(cell(row, "单价"))
        recv = _norm_num(cell(row, "合计金额"))
        if recv is None and qty is not None and unit_price is not None:
            recv = round(qty * unit_price, 4)
        db.add(models.PurchaseItem(
            po_no=_norm_str(cell(row, "采购单号")),
            supplier_id=sid,
            delivery_date=_norm_date(cell(row, "下单日期")),
            project_code=_norm_str(cell(row, "订单编号")),
            item_name=item_name,
            spec=_norm_str(cell(row, "规格型号")),
            qty=qty, unit_price=unit_price, received_amount=recv or 0,
            delivery_note_no=_norm_str(cell(row, "送货单号")),
            arrival_date=_norm_date(cell(row, "到货日期")),
            payment_method=_norm_str(cell(row, "付款方式")),
            invoice_date=_norm_date(cell(row, "开票日期")),
            invoice_amount=_norm_num(cell(row, "开票金额")) or 0,
            tax_rate=_norm_str(cell(row, "税率")),
            invoice_status=_norm_str(cell(row, "对账状态")) or "待对账",
            notes=_norm_str(cell(row, "备注")),
            buyer_id=current.id,
        ))
        created += 1
    await db.commit()
    return schemas.PurchaseImportResult(
        created=created, suppliers_created=suppliers_created,
        failed=len(errors), errors=errors[:50],
    )


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
    if "custom_values" in data:
        data["custom_values"] = await _clean_custom(db, data.get("custom_values"))
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
    """删除只删「采购明细」这一条记录本身，不会级联删除/冲正仓库入库或财务请款——
    如果链路已经走到那两边，直接物理删除会让仓库库存虚高、财务的钱对不上明细，所以直接拦截，
    要求先去对应模块走正常的撤销流程（仓库冲红 / 请款单处理），保证两边流水都有据可查。"""
    r = await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.id == iid))
    item = r.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "明细不存在")
    if _buyer_restricted(current) and item.buyer_id != current.id:
        raise HTTPException(403, "无权删除他人明细")
    if item.invoice_status in ("已对账", "已开票"):
        raise HTTPException(400, "已对账/已开票的明细不可删除")
    wh = await db.execute(select(models.WhTxn.id).where(
        models.WhTxn.purchase_item_id == iid, models.WhTxn.is_reversal == False,  # noqa: E712
        models.WhTxn.reversed == False))  # noqa: E712
    if wh.scalar_one_or_none():
        raise HTTPException(400, "该明细已收货入库，仓库库存已增加；请先到【仓库-出入库流水】把对应入库单冲红，再回来删除")
    pri = await db.execute(select(func.count(models.PaymentRequestItem.id)).where(
        models.PaymentRequestItem.item_id == iid))
    if pri.scalar():
        raise HTTPException(400, "该明细已被请款（含已批/已付），不可直接删除；请先到【请款记录】里处理对应请款单")
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
    # 🆕 回写清单：到货日期 / 进度(已到货) / 仓库签字
    if item.source_sheet_id and item.source_record_id:
        # 各来源表「到货日期/仓库签字」列名不同，全写一遍只有存在的列生效
        wb = {"进度": "已到货"}
        for c in _ALL_ARRIVAL_COLS:
            wb[c] = body.arrival_date
        for c in _ALL_WH_SIGN_COLS:
            wb[c] = (current.full_name or current.username)
        await _writeback_sheet_row(db, item.source_sheet_id, item.source_record_id, wb)
    # 🆕 采购收货 → 自动入库（带采购单价/金额），库存与财务成本据此计算
    await _auto_stock_in(db, item, current)
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
    r = await db.execute(stmt.limit(500))
    return await _attach_pay_status(db, [_item_out(i) for i in r.scalars().all()])


@router.get("/statements/{sid}/export")
async def export_supplier_statement(
    sid: int,
    current: models.User = Depends(require_roles(*_PURCHASE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """🆕 C6：导出某供应商采购对账单（.xlsx）：抬头 + 汇总 + 明细 + 合计。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    sr = await db.execute(select(models.Supplier).where(models.Supplier.id == sid))
    sup = sr.scalar_one_or_none()
    if not sup:
        raise HTTPException(404, "供应商不存在")
    ir = await db.execute(select(models.PurchaseItem).where(
        models.PurchaseItem.supplier_id == sid).order_by(
        models.PurchaseItem.delivery_date.asc().nullsfirst(), models.PurchaseItem.id.asc()))
    items = await _attach_pay_status(db, [_item_out(i) for i in ir.scalars().all()])
    obr = await db.execute(select(models.SupplierOpeningBalance).where(
        models.SupplierOpeningBalance.supplier_id == sid))
    ob = obr.scalar_one_or_none()
    opening = (ob.outstanding_amount if ob else 0) or 0

    recv = sum(i.received_amount or 0 for i in items)
    inv = sum(i.invoice_amount or 0 for i in items)
    paid = sum(i.paid_amount or 0 for i in items)
    uninv = sum((i.received_amount or 0) for i in items if i.invoice_status != "已开票")
    outstanding = opening + recv - paid

    wb = Workbook()
    ws = wb.active
    ws.title = "采购对账单"
    thin = Side(style="thin", color="C0C4CC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    cols = ["下单日期", "采购单号", "项目编号", "名称", "规格型号", "品牌", "数量", "单价",
            "收货金额", "开票日期", "开票金额", "已付款", "付款状态", "对账状态", "备注"]
    n = len(cols)

    def _cell(row, col, val, bold=False, fill=None, align="left", money=False):
        c = ws.cell(row=row, column=col, value=val)
        c.font = Font(bold=bold)
        c.alignment = Alignment(horizontal=align, vertical="center", wrap_text=False)
        if fill:
            c.fill = PatternFill("solid", fgColor=fill)
        if money and isinstance(val, (int, float)):
            c.number_format = "#,##0.00"
        return c

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n)
    t = _cell(1, 1, f"{sup.name}  采购对账单", bold=True, align="center")
    t.font = Font(bold=True, size=15)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=n)
    _cell(2, 1, f"分类：{sup.category or '—'}    联系人：{sup.contact or '—'}    电话：{sup.phone or '—'}",
          align="center")
    # 汇总条
    summ = (f"期初欠款：{opening:,.2f}    收货合计：{recv:,.2f}    开票合计：{inv:,.2f}    "
            f"待开票：{uninv:,.2f}    已付款：{paid:,.2f}    欠款余额：{outstanding:,.2f}")
    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=n)
    _cell(3, 1, summ, bold=True, fill="FFF7E6", align="center")

    hrow = 5
    for j, name in enumerate(cols, start=1):
        c = _cell(hrow, j, name, bold=True, fill="E8F5EE", align="center")
        c.border = border
    r0 = hrow + 1
    for k, it in enumerate(items):
        vals = [it.delivery_date or "", it.po_no or "", it.project_code or "", it.item_name,
                it.spec or "", it.brand or "", it.qty, it.unit_price, it.received_amount or 0,
                it.invoice_date or "", it.invoice_amount or 0, it.paid_amount or 0,
                it.pay_status or "未付款", it.invoice_status, it.notes or ""]
        for j, v in enumerate(vals, start=1):
            money = j in (8, 9, 11, 12)
            align = "right" if j in (7, 8, 9, 11, 12) else "center" if j in (1, 2, 3, 10, 13, 14) else "left"
            c = _cell(r0 + k, j, v, align=align, money=money)
            c.border = border
    # 合计行
    tot_row = r0 + len(items)
    _cell(tot_row, 1, "合计", bold=True, fill="F5F7FA", align="center").border = border
    for j in range(2, n + 1):
        c = _cell(tot_row, j, "", fill="F5F7FA")
        c.border = border
    _cell(tot_row, 9, recv, bold=True, fill="F5F7FA", align="right", money=True).border = border
    _cell(tot_row, 11, inv, bold=True, fill="F5F7FA", align="right", money=True).border = border
    _cell(tot_row, 12, paid, bold=True, fill="F5F7FA", align="right", money=True).border = border

    widths = [12, 15, 12, 20, 18, 10, 8, 10, 13, 12, 13, 12, 10, 10, 18]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=hrow, column=i).column_letter].width = w
    ws.freeze_panes = f"A{r0}"

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    fname = f"{sup.name}_采购对账单.xlsx"
    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


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
    voucher_name = None
    if pr.pay_voucher_file_id:
        ar = await db.execute(select(models.Attachment.name).where(
            models.Attachment.id == pr.pay_voucher_file_id))
        voucher_name = ar.scalar_one_or_none()
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
        pay_voucher_file_id=pr.pay_voucher_file_id,
        pay_voucher_name=voucher_name,
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
    paid_amount: float = Form(...),
    paid_date: str = Form(...),
    payment_method: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    current: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    """财务付款：记录金额/日期/方式，并可上传付款凭证（付款单据）。"""
    r = await db.execute(select(models.PaymentRequest).where(models.PaymentRequest.id == prid))
    pr = r.scalar_one_or_none()
    if not pr:
        raise HTTPException(404, "请款单不存在")
    if pr.status != "approved":
        raise HTTPException(400, "只有已审批的请款单可付款")
    pr.status = "paid"
    pr.paid_amount = paid_amount
    pr.paid_date = paid_date
    pr.payment_method = payment_method
    if file is not None and file.filename:
        from .attachments_router import save_upload
        att = await save_upload(db, file, biz_type="payment_voucher", biz_id=pr.id, user=current)
        await db.flush()
        pr.pay_voucher_file_id = att.id

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
                    add = round(paid_amount * ratio, 4)
                else:
                    add = round(paid_amount / len(pri_rows), 4)
                item.paid_amount = (item.paid_amount or 0) + add
                item.paid_date = paid_date

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
