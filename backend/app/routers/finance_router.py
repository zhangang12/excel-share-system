"""🆕 v3 M09 财务部：待开票/已开票（查 sales_ledger）+ 售后费用（查 aftersales 已审批）。

发票上传端点复用 sales_router 的 /api/sales/ledger/{id}/invoice-upload（finance 角色）。
"""
from typing import List, Optional

from fastapi import APIRouter, Depends
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
