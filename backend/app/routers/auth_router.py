"""认证：登录 / me / 改密 / 登出"""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from .. import models, schemas
from ..auth import verify_password, hash_password, create_access_token
from ..deps import get_current_user
from ..utils import write_audit

router = APIRouter(prefix="/api/auth", tags=["认证"])


def _user_to_out(u: models.User) -> schemas.UserOut:
    roles = list(u.roles or [])
    if u.role and u.role.id not in {r.id for r in roles}:
        roles = [u.role] + roles
    roles = sorted(roles, key=lambda r: r.id)
    return schemas.UserOut(
        id=u.id,
        username=u.username,
        full_name=u.full_name,
        email=u.email,
        role_id=u.role_id,
        role_code=u.role.code if u.role else None,
        role_name=u.role.name if u.role else None,
        role_ids=[r.id for r in roles],
        role_codes=sorted(u.role_codes),   # 🆕 用 property(含 finance_lead⊇finance 隐含)，前端 hasRole 才一致
        role_names=[r.name for r in roles],
        is_active=u.is_active,
        password_must_change=u.password_must_change,
        wxid=u.wxid,
        hidden_tabs=list(u.hidden_tabs or []),   # 🆕 #7 前端据此隐藏二级菜单tab
        grant_menus=list(u.grant_menus or []),   # 🆕 反馈#268 额外开通的管理组菜单
        created_at=u.created_at,
        last_login=u.last_login,
    )


@router.post("/login", response_model=schemas.TokenOut)
async def login(data: schemas.LoginIn, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(models.User).where(models.User.username == data.username))
    u = res.scalar_one_or_none()
    if not u or not verify_password(data.password, u.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    if not u.is_active:
        raise HTTPException(403, "账号已停用")

    u.last_login = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(u)

    token = create_access_token(u.id)
    await write_audit(db, user=u, action="login")
    return schemas.TokenOut(access_token=token, user=_user_to_out(u))


@router.get("/me", response_model=schemas.UserOut)
async def me(current: models.User = Depends(get_current_user)):
    return _user_to_out(current)


@router.get("/menus", response_model=schemas.MenusOut)
async def my_menus(current: models.User = Depends(get_current_user)):
    """🆕 v3：当前用户可见菜单（前端侧边栏渲染的唯一权威）+ 详单可点性。"""
    from ..menus import user_menu_keys, user_can_view_detail, MENU_DEFS, ADMIN_MENU_DEFS
    keys = user_menu_keys(current)
    labels = {m["key"]: m["label"] for m in (MENU_DEFS + ADMIN_MENU_DEFS)}
    return schemas.MenusOut(
        menus=[schemas.MenuItem(key=k, label=labels.get(k, k)) for k in keys],
        can_view_detail=user_can_view_detail(current),
    )


@router.post("/change-password", response_model=schemas.Msg)
async def change_password(
    data: schemas.ChangePasswordIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(data.old_password, current.password_hash):
        raise HTTPException(400, "原密码不正确")
    if data.old_password == data.new_password:
        raise HTTPException(400, "新密码不能与原密码相同")
    current.password_hash = hash_password(data.new_password)
    current.password_must_change = False
    await db.commit()
    await write_audit(db, user=current, action="change_password")
    return schemas.Msg(message="密码已修改")


@router.post("/logout", response_model=schemas.Msg)
async def logout(_: models.User = Depends(get_current_user)):
    # JWT 无状态，靠客户端清 token 即可
    return schemas.Msg(message="已登出")
