"""认证：登录 / me / 改密 / 登出 / 🆕 企微静默登录"""
import logging
from datetime import datetime, timezone
from typing import Union
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from .. import models, schemas, gate
from ..auth import verify_password, hash_password, create_access_token
from ..config import settings
from ..deps import get_current_user
from ..utils import write_audit

log = logging.getLogger("auth")

router = APIRouter(prefix="/api/auth", tags=["认证"])


def _user_to_out(u: models.User) -> schemas.UserOut:
    from ..menus import ADMIN_MENU_DEFS
    roles = list(u.roles or [])
    if u.role and u.role.id not in {r.id for r in roles}:
        roles = [u.role] + roles
    roles = sorted(roles, key=lambda r: r.id)
    menus = list(u.menus or [])
    admin_keys = [m["key"] for m in ADMIN_MENU_DEFS]
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
        menus=menus,                            # 🆕 该账号配置的一级菜单 key
        # 派生值（兼容旧客户端/旧桌面端）：menus ∩ 管理组有效 key；不再读 grant_menus 列
        grant_menus=[k for k in admin_keys if k in set(menus)],
        created_at=u.created_at,
        last_login=u.last_login,
    )


def _client_ip(request: Request) -> str:
    """客户端真实 IP：优先 X-Real-IP（nginx 用 $remote_addr 覆写，外部无法伪造）；
    次取 X-Forwarded-For **末段**（nginx $proxy_add_x_forwarded_for 是把真实地址**追加**到链尾，
    取首段会被客户端伪造的 XFF 骗过）；兜底直连地址。"""
    rip = (request.headers.get("x-real-ip") or "").strip()
    if rip:
        return rip
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    return parts[-1] if parts else (request.client.host if request.client else "")


# 🆕「记住我」的令牌寿命。H5 给手机用，每次登录都要管理层报验证码，
#   8 小时一过就得再来一遍，体验太差。延长的只是**令牌有效期**，
#   密码一个字都不落客户端；用户点「退出」即清除。
REMEMBER_MINUTES = 30 * 24 * 60


async def _issue_token(db: AsyncSession, u: models.User, *, ip: str = "",
                       remember: bool = False) -> schemas.TokenOut:
    """登录成功签发 token + 写审计（login 免闸路径与 verify-gate 共用）。"""
    token = create_access_token(u.id, minutes=REMEMBER_MINUTES if remember else None)
    await write_audit(db, user=u, action="login", ip=ip or None)
    return schemas.TokenOut(access_token=token, user=_user_to_out(u))


@router.post("/login", response_model=Union[schemas.TokenOut, schemas.GateRequiredOut])
async def login(data: schemas.LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(models.User).where(models.User.username == data.username))
    u = res.scalar_one_or_none()
    if not u or not verify_password(data.password, u.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    if not u.is_active:
        raise HTTPException(403, "账号已停用")

    u.last_login = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(u)

    # ---- 🆕 外网登录两步闸门（免闸：admin 角色 / 桌面客户端 / 内网 IP / 开关关闭）----
    # 🆕 设备闸（device_gate，默认关）：打开后桌面端还要 X-PMS-Device 落在名单里才免闸，
    #    不在名单的机器照样走验证码。名单是管理层在「外网访问」页手工录入的。
    ip = _client_ip(request)
    is_desktop = request.headers.get("x-pms-client", "").startswith("desktop/")
    device_id = (request.headers.get("x-pms-device") or "").strip()
    if not u.has_role("admin"):
        cfg = await gate.get_gate_config(db)
        exempt = (gate.is_intranet(ip, cfg["cidrs"])
                  or gate.desktop_exempt(is_desktop, device_id,
                                         device_gate=cfg["device_gate"],
                                         device_ids=cfg["device_ids"]))
        if cfg["enabled"] and not exempt:
            try:
                pre_token = await gate.issue_code(db, u)
            except HTTPException as e:
                await write_audit(db, user=u, action="login_gate_fail",
                                  detail=str(e.detail), ip=ip or None)
                raise
            await write_audit(db, user=u, action="login_gate_issue", ip=ip or None)
            return schemas.GateRequiredOut(
                gate_required=True, pre_token=pre_token,
                message="已通知管理层，请联系管理层获取验证码")
    return await _issue_token(db, u, ip=ip, remember=bool(data.remember))


# ==================== 🆕 企业微信内嵌：静默登录 ====================

_WECOM_OAUTH_AUTHORIZE = "https://open.weixin.qq.com/connect/oauth2/authorize"


def wecom_authorize_url(redirect_uri: str, state: str = "pms") -> str:
    """拼企微网页授权地址（snsapi_base = 静默，用户无感，不弹授权页）。"""
    from urllib.parse import quote
    return (f"{_WECOM_OAUTH_AUTHORIZE}?appid={settings.wecom_corp_id}"
            f"&redirect_uri={quote(redirect_uri, safe='')}"
            f"&response_type=code&scope=snsapi_base&state={state}"
            f"#wechat_redirect")


@router.get("/wecom/entry")
async def wecom_entry():
    """企微工作台/消息卡片的入口：302 跳到企微授权，授权完带 code 回 /h5/。

    ⚠️ 为什么要服务端拼这个跳转，而不是前端自己拼：`corpid` 是配置项，
       烧进前端就意味着改一次要重发客户端；而且前端还得知道 https 根地址，
       它在企微内嵌、浏览器、APP 三种壳里各不相同。
    """
    from fastapi.responses import RedirectResponse
    base = (settings.public_base_url or "").rstrip("/")
    if not (settings.wecom_oauth_enabled and base and settings.wecom_corp_id):
        # 没开就退回普通登录页——**不能报错**：这个地址是配在企微工作台上的，
        # 报错等于整个应用打不开。
        return RedirectResponse(f"{base}/h5/#/login" if base else "/h5/#/login")
    return RedirectResponse(wecom_authorize_url(f"{base}/h5/"))


@router.post("/wecom", response_model=schemas.TokenOut)
async def wecom_login(data: schemas.WecomLoginIn, request: Request,
                      db: AsyncSession = Depends(get_db)):
    """拿企微授权 code 换 token。前端在企微内嵌里发现 URL 带 code 就调这里。

    流程：code → 企微 getuserinfo 拿 UserId → 按 `users.wxid` 找人 → 签 token。

    ⚠️ **不走外网验证码闸门**，这是有意的，理由必须写清楚：
       现在的闸门是「输密码 → 把 6 位码**推到企微** → 再输码」，
       也就是说**「能看到企微」本来就是现行的第二因子**。
       走到这个接口的人，已经在企业微信客户端里通过了企业的身份认证，
       用的是同一个信任锚点，只是省掉了打字。`gate.desktop_exempt`
       （桌面客户端免闸）是同一类先例。
       ⚠️ 前提是 **corpid/secret 只在服务端**、code 只能用一次且 5 分钟过期——
       这两条任何一条破了，这个豁免就不成立了。

    ⚠️ 找不到人时**只说「未绑定」，绝不自动建账号**：企微通讯录里
       会有访客、外部联系人，自动建号等于给系统开后门。
    """
    if not settings.wecom_oauth_enabled:
        raise HTTPException(403, "企微登录未开启")
    code = (data.code or "").strip()
    if not code:
        raise HTTPException(400, "缺少 code")
    from ..notify import wecom_userid_by_code
    try:
        wxid = await wecom_userid_by_code(code)
    except Exception as e:  # noqa: BLE001 —— 企微侧异常统一转 401，不外泄细节
        log.warning("企微 code 换 userid 失败: %s", e)
        raise HTTPException(401, "企业微信身份校验失败，请重新打开") from e
    if not wxid:
        raise HTTPException(401, "没拿到企业微信身份（可能是外部联系人）")

    res = await db.execute(select(models.User).where(models.User.wxid == wxid))
    u = res.scalar_one_or_none()
    if not u:
        raise HTTPException(403, "这个企业微信账号还没绑定系统用户，请找管理员在「企微绑定」里绑一下")
    if not u.is_active:
        raise HTTPException(403, "账号已停用")

    ip = _client_ip(request)
    u.last_login = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(u)
    await write_audit(db, user=u, action="login_wecom", ip=ip or None)
    # remember=True：企微里没人愿意一天登好几次，且这条路径本来就靠企微身份兜底
    return await _issue_token(db, u, ip=ip, remember=True)


@router.post("/login/verify-gate", response_model=schemas.TokenOut)
async def login_verify_gate(data: schemas.GateVerifyIn, request: Request,
                            db: AsyncSession = Depends(get_db)):
    """🆕 外网登录第二步：校验 6 位随机码，通过才发 token。"""
    res = await db.execute(select(models.User).where(models.User.username == data.username))
    u = res.scalar_one_or_none()
    if not u or not u.is_active:
        raise HTTPException(400, "验证码无效或已过期，请重新获取")
    ip = _client_ip(request)
    try:
        await gate.verify_code(db, u, data.pre_token, data.code)
    except HTTPException as e:
        await write_audit(db, user=u, action="login_gate_fail",
                          detail=str(e.detail), ip=ip or None)
        raise
    u.last_login = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(u)
    return await _issue_token(db, u, ip=ip, remember=bool(data.remember))


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
