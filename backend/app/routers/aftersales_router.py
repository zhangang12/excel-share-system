"""🆕 v3 M10 售后部（§十五）：登记(物料清单必传)→主管审批→自动同步财务部。

- 售后部无项目目录菜单：登记弹窗的项目下拉走 GET /aftersales/projects 轻量端点（仅 code/name）
- 审批通过 → 推送财务部角色池（含费用/问题摘要/物料清单），财务部「售后费用」tab 展示已审批记录
"""
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user, require_roles
from ..notify import push_message
from ..utils import write_audit
from .attachments_router import save_upload

router = APIRouter(prefix="/api/aftersales", tags=["售后部"])


def _uname(u: Optional[models.User]) -> Optional[str]:
    return (u.full_name or u.username) if u else None


async def _rows(db: AsyncSession, items: list[models.AfterSales]) -> list[schemas.AfterSalesRow]:
    mat_ids = [a.mat_file_id for a in items if a.mat_file_id]
    names: dict[int, str] = {}
    if mat_ids:
        r = await db.execute(select(models.Attachment).where(models.Attachment.id.in_(mat_ids)))
        names = {x.id: x.name for x in r.scalars().all()}
    uids = [a.created_by for a in items if a.created_by]
    unames: dict[int, str] = {}
    if uids:
        r = await db.execute(select(models.User).where(models.User.id.in_(uids)))
        unames = {u.id: _uname(u) for u in r.scalars().all()}
    out = []
    for a in items:
        p = a.project
        out.append(schemas.AfterSalesRow(
            id=a.id, project_id=a.project_id,
            code=p.code if p else "", name=p.name if p else "",
            problem=a.problem, cost=a.cost or 0, status=a.status,
            mat_file_id=a.mat_file_id, mat_file_name=names.get(a.mat_file_id),
            created_by_name=unames.get(a.created_by), created_at=a.created_at,
        ))
    return out


@router.get("", response_model=schemas.AfterSalesListOut)
async def list_aftersales(
    current: models.User = Depends(require_roles("as_worker", "as_lead", "finance")),
    db: AsyncSession = Depends(get_db),
):
    """售后/管理层看全部；财务只读已审批（财务页「售后费用」tab 复用此端点 status=approved 过滤）。"""
    q = select(models.AfterSales)
    if current.role and current.role.code == "finance":
        q = q.where(models.AfterSales.status == "approved")
    r = await db.execute(q.order_by(models.AfterSales.id.desc()).limit(500))
    items = list(r.scalars().all())
    rows = await _rows(db, items)
    stats = schemas.AfterSalesStats(
        total=len(items),
        pending=sum(1 for a in items if a.status == "pending"),
        approved_cost=sum(a.cost or 0 for a in items if a.status == "approved"),
        total_cost=sum(a.cost or 0 for a in items),
    )
    return schemas.AfterSalesListOut(rows=rows, stats=stats)


@router.get("/projects", response_model=List[schemas.AfterSalesProjOption])
async def project_options(
    _: models.User = Depends(require_roles("as_worker", "as_lead")),
    db: AsyncSession = Depends(get_db),
):
    """登记弹窗项目下拉（售后无目录菜单，单开轻量只读端点，仅 code/name）。"""
    r = await db.execute(
        select(models.Project.id, models.Project.code, models.Project.name)
        .where(models.Project.is_deleted == False)  # noqa: E712
        .order_by(models.Project.code)
    )
    return [schemas.AfterSalesProjOption(id=i, code=c, name=n) for i, c, n in r.all()]


@router.post("", response_model=schemas.Msg)
async def create_aftersales(
    project_id: int = Form(...),
    problem: str = Form(...),
    cost: float = Form(...),
    file: UploadFile = File(...),
    current: models.User = Depends(require_roles("as_worker")),
    db: AsyncSession = Depends(get_db),
):
    """售后员工登记：项目+问题+费用+物料清单（四项必填）→ 待审批，推售后主管。"""
    if not problem.strip():
        raise HTTPException(400, "请填写售后问题")
    if cost <= 0:
        raise HTTPException(400, "请填写售后费用")
    r = await db.execute(select(models.Project).where(
        models.Project.id == project_id, models.Project.is_deleted == False))  # noqa: E712
    p = r.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")
    a = models.AfterSales(project_id=project_id, problem=problem.strip(),
                          cost=cost, status="pending", created_by=current.id)
    db.add(a)
    await db.flush()
    att = await save_upload(db, file, biz_type="aftersales_mat", biz_id=a.id,
                            project_id=project_id, user=current)
    a.mat_file_id = att.id
    await db.commit()
    await push_message(db, to_role="as_lead", kind="info",
                       text=f"【售后待审批】{p.code} {p.name}：{problem[:20]}… 费用 ¥{cost:,.0f}",
                       biz_type="aftersales", biz_id=a.id)
    await write_audit(db, user=current, action="create", target_type="aftersales", target_id=a.id)
    return schemas.Msg(message="已登记，等待售后主管审批")


@router.post("/{aid}/approve", response_model=schemas.Msg)
async def approve(
    aid: int,
    current: models.User = Depends(require_roles("as_lead")),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.AfterSales).where(models.AfterSales.id == aid))
    a = r.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "记录不存在")
    if a.status != "pending":
        raise HTTPException(400, "该记录不在待审批状态")
    a.status = "approved"
    a.appr_by = current.id
    a.appr_at = datetime.now(timezone.utc)
    await db.commit()
    p = a.project
    mat = ""
    if a.mat_file_id:
        rr = await db.execute(select(models.Attachment).where(models.Attachment.id == a.mat_file_id))
        m = rr.scalar_one_or_none()
        if m:
            mat = f"，物料清单：{m.name}"
    await push_message(db, to_role="finance", kind="info",
                       text=f"【售后费用同步】{p.code} {p.name} 售后费用 ¥{a.cost:,.0f} 已审批{mat}，问题：{a.problem[:20]}…",
                       biz_type="aftersales", biz_id=a.id)
    await write_audit(db, user=current, action="approve", target_type="aftersales", target_id=aid)
    return schemas.Msg(message="已通过，售后费用/问题/物料清单已同步财务部")


@router.post("/{aid}/reject", response_model=schemas.Msg)
async def reject(
    aid: int,
    current: models.User = Depends(require_roles("as_lead")),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.AfterSales).where(models.AfterSales.id == aid))
    a = r.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "记录不存在")
    if a.status != "pending":
        raise HTTPException(400, "该记录不在待审批状态")
    a.status = "rejected"
    a.appr_by = current.id
    a.appr_at = datetime.now(timezone.utc)
    await db.commit()
    await write_audit(db, user=current, action="reject", target_type="aftersales", target_id=aid)
    return schemas.Msg(message="已驳回")
