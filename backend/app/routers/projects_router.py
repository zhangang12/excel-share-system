"""项目管理"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, func

from ..database import get_db
from .. import models, schemas
from ..utils import write_audit
from ..deps import (
    get_current_user, require_admin, require_not_viewer,
    user_can_view_project, user_can_edit_project,
)

router = APIRouter(prefix="/api/projects", tags=["项目"])


async def _project_to_out(p: models.Project, db: AsyncSession) -> schemas.ProjectOut:
    res = await db.execute(
        select(func.count(models.ProjectMember.id)).where(models.ProjectMember.project_id == p.id)
    )
    member_count = res.scalar() or 0
    manager_name = None
    if p.manager:
        manager_name = p.manager.full_name or p.manager.username
    return schemas.ProjectOut(
        id=p.id, code=p.code, name=p.name, description=p.description,
        status=p.status, manager_id=p.manager_id, manager_name=manager_name,
        member_count=member_count,
        created_at=p.created_at, updated_at=p.updated_at,
    )


@router.get("", response_model=List[schemas.ProjectOut])
async def list_projects(
    q: Optional[str] = Query(None),
    status: Optional[str] = None,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(models.Project).where(models.Project.is_deleted == False)
    if q:
        # 转义 LIKE 通配符（_ 和 %）和反斜杠，让搜索按字面量匹配
        q_esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{q_esc}%"
        query = query.where(or_(
            models.Project.code.like(like, escape="\\"),
            models.Project.name.like(like, escape="\\"),
        ))
    if status:
        query = query.where(models.Project.status == status)
    query = query.order_by(models.Project.updated_at.desc())
    res = await db.execute(query)
    items = res.scalars().all()
    visible = []
    for p in items:
        if await user_can_view_project(db, current, p):
            visible.append(await _project_to_out(p, db))
    return visible


@router.post("", response_model=schemas.ProjectOut)
async def create_project(
    data: schemas.ProjectCreate,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    if current.role.code not in ("admin", "manager"):
        raise HTTPException(403, "无权创建项目")
    # 只与活跃项目比较；已软删的项目允许复用其 code
    res = await db.execute(
        select(models.Project).where(
            models.Project.code == data.code,
            models.Project.is_deleted == False,
        )
    )
    if res.scalar_one_or_none():
        raise HTTPException(400, "项目编号已存在")
    p = models.Project(
        code=data.code, name=data.name, description=data.description,
        status=data.status or "进行中",
        manager_id=data.manager_id,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    res = await db.execute(select(models.Project).where(models.Project.id == p.id))
    p = res.scalar_one()
    await write_audit(db, user=current, action="create_project", target_type="project", target_id=p.id, detail=f"{p.code} {p.name}")
    return await _project_to_out(p, db)


@router.get("/{pid}", response_model=schemas.ProjectOut)
async def get_project(
    pid: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(models.Project).where(models.Project.id == pid, models.Project.is_deleted == False)
    )
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")
    if not await user_can_view_project(db, current, p):
        raise HTTPException(403, "无权访问该项目")
    return await _project_to_out(p, db)


@router.put("/{pid}", response_model=schemas.ProjectOut)
async def update_project(
    pid: int, data: schemas.ProjectUpdate,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(models.Project).where(models.Project.id == pid))
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")
    if not await user_can_edit_project(db, current, p):
        raise HTTPException(403, "无权修改项目")
    if data.name is not None: p.name = data.name
    if data.description is not None: p.description = data.description
    if data.status is not None: p.status = data.status
    if data.manager_id is not None and current.role.code in ("admin", "manager"):
        p.manager_id = data.manager_id
    await db.commit()
    await db.refresh(p)
    res = await db.execute(select(models.Project).where(models.Project.id == pid))
    p = res.scalar_one()
    return await _project_to_out(p, db)


@router.delete("/{pid}", response_model=schemas.Msg)
async def delete_project(
    pid: int,
    _: models.User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(models.Project).where(models.Project.id == pid))
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")
    orig_code = p.code
    p.is_deleted = True
    # 把 code 加前缀腾出 unique 约束，方便用同 code 重建项目
    if not p.code.startswith("_deleted_"):
        p.code = f"_deleted_{pid}_{p.code}"[:64]
    await db.commit()
    await write_audit(db, user=_, action="delete_project", target_type="project", target_id=pid, detail=orig_code)
    return schemas.Msg(message="已删除")


# ---------- 成员管理 ----------
def _member_to_out(m: models.ProjectMember) -> schemas.ProjectMemberOut:
    return schemas.ProjectMemberOut(
        id=m.id, user_id=m.user_id, username=m.user.username,
        full_name=m.user.full_name,
        role_name=m.user.role.name if m.user.role else None,
        permission=m.permission, added_at=m.added_at,
    )


@router.get("/{pid}/members", response_model=List[schemas.ProjectMemberOut])
async def list_members(
    pid: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(models.Project).where(models.Project.id == pid))
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")
    if not await user_can_view_project(db, current, p):
        raise HTTPException(403, "无权查看")
    res = await db.execute(
        select(models.ProjectMember).where(models.ProjectMember.project_id == pid)
    )
    return [_member_to_out(m) for m in res.scalars().all()]


async def _can_manage_members(db, user, project) -> bool:
    if user.role and user.role.code in ("admin", "manager"):
        return True
    return False


@router.post("/{pid}/members", response_model=schemas.ProjectMemberOut)
async def add_member(
    pid: int, data: schemas.ProjectMemberIn,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(models.Project).where(models.Project.id == pid))
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")
    if not await _can_manage_members(db, current, p):
        raise HTTPException(403, "无权添加成员")
    res = await db.execute(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == pid,
            models.ProjectMember.user_id == data.user_id,
        )
    )
    if res.scalar_one_or_none():
        raise HTTPException(400, "该用户已是项目成员")
    if data.permission not in ("edit", "view"):
        raise HTTPException(400, "权限取值必须是 edit 或 view")
    m = models.ProjectMember(project_id=pid, user_id=data.user_id, permission=data.permission)
    db.add(m)
    await db.commit()
    res = await db.execute(select(models.ProjectMember).where(models.ProjectMember.id == m.id))
    m = res.scalar_one()
    return _member_to_out(m)


@router.put("/{pid}/members/{mid}", response_model=schemas.ProjectMemberOut)
async def update_member(
    pid: int, mid: int, data: schemas.ProjectMemberIn,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(models.Project).where(models.Project.id == pid))
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")
    if not await _can_manage_members(db, current, p):
        raise HTTPException(403, "无权修改成员")
    res = await db.execute(
        select(models.ProjectMember).where(
            models.ProjectMember.id == mid, models.ProjectMember.project_id == pid
        )
    )
    m = res.scalar_one_or_none()
    if not m:
        raise HTTPException(404, "成员不存在")
    if data.permission not in ("edit", "view"):
        raise HTTPException(400, "权限取值必须是 edit 或 view")
    m.permission = data.permission
    await db.commit()
    res = await db.execute(select(models.ProjectMember).where(models.ProjectMember.id == mid))
    m = res.scalar_one()
    return _member_to_out(m)


@router.delete("/{pid}/members/{mid}", response_model=schemas.Msg)
async def remove_member(
    pid: int, mid: int,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(models.Project).where(models.Project.id == pid))
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")
    if not await _can_manage_members(db, current, p):
        raise HTTPException(403, "无权移除成员")
    res = await db.execute(
        select(models.ProjectMember).where(
            models.ProjectMember.id == mid, models.ProjectMember.project_id == pid
        )
    )
    m = res.scalar_one_or_none()
    if not m:
        raise HTTPException(404, "成员不存在")
    await db.delete(m)
    await db.commit()
    return schemas.Msg(message="已移除")
