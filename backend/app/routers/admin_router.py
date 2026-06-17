"""管理后台：用户 / 部门 / 角色"""
from typing import List, Optional
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
    # admin 角色对 UI 隐身（不出现在角色下拉、字段权限对话框等任何地方）
    # 🆕 A2: 标准件/外协采购员已并入「采购部 (buyer)」，对前端隐藏但保留数据(存量用户兼容)
    HIDDEN_ROLE_CODES = ("admin", "buyer_standard", "buyer_outsource")
    res = await db.execute(
        select(models.Role).where(models.Role.code.notin_(HIDDEN_ROLE_CODES)).order_by(models.Role.id)
    )
    return [schemas.RoleOut.model_validate(r) for r in res.scalars().all()]




# ---------- 用户 ----------
def _resolve_roles(u: models.User) -> list[models.Role]:
    """用户全部角色（锚点 role + user_roles 关联，去重，按 id 序）。"""
    roles = list(u.roles or [])
    if u.role and u.role.id not in {r.id for r in roles}:
        roles = [u.role] + roles
    return sorted(roles, key=lambda r: r.id)


def _user_to_out(u: models.User) -> schemas.UserOut:
    roles = _resolve_roles(u)
    return schemas.UserOut(
        id=u.id, username=u.username, full_name=u.full_name, email=u.email,
        role_id=u.role_id,
        role_code=u.role.code if u.role else None,
        role_name=u.role.name if u.role else None,
        role_ids=[r.id for r in roles],
        role_codes=[r.code for r in roles],
        role_names=[r.name for r in roles],
        is_active=u.is_active,
        password_must_change=u.password_must_change,
        wxid=u.wxid,
        created_at=u.created_at, last_login=u.last_login,
    )


# 不可经 API 分配的角色：admin 仅系统种子持有；两旧采购角色已并入 buyer
_UNASSIGNABLE_CODES = {"admin", "buyer_standard", "buyer_outsource"}


def _guard_assignable(actor: models.User, codes: set[str]) -> None:
    """越权防护：拦截把高权/系统角色塞给用户。
    - admin / 旧采购角色：任何人都不可经 API 分配
    - manager（管理层）：仅 admin 可分配（防止 manager 自助提权造 manager）"""
    bad = codes & _UNASSIGNABLE_CODES
    if bad:
        raise HTTPException(403, f"不可分配角色：{'、'.join(sorted(bad))}（系统/已停用角色）")
    if "manager" in codes and not actor.has_role("admin"):
        raise HTTPException(403, "仅超级管理员可分配「管理层」角色")


async def _set_user_roles(db: AsyncSession, u: models.User, role_ids: list[int],
                          actor: models.User) -> set[str]:
    """整体设置用户角色集（去重保序）：校验存在 + 越权防护 → 设锚点 role_id → 用 Core
    重写 user_roles（roles 关系是 viewonly，不参与 flush）。要求 u 已有 id。返回 code 集合。"""
    from sqlalchemy import delete as _del
    ordered = list(dict.fromkeys(role_ids))
    rres = await db.execute(select(models.Role).where(models.Role.id.in_(ordered)))
    found = {r.id: r for r in rres.scalars().all()}
    if any(rid not in found for rid in ordered):
        raise HTTPException(400, "角色不存在")
    codes = {found[rid].code for rid in ordered}
    _guard_assignable(actor, codes)
    u.role_id = ordered[0]
    await db.execute(_del(models.UserRole).where(models.UserRole.user_id == u.id))
    for rid in ordered:
        db.add(models.UserRole(user_id=u.id, role_id=rid))
    return codes


def _effective_role_ids(data) -> Optional[list[int]]:
    """取请求里的角色集：优先 role_ids，回退单 role_id；都没有则 None。"""
    if data.role_ids:
        return list(dict.fromkeys(data.role_ids))  # 去重保序
    if getattr(data, "role_id", None) is not None:
        return [data.role_id]
    return None


@router.get("/users", response_model=List[schemas.UserOut])
async def list_users(
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    # 过滤掉拥有 admin 角色的用户（admin 对 UI 完全隐身；多角色：任一角色是 admin 即隐身）
    res = await db.execute(select(models.User).order_by(models.User.id))
    return [_user_to_out(u) for u in res.scalars().all() if not u.has_role("admin")]


@router.post("/users", response_model=schemas.UserOut)
async def create_user(
    data: schemas.UserCreate,
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(models.User).where(models.User.username == data.username))
    if res.scalar_one_or_none():
        raise HTTPException(400, "用户名已存在")
    role_ids = _effective_role_ids(data)
    if not role_ids:
        raise HTTPException(400, "请至少选择一个角色")
    u = models.User(
        username=data.username,
        full_name=data.full_name,
        email=data.email,
        password_hash=hash_password(data.password),
        password_must_change=True,  # 管理员建账号后强制首登改密
        role_id=role_ids[0],
        is_active=data.is_active,
    )
    db.add(u)
    await db.flush()  # 拿到 u.id（写 user_roles + 项目成员回填）
    codes = await _set_user_roles(db, u, role_ids, current)  # 校验 + 越权防护 + 设锚点 + 写 user_roles
    # 新用户默认加入所有「存量活跃项目」为 edit 成员
    # （admin/manager 在 deps 层自动拥有全部项目权限，无需塞 member）
    if u.is_active and not (codes & {"admin", "manager"}):
        from .projects_router import _add_user_to_all_active_projects
        await _add_user_to_all_active_projects(db, u.id, "edit")
    await db.commit()
    # expire_on_commit=False：显式刷新 锚点 role + 多角色 roles，避免返回陈旧集合
    await db.refresh(u, attribute_names=["role", "roles"])
    return _user_to_out(u)


@router.put("/users/{uid}", response_model=schemas.UserOut)
async def update_user(
    uid: int,
    data: schemas.UserUpdate,
    current: models.User = Depends(require_admin),
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
    new_role_ids = _effective_role_ids(data)
    if new_role_ids is not None:
        codes = await _set_user_roles(db, u, new_role_ids, current)  # 整体替换 + 越权防护
        # 角色变更后：若用户变成 active 的非管理层，确保其为所有活跃项目成员
        # （修复：admin/manager 降级为普通角色后无 ProjectMember → 一览/项目看不到任何项目）
        if u.is_active and not (codes & {"admin", "manager"}):
            from .projects_router import _add_user_to_all_active_projects
            await _add_user_to_all_active_projects(db, u.id, "edit")
    if data.is_active is not None:
        u.is_active = data.is_active
    if data.password:
        u.password_hash = hash_password(data.password)
        u.password_must_change = True
    await db.commit()
    # expire_on_commit=False：显式刷新 锚点 role + 多角色 roles，避免返回陈旧集合
    await db.refresh(u, attribute_names=["role", "roles"])
    return _user_to_out(u)


@router.put("/users/{uid}/wxid", response_model=schemas.Msg)
async def bind_wxid(
    uid: int,
    data: schemas.WxidIn,
    _: models.User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """🆕 v3：绑定/更新用户企业微信 userid（F1 手动绑定口径；空串=解绑）。"""
    res = await db.execute(select(models.User).where(models.User.id == uid))
    u = res.scalar_one_or_none()
    if not u:
        raise HTTPException(404, "用户不存在")
    u.wxid = data.wxid.strip() or None
    await db.commit()
    return schemas.Msg(message="已绑定" if u.wxid else "已解绑")


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
    # 清理全部指向该用户的外键引用，避免 FK 约束导致删除失败（含采购/物流/售后/反馈等业务表）。
    # 可空外键 → 置 None（保留业务行，仅解除与该用户的关联）；非空外键 → 删除依赖行。
    from sqlalchemy import update as _upd, delete as _del
    # 非空外键：必须删行
    await db.execute(_del(models.Message).where(models.Message.to_user_id == uid))           # 站内消息
    await db.execute(_del(models.ExportRequest).where(models.ExportRequest.user_id == uid))  # 导出申请
    await db.execute(_del(models.ProjectMember).where(models.ProjectMember.user_id == uid))  # 项目成员(本就 CASCADE,显式删兼容 SQLite 关闭 FK)
    await db.execute(_del(models.UserRole).where(models.UserRole.user_id == uid))             # 多角色关联(roles 为 viewonly 不自动级联，显式删)
    # 可空外键：置 None
    nullables = [
        (models.AuditLog, "user_id"),
        (models.Record, "created_by"), (models.Record, "updated_by"),
        (models.Project, "manager_id"),
        (models.DeptOrder, "worker_id"), (models.DeptOrder, "created_by"), (models.DeptOrder, "notify_user_id"),
        (models.SalesLedger, "sales_uid"),
        (models.Shipment, "shipped_by"),
        (models.Attachment, "uploaded_by"),
        (models.WhTxn, "operator_id"),
        (models.AfterSales, "created_by"), (models.AfterSales, "appr_by"),
        (models.Feedback, "created_by"), (models.Feedback, "designer_uid"), (models.Feedback, "appr_by"),
        (models.ExportRequest, "appr_by"),
        (models.UserFeedback, "user_id"),
    ]
    for model, col in nullables:
        await db.execute(_upd(model).where(getattr(model, col) == uid).values(**{col: None}))
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
