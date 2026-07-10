"""🆕 v3 M09 财务部：待开票/已开票（查 sales_ledger）+ 售后费用（查 aftersales 已审批）。

发票上传端点复用 sales_router 的 /api/sales/ledger/{id}/invoice-upload（finance 角色）。
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from .. import models, schemas
from ..deps import require_roles

router = APIRouter(prefix="/api/finance", tags=["财务部"])


def _uname(u: Optional[models.User]) -> Optional[str]:
    return (u.full_name or u.username) if u else None


async def _invoice_rows(db: AsyncSession, state: str) -> list[schemas.FinanceInvoiceRow]:
    r = await db.execute(
        select(models.SalesLedger).join(models.Project)
        .where(models.SalesLedger.invoice_state == state,
               models.Project.is_deleted == False)  # noqa: E712
        .order_by(models.SalesLedger.id.desc())
    )
    leds = list(r.scalars().all())
    att_ids = set()
    for l in leds:
        att_ids.update(x for x in (l.invoice_apply_file_id, l.invoice_file_id) if x)
    names: dict[int, str] = {}
    if att_ids:
        r2 = await db.execute(select(models.Attachment).where(models.Attachment.id.in_(att_ids)))
        names = {a.id: a.name for a in r2.scalars().all()}
    rows = []
    for l in leds:
        p = l.project
        rows.append(schemas.FinanceInvoiceRow(
            ledger_id=l.id, code=p.code if p else "", name=p.name if p else "",
            customer=l.customer, sales_name=_uname(l.sales_user),
            amount=l.amount or 0, tax_rate=l.tax_rate,
            invoice_batch_id=l.invoice_batch_id,
            apply_file_id=l.invoice_apply_file_id,
            apply_file_name=names.get(l.invoice_apply_file_id),
            invoice_file_id=l.invoice_file_id,
            invoice_file_name=names.get(l.invoice_file_id),
        ))
    return rows


@router.get("/pending-invoices", response_model=List[schemas.FinanceInvoiceRow])
async def pending_invoices(
    _: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    return await _invoice_rows(db, "pending_invoice")


@router.get("/invoiced", response_model=List[schemas.FinanceInvoiceRow])
async def invoiced(
    _: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    return await _invoice_rows(db, "invoiced")


@router.get("/payment-requests", response_model=list[schemas.PaymentRequestOut])
async def finance_payment_requests(
    status: str = "pending",
    _: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    """财务待审请款列表（默认只看 pending，可传 approved/paid/rejected/all）"""
    from ..routers.purchase_mgmt_router import _pr_out
    stmt = select(models.PaymentRequest).order_by(models.PaymentRequest.created_at.desc())
    if status != "all":
        stmt = stmt.where(models.PaymentRequest.status == status)
    r = await db.execute(stmt)
    return [await _pr_out(db, pr.id) for pr in r.scalars().all()]


@router.get("/aftersales", response_model=schemas.AfterSalesListOut)
async def finance_aftersales(
    _: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    """财务部「售后费用」tab：售后部已审批的记录（含费用/问题/物料清单）。"""
    from .aftersales_router import _rows
    r = await db.execute(
        select(models.AfterSales).where(models.AfterSales.status == "approved")
        .order_by(models.AfterSales.id.desc())
    )
    items = list(r.scalars().all())
    rows = await _rows(db, items)
    stats = schemas.AfterSalesStats(
        total=len(items), pending=0,
        approved_cost=sum(a.cost or 0 for a in items),
        total_cost=sum(a.cost or 0 for a in items),
    )
    return schemas.AfterSalesListOut(rows=rows, stats=stats)


# ==================== 🆕 支出总览（所有花销在财务部一处看） ====================
# 盈利改善规划第一档(项目毛利/成本黑洞审计)的第一块：先把全公司的钱花在哪按月汇总到一张表。
# 口径：采购付款(purchase_items.paid_amount,按付款日期) + 安装/售后费用(aftersales已审批,按审批时间)
#      + OA费用(业务/报销大类已审批,按最后更新时间;核定金额优先)。
# 材料领用成本是"项目成本"口径(不重复计——钱已在采购付款里),归项目毛利榜(待办)。
@router.get("/expense-overview")
async def expense_overview(
    year: Optional[int] = Query(None, description="年份，默认今年"),
    current: models.User = Depends(require_roles("finance", "finance_lead")),
    db: AsyncSession = Depends(get_db),
):
    from datetime import date as _d
    y = year or int(_d.today().isoformat()[:4])
    ys = str(y)
    months = [f"{ys}-{m:02d}" for m in range(1, 13)]
    buckets: dict = {m: {"purchase": 0.0, "aftersales": 0.0, "oa": 0.0} for m in months}
    undated = {"purchase": 0.0, "aftersales": 0.0, "oa": 0.0}

    def put(kind: str, month: Optional[str], amt: float) -> None:
        if not amt:
            return
        if month and month in buckets:
            buckets[month][kind] += amt
        elif not month:
            undated[kind] += amt   # 已付但没记日期的,单独一行提示补日期

    # ① 采购付款（含整单维护直接记的已付款）
    r = await db.execute(select(models.PurchaseItem.paid_amount, models.PurchaseItem.paid_date)
                         .where(models.PurchaseItem.paid_amount > 0))
    for amt, pd in r.all():
        m = (pd or "")[:7] or None
        if m and not m.startswith(ys):
            continue
        put("purchase", m, float(amt or 0))

    # ② 安装/售后费用（已审批）
    r = await db.execute(select(models.AfterSales.cost, models.AfterSales.appr_at, models.AfterSales.created_at)
                         .where(models.AfterSales.status == "approved", models.AfterSales.cost > 0))
    for cost, appr, created in r.all():
        dt = appr or created
        m = dt.strftime("%Y-%m") if dt else None
        if m and not m.startswith(ys):
            continue
        put("aftersales", m, float(cost or 0))

    # ③ OA 费用（业务/报销大类，已审批；核定金额优先。采购大类不计——避免与①采购付款双算）
    r = await db.execute(select(models.OaRequest.amount, models.OaRequest.settle_amount, models.OaRequest.updated_at)
                         .where(models.OaRequest.status == "approved",
                                models.OaRequest.category.in_(("business", "reimbursement"))))
    for amt, settle, upd in r.all():
        val = settle if settle is not None else amt
        m = upd.strftime("%Y-%m") if upd else None
        if m and not m.startswith(ys):
            continue
        put("oa", m, float(val or 0))

    rows = [{"month": m, **{k: round(v, 2) for k, v in buckets[m].items()},
             "total": round(sum(buckets[m].values()), 2)} for m in months]
    totals = {k: round(sum(b[k] for b in buckets.values()) + undated[k], 2)
              for k in ("purchase", "aftersales", "oa")}
    totals["grand"] = round(sum(totals.values()), 2)
    und = {k: round(v, 2) for k, v in undated.items()}
    und["total"] = round(sum(undated.values()), 2)
    return {"year": y, "rows": rows, "undated": und, "totals": totals}
