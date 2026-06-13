"""🆕 v3 M16 导出审批：申请 → 管理层审批 → 永久放行（can_export）。

可逆开关 settings.export_approval_enabled 默认关闭——关闭时所有导出与现状一致，
本路由的申请/审批仍可用但导出端点不拦截（开关打开后才生效）。
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from .. import models, schemas
from ..config import settings
from ..deps import get_current_user, require_admin_or_manager
from ..notify import push_message
from ..utils import write_audit

router = APIRouter(prefix="/api/export-requests", tags=["导出审批"])


def _uname(u: Optional[models.User]) -> Optional[str]:
    return (u.full_name or u.username) if u else None


@router.get("/config", response_model=schemas.ExportConfigOut)
async def export_config(current: models.User = Depends(get_current_user)):
    """前端据此决定导出按钮行为（开关关=直接导出；开=无权时引导申请）。"""
    is_mgr = current.role and current.role.code in ("admin", "manager")
    return schemas.ExportConfigOut(
        enabled=settings.export_approval_enabled,
        # 开关关闭时导出不受限 → 视为可导出；开启时仅管理层或已获权
        can_export=bool((not settings.export_approval_enabled) or is_mgr or getattr(current, "can_export", False)),
    )


@router.post("", response_model=schemas.Msg)
async def create_request(
    data: schemas.ExportRequestIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 查重：已有待审批
    r = await db.execute(select(models.ExportRequest).where(
        models.ExportRequest.user_id == current.id,
        models.ExportRequest.status == "pending"))
    if r.scalar_one_or_none():
        raise HTTPException(400, "你已有待审批的导出申请")
    req = models.ExportRequest(user_id=current.id, scope=data.scope.strip() or "数据导出")
    db.add(req)
    await db.commit()
    await push_message(db, to_role="manager", kind="info",
                       text=f"【导出申请】{_uname(current)} 申请导出权限：{req.scope}",
                       biz_type="export_request", biz_id=req.id)
    return schemas.Msg(message="导出申请已提交，等待管理层审批")


@router.get("", response_model=List[schemas.ExportRequestOut])
async def list_requests(
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.ExportRequest).order_by(models.ExportRequest.id.desc()).limit(300))
    out = []
    for req in r.scalars().all():
        out.append(schemas.ExportRequestOut(
            id=req.id, user_id=req.user_id, user_name=_uname(req.user),
            user_role=req.user.role.name if (req.user and req.user.role) else None,
            scope=req.scope, status=req.status, created_at=req.created_at))
    return out


@router.post("/{rid}/approve", response_model=schemas.Msg)
async def approve(
    rid: int,
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.ExportRequest).where(models.ExportRequest.id == rid))
    req = r.scalar_one_or_none()
    if not req:
        raise HTTPException(404, "申请不存在")
    if req.status != "pending":
        raise HTTPException(400, "该申请不在待审批状态")
    req.status = "approved"
    req.appr_by = current.id
    # 永久放行
    r = await db.execute(select(models.User).where(models.User.id == req.user_id))
    u = r.scalar_one_or_none()
    if u:
        u.can_export = True
    await db.commit()
    await push_message(db, to_user_id=req.user_id, kind="info",
                       text=f"【导出已批准】你的导出权限已获批，现在可以导出数据。",
                       biz_type="export_request", biz_id=rid)
    await write_audit(db, user=current, action="export_approve", target_type="export_request", target_id=rid)
    return schemas.Msg(message="已批准，该用户获得导出权限")


@router.post("/{rid}/reject", response_model=schemas.Msg)
async def reject(
    rid: int,
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.ExportRequest).where(models.ExportRequest.id == rid))
    req = r.scalar_one_or_none()
    if not req:
        raise HTTPException(404, "申请不存在")
    if req.status != "pending":
        raise HTTPException(400, "该申请不在待审批状态")
    req.status = "rejected"
    req.appr_by = current.id
    await db.commit()
    await push_message(db, to_user_id=req.user_id, kind="warn",
                       text="【导出被驳回】你的导出申请被管理层驳回。",
                       biz_type="export_request", biz_id=rid)
    return schemas.Msg(message="已驳回")
