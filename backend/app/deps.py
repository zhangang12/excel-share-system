"""依赖项：当前用户 / 权限检查"""
from fastapi import Depends, HTTPException, status, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .database import get_db
from . import models
from .auth import decode_token


async def get_current_user(
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
) -> models.User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    token = authorization.split(" ", 1)[1].strip()
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已过期")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效凭证")
    res = await db.execute(select(models.User).where(models.User.id == int(user_id)))
    user = res.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号已禁用")
    return user


async def require_admin(current: models.User = Depends(get_current_user)) -> models.User:
    # admin 与 manager（管理层）拥有同等的全部权限
    if not current.role or current.role.code not in ("admin", "manager"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权操作")
    return current

async def require_admin_or_manager(current: models.User = Depends(get_current_user)) -> models.User:
    """admin 或 管理层都允许"""
    if not current.role or current.role.code not in ("admin", "manager"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅管理员/管理层可操作")
    return current



async def require_not_viewer(current: models.User = Depends(get_current_user)) -> models.User:
    if current.role and current.role.code == "viewer":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只读账号不可操作")
    return current


def require_roles(*codes: str):
    """🆕 依赖工厂：仅允许指定角色（admin/manager 始终放行）。
    用法：Depends(require_roles("sales", "sales_lead"))"""
    allowed = set(codes) | {"admin", "manager"}

    async def _dep(current: models.User = Depends(get_current_user)) -> models.User:
        if not current.role or current.role.code not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "无权操作")
        return current

    return _dep


async def require_can_view_detail(
    current: models.User = Depends(get_current_user),
) -> models.User:
    """🆕 项目详单闸门：销售/电工/装配/售后角色无详单权限（2026-06-12 收紧口径）。"""
    from .menus import user_can_view_detail
    if not user_can_view_detail(current):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "你没有项目详单权限")
    return current


def ensure_can_export(user: models.User) -> None:
    """🆕 M16 导出闸门：开关关闭时 no-op（老导出行为不变）；
    开关开启时仅管理层或已获导出权(can_export)放行，否则 403 引导申请。"""
    from .config import settings
    if not settings.export_approval_enabled:
        return
    if user.role and user.role.code in ("admin", "manager"):
        return
    if getattr(user, "can_export", False):
        return
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "导出需审批：请在「导出审批」申请，管理层通过后即可导出",
    )


async def user_can_view_project(
    db: AsyncSession, user: models.User, project: models.Project
) -> bool:
    """项目详情/字段/导出的每项目门禁（get_project、datasheets 读、excel 导出共用）。

    注意：项目目录「列表显示哪些项目」的行级可见性在 list_projects 单独处理
    （设计/电工/装配只列被派单的、销售只列自己下单的），不在此函数——
    因为本函数还把守详情/字段/导出，设计师有详单权时应能读任意项目（#91 口径）。
    """
    if not user.role:
        return False
    # admin / manager / 各部门负责人(_lead)：全部项目可见（不依赖成员资格——修复新建主管看不到老项目）
    if user.role.code in ("admin", "manager") or user.role.code.endswith("_lead"):
        return True
    # 其余角色：项目成员才可见（成员由建项目自动添加 + 启动回填，普通员工默认是全部项目成员）
    res = await db.execute(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == user.id,
        )
    )
    return res.scalar_one_or_none() is not None


async def user_can_edit_project(
    db: AsyncSession, user: models.User, project: models.Project
) -> bool:
    if not user.role:
        return False
    # admin / manager：全可编辑
    if user.role.code in ("admin", "manager"):
        return True
    res = await db.execute(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == user.id,
        )
    )
    m = res.scalar_one_or_none()
    return m is not None and m.permission == "edit"
