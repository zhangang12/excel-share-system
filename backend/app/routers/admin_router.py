"""管理后台：用户 / 部门 / 角色"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from .. import models, schemas
from ..auth import hash_password
from ..deps import require_admin_or_manager, get_current_user
from ..utils import write_audit

router = APIRouter(prefix="/api/admin", tags=["管理后台"])


async def _require_wxbind_ops(current: models.User = Depends(get_current_user)) -> models.User:
    """🆕 2026-07-21 企微绑定页权限：admin/manager + 人事(hr)。
    人事需要给全体员工绑/解绑企微 userid；用户列表接口对人事只读开放(供绑定页使用)，
    其余用户管理接口(建/改/删/调角色)仍仅 admin/manager。"""
    if not current.has_role("admin", "manager", "hr"):
        raise HTTPException(403, "仅管理员/管理层/人事可操作")
    return current


# ---------- 角色（只读） ----------
@router.get("/roles", response_model=List[schemas.RoleOut])
async def list_roles(
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    # admin 角色对 UI 隐身（不出现在角色下拉、字段权限对话框等任何地方）
    # 🆕 2026-06-19: 重新启用「标准件采购员/外协机加工采购员」两个子角色——采购部项目列表按其区分两套列
    #    (外协采购员→外协加工/不锈钢原料/CAD激光图纸；标准件采购员→电工采购单/标准件清单/外购附图)。
    HIDDEN_ROLE_CODES = ("admin",)
    res = await db.execute(
        select(models.Role).where(models.Role.code.notin_(HIDDEN_ROLE_CODES)).order_by(models.Role.id)
    )
    return [schemas.RoleOut.model_validate(r) for r in res.scalars().all()]


# ---------- 🆕 #7 可授权的二级菜单(tab) 注册表 ----------
@router.get("/tab-registry")
async def get_tab_registry(_: models.User = Depends(require_admin_or_manager)):
    """按账号分配二级菜单权限用：返回可管控的 tab 清单(带全局唯一 key)。"""
    from ..menus import tab_registry
    return tab_registry()


# ---------- 🆕 一级菜单定义（用户管理「菜单权限」弹窗用，顺序即侧边栏顺序） ----------
@router.get("/menu-defs")
async def get_menu_defs(_: models.User = Depends(require_admin_or_manager)):
    """返回全量一级菜单定义：business=业务区(MENU_DEFS)、admin=管理组(ADMIN_MENU_DEFS)。"""
    from ..menus import MENU_DEFS, ADMIN_MENU_DEFS
    return {"business": MENU_DEFS, "admin": ADMIN_MENU_DEFS}




# ---------- 用户 ----------
def _resolve_roles(u: models.User) -> list[models.Role]:
    """用户全部角色（锚点 role + user_roles 关联，去重，按 id 序）。"""
    roles = list(u.roles or [])
    if u.role and u.role.id not in {r.id for r in roles}:
        roles = [u.role] + roles
    return sorted(roles, key=lambda r: r.id)


def _user_to_out(u: models.User) -> schemas.UserOut:
    from ..menus import ADMIN_MENU_DEFS
    roles = _resolve_roles(u)
    menus = list(u.menus or [])
    admin_keys = [m["key"] for m in ADMIN_MENU_DEFS]
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
        hidden_tabs=list(u.hidden_tabs or []),
        menus=menus,
        # 派生值（兼容旧客户端/旧桌面端）：menus ∩ 管理组有效 key；不再读 grant_menus 列
        grant_menus=[k for k in admin_keys if k in set(menus)],
        created_at=u.created_at, last_login=u.last_login,
    )


# 不可经 API 分配的角色：admin 仅系统种子持有（采购两子角色 2026-06-19 重新启用，可分配）
_UNASSIGNABLE_CODES = {"admin"}


def _guard_assignable(actor: models.User, codes: set[str]) -> None:
    """越权防护：拦截把系统级/已停用角色塞给用户。
    - admin / 旧采购角色(buyer_standard/buyer_outsource)：任何人都不可经 API 分配（admin 仅系统种子持有）
    - manager（管理层）：管理层自身亦可分配（用户口径 2026-06-17：manager 可为自己/他人调整角色）"""
    bad = codes & _UNASSIGNABLE_CODES
    if bad:
        raise HTTPException(403, f"不可分配角色：{'、'.join(sorted(bad))}（系统/已停用角色）")


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
    _: models.User = Depends(_require_wxbind_ops),  # 🆕 人事(hr)只读用户列表→企微绑定页
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
    # 🆕 一级菜单按账号配置：建号时用所选角色（含锚点）的 ROLE_DEFAULT_MENUS 并集
    # + messages + oa 预填（一次性默认；之后由「菜单权限」按账号调整，改角色不再联动）。
    # admin/manager 天然全可见，无需预填。
    if not (codes & {"admin", "manager"}):
        from ..menus import default_menus_for_roles
        eff = set(codes) | ({"finance"} if "finance_lead" in codes else set())  # 与 role_codes 隐含口径一致
        u.menus = default_menus_for_roles(eff)
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
    current: models.User = Depends(require_admin_or_manager),  # 🆕 管理层亦可编辑用户/调整角色(自己+他人)
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(models.User).where(models.User.id == uid))
    u = res.scalar_one_or_none()
    if not u:
        raise HTTPException(404, "用户不存在")
    # 非超级管理员不可篡改超级管理员账号(admin 系统级保护)
    if u.has_role("admin") and not current.has_role("admin"):
        raise HTTPException(403, "不可修改超级管理员账号")
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
    if data.hidden_tabs is not None:   # 🆕 #7 设置该账号隐藏的二级菜单tab
        u.hidden_tabs = list(dict.fromkeys(data.hidden_tabs))
    if data.password:
        u.password_hash = hash_password(data.password)
        u.password_must_change = True
    await db.commit()
    # expire_on_commit=False：显式刷新 锚点 role + 多角色 roles，避免返回陈旧集合
    await db.refresh(u, attribute_names=["role", "roles"])
    return _user_to_out(u)


@router.put("/users/{uid}/menus", response_model=schemas.UserOut)
async def set_user_menus(
    uid: int,
    data: schemas.SetMenusIn,
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """🆕 一级菜单按账号配置（角色菜单矩阵已废除）：整体替换该账号的一级菜单 key 清单。

    key 必须 ⊆ MENU_DEFS ∪ ADMIN_MENU_DEFS，非法 400；去重后按规范顺序落库。
    仅对非 admin/manager 账号有意义（管理层天然全可见）；写审计。"""
    from ..menus import MENU_DEFS, ADMIN_MENU_DEFS, canonical_menu_order
    res = await db.execute(select(models.User).where(models.User.id == uid))
    u = res.scalar_one_or_none()
    if not u:
        raise HTTPException(404, "用户不存在")
    if u.has_role("admin") and not current.has_role("admin"):
        raise HTTPException(403, "不可修改超级管理员账号")
    valid_keys = {m["key"] for m in (MENU_DEFS + ADMIN_MENU_DEFS)}
    bad = set(data.menus) - valid_keys
    if bad:
        raise HTTPException(400, f"非法菜单 key：{'、'.join(sorted(bad))}")
    u.menus = canonical_menu_order(dict.fromkeys(data.menus))
    await write_audit(db, user=current, action="set_menus",
                      target_type="user", target_id=u.id,
                      detail=f"设置一级菜单: {u.menus}")
    await db.commit()
    await db.refresh(u, attribute_names=["role", "roles"])
    return _user_to_out(u)


@router.put("/users/{uid}/grant-menus", response_model=schemas.UserOut)
async def grant_menus(
    uid: int,
    data: schemas.GrantMenusIn,
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """兼容包装（桌面端 1.0.2 在用）：按账号开通/收回管理组菜单。

    语义 = 仅对 ADMIN_MENU_DEFS 的 key：入参含有的加入 User.menus、不含的从 User.menus
    移除；其余（业务）key 不动。仅对非 admin/manager 账号有意义（管理层天然全可见）；写审计。"""
    from ..menus import ADMIN_MENU_DEFS, canonical_menu_order, user_menu_keys
    res = await db.execute(select(models.User).where(models.User.id == uid))
    u = res.scalar_one_or_none()
    if not u:
        raise HTTPException(404, "用户不存在")
    if u.has_role("admin") and not current.has_role("admin"):
        raise HTTPException(403, "不可修改超级管理员账号")
    valid_keys = {m["key"] for m in ADMIN_MENU_DEFS}
    bad = set(data.grant_menus) - valid_keys
    if bad:
        raise HTTPException(400, f"不可开通的管理组菜单：{'、'.join(sorted(bad))}")
    # menus 为 NULL（未配置）时先按当前有效可见值落地，再对管理组 key 做增删
    base = list(u.menus) if u.menus is not None else user_menu_keys(u)
    kept = set(base) - valid_keys
    u.menus = canonical_menu_order(kept | set(data.grant_menus))
    await write_audit(db, user=current, action="grant_menus",
                      target_type="user", target_id=u.id,
                      detail=f"开通管理组菜单: {[k for k in u.menus if k in valid_keys]}")
    await db.commit()
    await db.refresh(u, attribute_names=["role", "roles"])
    return _user_to_out(u)


@router.put("/users/{uid}/wxid", response_model=schemas.Msg)
async def bind_wxid(
    uid: int,
    data: schemas.WxidIn,
    current: models.User = Depends(_require_wxbind_ops),  # 🆕 人事(hr)亦可绑/解绑企微 userid
    db: AsyncSession = Depends(get_db),
):
    """🆕 v3：绑定/更新用户企业微信 userid（F1 手动绑定口径；空串=解绑）。"""
    res = await db.execute(select(models.User).where(models.User.id == uid))
    u = res.scalar_one_or_none()
    if not u:
        raise HTTPException(404, "用户不存在")
    if u.has_role("admin") and not current.has_role("admin"):
        raise HTTPException(403, "不可修改超级管理员账号")
    u.wxid = data.wxid.strip() or None
    await db.commit()
    return schemas.Msg(message="已绑定" if u.wxid else "已解绑")


@router.post("/wecom-test", response_model=schemas.Msg)
async def wecom_test(
    uid: Optional[int] = None,
    current: models.User = Depends(_require_wxbind_ops),  # 🆕 人事(hr)亦可做推送自检
    db: AsyncSession = Depends(get_db),
):
    """🆕 企微推送自检：给指定用户(uid 省略=当前登录人，需已绑 userid)发一条测试消息，
    失败原因直接抛出。admin/管理层/人事均可用。"""
    from ..config import settings
    from ..notify import _send_wecom
    from datetime import datetime
    if not (settings.wecom_corp_id and settings.wecom_secret and settings.wecom_agent_id):
        raise HTTPException(400, "企微凭证未配置：请在 .env.prod 填 WECOM_CORP_ID/WECOM_AGENT_ID/WECOM_SECRET 后重启")
    target = current
    if uid is not None and uid != current.id:
        res = await db.execute(select(models.User).where(models.User.id == uid))
        target = res.scalar_one_or_none()
        if not target:
            raise HTTPException(404, "用户不存在")
    who = target.full_name or target.username
    if not target.wxid:
        raise HTTPException(400, f"{who} 还没绑定企业微信 userid")
    try:
        await _send_wecom(db, [target.id], f"【测试推送】企业微信推送已打通 ✅ {datetime.now():%Y-%m-%d %H:%M:%S}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"发送失败：{e}")
    return schemas.Msg(message=f"已向 {who} 发送测试消息，请查收企业微信")


@router.delete("/users/{uid}", response_model=schemas.Msg)
async def delete_user(
    uid: int,
    current: models.User = Depends(require_admin_or_manager),  # 🆕 管理层=管理员权限(可删用户)
    db: AsyncSession = Depends(get_db),
):
    if uid == current.id:
        raise HTTPException(400, "不能删除自己")
    res = await db.execute(select(models.User).where(models.User.id == uid))
    u = res.scalar_one_or_none()
    if not u:
        raise HTTPException(404, "用户不存在")
    # 非超级管理员不可删除超级管理员账号(admin 系统级保护)
    if u.has_role("admin") and not current.has_role("admin"):
        raise HTTPException(403, "不可删除超级管理员账号")
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
