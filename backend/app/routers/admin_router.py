"""管理后台：用户 / 部门 / 角色"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from .. import models, schemas
from ..auth import hash_password
from ..deps import require_admin, require_admin_or_manager
from ..utils import write_audit

router = APIRouter(prefix="/api/admin", tags=["管理后台"])


# ---------- 角色（只读） ----------
@router.get("/roles", response_model=List[schemas.RoleOut])
async def list_roles(
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    # admin 角色对 UI 隐身：不出现在角色下拉、字段权限对话框等任何地方
    res = await db.execute(
        select(models.Role).where(models.Role.code != "admin").order_by(models.Role.id)
    )
    return [schemas.RoleOut.model_validate(r) for r in res.scalars().all()]




# ---------- 用户 ----------
def _user_to_out(u: models.User) -> schemas.UserOut:
    return schemas.UserOut(
        id=u.id, username=u.username, full_name=u.full_name, email=u.email,
        role_id=u.role_id,
        role_code=u.role.code if u.role else None,
        role_name=u.role.name if u.role else None,
        is_active=u.is_active,
        password_must_change=u.password_must_change,
        created_at=u.created_at, last_login=u.last_login,
    )


@router.get("/users", response_model=List[schemas.UserOut])
async def list_users(
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    # 过滤掉 admin 角色（admin 用户对 UI 完全隐身）
    res = await db.execute(
        select(models.User)
        .join(models.Role, models.Role.id == models.User.role_id)
        .where(models.Role.code != "admin")
        .order_by(models.User.id)
    )
    return [_user_to_out(u) for u in res.scalars().all()]


@router.post("/users", response_model=schemas.UserOut)
async def create_user(
    data: schemas.UserCreate,
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(models.User).where(models.User.username == data.username))
    if res.scalar_one_or_none():
        raise HTTPException(400, "用户名已存在")
    # 校验 role 存在
    role_res = await db.execute(select(models.Role).where(models.Role.id == data.role_id))
    if not role_res.scalar_one_or_none():
        raise HTTPException(400, "角色不存在")
    u = models.User(
        username=data.username,
        full_name=data.full_name,
        email=data.email,
        password_hash=hash_password(data.password),
        password_must_change=True,  # 管理员建账号后强制首登改密
        role_id=data.role_id,
        is_active=data.is_active,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    # 重新查带关联（避免 lazy 异步问题）
    res = await db.execute(select(models.User).where(models.User.id == u.id))
    u = res.scalar_one()
    return _user_to_out(u)


@router.put("/users/{uid}", response_model=schemas.UserOut)
async def update_user(
    uid: int,
    data: schemas.UserUpdate,
    _: models.User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(models.User).where(models.User.id == uid))
    u = res.scalar_one_or_none()
    if not u:
        raise HTTPException(404, "用户不存在")
    if data.full_name is not None:
        u.full_name = data.full_name
    if data.email is not None:
        u.email = data.email
    if data.role_id is not None:
        u.role_id = data.role_id
    if data.is_active is not None:
        u.is_active = data.is_active
    if data.password:
        u.password_hash = hash_password(data.password)
        u.password_must_change = True
    await db.commit()
    res = await db.execute(select(models.User).where(models.User.id == uid))
    u = res.scalar_one()
    return _user_to_out(u)


@router.delete("/users/{uid}", response_model=schemas.Msg)
async def delete_user(
    uid: int,
    current: models.User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if uid == current.id:
        raise HTTPException(400, "不能删除自己")
    res = await db.execute(select(models.User).where(models.User.id == uid))
    u = res.scalar_one_or_none()
    if not u:
        raise HTTPException(404, "用户不存在")
    # 清掉 audit_logs / records.created_by/updated_by 等外键引用，避免 FK 约束
    from sqlalchemy import update as _upd
    await db.execute(
        _upd(models.AuditLog).where(models.AuditLog.user_id == uid).values(user_id=None)
    )
    await db.execute(
        _upd(models.Record).where(models.Record.created_by == uid).values(created_by=None)
    )
    await db.execute(
        _upd(models.Record).where(models.Record.updated_by == uid).values(updated_by=None)
    )
    await db.delete(u)
    await db.commit()
    return schemas.Msg(message="已删除")


# ---------- 审计 ----------
from sqlalchemy import desc as _desc


@router.get("/audit", response_model=List[schemas.AuditOut])
async def list_audit(
    limit: int = 200,
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    # admin 用户对所有人隐身：审计日志里也不显示 admin 的操作
    res = await db.execute(
        select(models.AuditLog)
        .where(models.AuditLog.username != "admin")
        .order_by(_desc(models.AuditLog.created_at)).limit(limit)
    )
    return [schemas.AuditOut.model_validate(r) for r in res.scalars().all()]
