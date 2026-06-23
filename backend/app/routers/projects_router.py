"""项目管理"""
import json
from datetime import date
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, func, delete as sql_delete, update as sql_update

from ..database import get_db
from .. import models, schemas
from ..sheet_templates import SHEET_TEMPLATES, is_date_field, normalize_date_str
from ..utils import write_audit


def _template_field_type(name: str) -> str:
    """按字段名推断模板字段类型（用于新建项目时预置空数据表）。"""
    if name == '进度':
        return 'select'
    if name.endswith('日期'):
        return 'date'
    if name == '数量':
        return 'number'
    return 'text'


async def _create_sheet_with_fields(
    db: AsyncSession, project_id: int, name: str, field_names: list[str], sort_order: int,
) -> models.Datasheet:
    """建一张数据表 + 按字段名建表头字段（空表）。调用方负责 commit。"""
    d = models.Datasheet(project_id=project_id, name=name, sort_order=sort_order, header_lines=None)
    db.add(d)
    await db.flush()
    for f_idx, fname in enumerate(field_names):
        config = json.dumps({'options': ['完成', '进行中']}, ensure_ascii=False) if fname == '进度' else None
        db.add(models.Field(
            datasheet_id=d.id, name=fname, type=_template_field_type(fname),
            sort_order=f_idx, config=config,
        ))
    return d


async def create_default_template_sheets(db: AsyncSession, project_id: int) -> int:
    """为新建项目预置 4 个固定数据表 + 🆕第 5 张「电工采购单」（§十六）。
    每张表按模板建好字段表头，但不插入数据行（空表）。返回创建数量。调用方负责 commit。"""
    from ..sheet_templates import ELEC_PO_SHEET_NAME, ELEC_PO_COLUMNS
    created = 0
    for s_idx, (sheet_name, field_names) in enumerate(SHEET_TEMPLATES.items()):
        await _create_sheet_with_fields(db, project_id, sheet_name, list(field_names), s_idx)
        created += 1
    # 第 5 张：电工采购单（不在 SHEET_TEMPLATES，单独建）
    await _create_sheet_with_fields(
        db, project_id, ELEC_PO_SHEET_NAME, ELEC_PO_COLUMNS, len(SHEET_TEMPLATES))
    created += 1
    return created
from ..deps import (
    get_current_user, require_admin, require_not_viewer,
    user_can_view_project, user_can_edit_project,
)


async def _purge_project_derived_data(db: AsyncSession, project_ids: list[int]) -> dict:
    """显式删除若干项目挂着的所有派生数据：
    project_members / records / field_permissions / fields / datasheets。

    显式删整条链而不是依赖外键 CASCADE：因为
    - SQLite 默认不开外键约束
    - ORM-aware delete 在某些场景下行为不一致
    显式删更可控，且更易在审计日志里说明"清理了多少东西"。

    project 本身不动（保留 is_deleted=true 的墓碑记录）。
    """
    if not project_ids:
        return {"datasheets": 0, "fields": 0, "field_perms": 0, "records": 0, "members": 0}

    # 找出所有相关 datasheet_id / field_id
    res = await db.execute(
        select(models.Datasheet.id).where(models.Datasheet.project_id.in_(project_ids))
    )
    ds_ids = [r[0] for r in res.all()]
    field_ids: list[int] = []
    if ds_ids:
        res = await db.execute(
            select(models.Field.id).where(models.Field.datasheet_id.in_(ds_ids))
        )
        field_ids = [r[0] for r in res.all()]

    counts = {
        "datasheets": len(ds_ids),
        "fields": len(field_ids),
        "field_perms": 0,
        "records": 0,
        "members": 0,
    }

    # 1. field_permissions
    if field_ids:
        res = await db.execute(
            select(func.count(models.FieldPermission.id)).where(
                models.FieldPermission.field_id.in_(field_ids)
            )
        )
        counts["field_perms"] = res.scalar() or 0
        await db.execute(sql_delete(models.FieldPermission).where(
            models.FieldPermission.field_id.in_(field_ids)
        ))

    # 2. records
    if ds_ids:
        res = await db.execute(
            select(func.count(models.Record.id)).where(
                models.Record.datasheet_id.in_(ds_ids)
            )
        )
        counts["records"] = res.scalar() or 0
        await db.execute(sql_delete(models.Record).where(
            models.Record.datasheet_id.in_(ds_ids)
        ))

    # 3. fields
    if field_ids:
        await db.execute(sql_delete(models.Field).where(
            models.Field.id.in_(field_ids)
        ))

    # 4. datasheets
    if ds_ids:
        await db.execute(sql_delete(models.Datasheet).where(
            models.Datasheet.id.in_(ds_ids)
        ))

    # 5. project_members
    res = await db.execute(
        select(func.count(models.ProjectMember.id)).where(
            models.ProjectMember.project_id.in_(project_ids)
        )
    )
    counts["members"] = res.scalar() or 0
    await db.execute(sql_delete(models.ProjectMember).where(
        models.ProjectMember.project_id.in_(project_ids)
    ))

    return counts


async def soft_delete_project(db: AsyncSession, p: models.Project, *,
                              void_dept_orders: bool = True) -> dict:
    """软删项目并清理派生数据（详单/字段/行/权限/成员），可选把各部门任务单置作废。

    只做删除机制本身，不做业务前置校验、不 commit（调用方负责）。
    供管理员删除项目、销售订单作废审批通过共用，避免逻辑分叉。
    返回清理计数（含 dept_orders_voided）。
    """
    counts = await _purge_project_derived_data(db, [p.id])
    voided = 0
    if void_dept_orders:
        res = await db.execute(
            sql_update(models.DeptOrder)
            .where(models.DeptOrder.project_id == p.id,
                   models.DeptOrder.status.notin_(("done", "voided")))
            .values(status="voided")
        )
        voided = res.rowcount or 0
    p.is_deleted = True
    if not p.code.startswith("_deleted_"):
        p.code = f"_deleted_{p.id}_{p.code}"[:64]
    counts["dept_orders_voided"] = voided
    return counts


async def _add_all_active_users_as_members(
    db: AsyncSession, project_id: int, permission: str = "edit"
) -> int:
    """把所有 active 的非 admin/manager 用户加为某项目的成员。
    已经是成员的跳过。返回新增条数。

    admin/manager 不加：他们在 deps 层自动获得所有项目的访问权，加进
    member 列表只会让"成员"视图看起来有冗余。
    """
    # 取所有可加成员
    res = await db.execute(
        select(models.User).join(models.Role).where(
            models.User.is_active == True,
            models.Role.code.notin_(("admin", "manager")),
        )
    )
    candidates = res.scalars().all()
    if not candidates:
        return 0
    # 已存在的成员
    res = await db.execute(
        select(models.ProjectMember.user_id).where(
            models.ProjectMember.project_id == project_id
        )
    )
    existing_ids = {r[0] for r in res.all()}
    added = 0
    for u in candidates:
        if u.id in existing_ids:
            continue
        db.add(models.ProjectMember(
            project_id=project_id, user_id=u.id, permission=permission
        ))
        added += 1
    return added


async def _add_user_to_all_active_projects(
    db: AsyncSession, user_id: int, permission: str = "edit"
) -> int:
    """把某个用户加为所有「活跃项目」的成员（已是成员的跳过）。
    新建用户时调用，保证新用户能看到/编辑存量项目。返回新增条数。
    调用方负责 commit。"""
    res = await db.execute(
        select(models.Project.id).where(models.Project.is_deleted == False)
    )
    pids = [r[0] for r in res.all()]
    if not pids:
        return 0
    res = await db.execute(
        select(models.ProjectMember.project_id).where(
            models.ProjectMember.user_id == user_id
        )
    )
    existing_pids = {r[0] for r in res.all()}
    added = 0
    for pid in pids:
        if pid in existing_pids:
            continue
        db.add(models.ProjectMember(
            project_id=pid, user_id=user_id, permission=permission
        ))
        added += 1
    return added

router = APIRouter(prefix="/api/projects", tags=["项目"])


# 项目头元数据在 extra 中的 key 前缀：
#   __h__  → 项目详情的项目头表
#   __o__  → 项目一览（独立存储，与项目详情完全解耦）
HEADER_KEY_PREFIX = "__h__"
OVERVIEW_KEY_PREFIX = "__o__"


def _extract_header_meta(extra: Optional[dict]) -> dict:
    """从 project.extra 提取项目头数据（去掉 __h__ 前缀）。"""
    if not extra:
        return {}
    out = {}
    for k, v in extra.items():
        if isinstance(k, str) and k.startswith(HEADER_KEY_PREFIX):
            out[k[len(HEADER_KEY_PREFIX):]] = v
    return out


def _extract_overview_meta(extra: Optional[dict]) -> dict:
    """从 project.extra 提取一览字段数据（去掉 __o__ 前缀）。
    项目详情头表「镜像一览」时读这里，与一览同源。"""
    if not extra:
        return {}
    out = {}
    for k, v in extra.items():
        if isinstance(k, str) and k.startswith(OVERVIEW_KEY_PREFIX):
            out[k[len(OVERVIEW_KEY_PREFIX):]] = v
    return out


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
        header_meta=_extract_header_meta(p.extra),
        overview_meta=_extract_overview_meta(p.extra),
    )


@router.get("", response_model=List[schemas.ProjectOut])
async def list_projects(
    q: Optional[str] = Query(None),
    status: Optional[str] = None,
    year: Optional[str] = Query(None),
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    delivery_pids = select(models.SalesLedger.project_id).where(
        models.SalesLedger.order_type == "调货订单"
    )
    query = select(models.Project).where(
        models.Project.is_deleted == False,
        models.Project.id.not_in(delivery_pids),
    )
    if year:
        query = query.where(models.Project.code.like(f"{year}-%"))
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
    query = query.order_by(models.Project.code.desc())
    res = await db.execute(query)
    items = list(res.scalars().all())
    # 🆕 下单审批流：待审批/草稿(order_state pending/draft)的项目在通过前仅「销售本人 + 销售主管 + 管理层」可见，
    # 其余角色(含各部门负责人——其经 user_can_view_project 本会看全部)在审批通过前看不到该项目。
    if not current.has_role("admin", "manager", "sales_lead"):
        sub = await db.execute(
            select(models.SalesLedger.project_id, models.SalesLedger.sales_uid)
            .where(models.SalesLedger.order_state.in_(("pending", "draft")))
        )
        hidden = {pid for pid, suid in sub.all() if suid != current.id}
        if hidden:
            items = [p for p in items if p.id not in hidden]
    # 🆕 项目目录列表行级可见性（仅列表显示；可逆开关 project_dir_own_only，默认开启）：
    # 设计/电工/装配 只列被派单(worker_id)的；销售员只列自己下单(sales_uid)的；
    # 外加按项目目录 销售/电工/设计师 列姓名匹配补授的「可见名单」__viz_uids__（存量数据补全）。
    # 管理层/各部门负责人/采购/仓库/财务/物流等不受影响（看全部，详见 user_can_view_project）。
    from ..config import settings as _cfg
    _RESTRICTED = {"designer", "electrician", "assembler", "sales"}
    _codes = current.role_codes
    # 多角色取并集：仅当用户「全部角色」都在受限集时才受限；只要有一个更宽的角色就看全部
    restricted = _cfg.project_dir_own_only and bool(_codes) and _codes <= _RESTRICTED
    if restricted:
        my_pids: set[int] = set()
        if "sales" in _codes:
            sub = await db.execute(
                select(models.SalesLedger.project_id).where(models.SalesLedger.sales_uid == current.id))
            my_pids |= {r[0] for r in sub.all()}
        if _codes & {"designer", "electrician", "assembler"}:
            sub = await db.execute(
                select(models.DeptOrder.project_id).where(models.DeptOrder.worker_id == current.id))
            my_pids |= {r[0] for r in sub.all()}
        items = [p for p in items
                 if p.id in my_pids or current.id in ((p.extra or {}).get("__viz_uids__") or [])]
    visible = []
    for p in items:
        # 受限角色的列表已由 被派单/下单/可见名单 过滤定案，不再二次过成员检查；
        # 其余角色按 user_can_view_project(成员/负责人/管理层)。
        if restricted or await user_can_view_project(db, current, p):
            visible.append(await _project_to_out(p, db))
    return visible


@router.post("", response_model=schemas.ProjectOut)
async def create_project(
    data: schemas.ProjectCreate,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    if not current.has_role("admin", "manager"):
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
    await db.flush()  # 拿到 p.id 用于添加默认成员
    # 默认把所有 active 的非 admin/manager 用户加为 edit 成员
    added_count = await _add_all_active_users_as_members(db, p.id, permission="edit")
    # 预置 4 个固定数据表（表头按模板建好，空数据），与导入 Excel 后的结构一致
    sheet_count = await create_default_template_sheets(db, p.id)
    await db.commit()
    await db.refresh(p)
    res = await db.execute(select(models.Project).where(models.Project.id == p.id))
    p = res.scalar_one()
    await write_audit(
        db, user=current, action="create_project",
        target_type="project", target_id=p.id,
        detail=f"{p.code} {p.name} · 默认添加 {added_count} 个成员 · 预置 {sheet_count} 张数据表"
    )
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
    # 🆕 v3 详单闸门：销售/电工/装配/售后角色无项目详情权限（菜单矩阵同源）
    from ..menus import user_can_view_detail
    if not user_can_view_detail(current):
        raise HTTPException(403, "你没有项目详单权限")
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
    # 🆕 #5 生产主管(pm_lead)可在项目目录调整项目状态——仅限"只改状态"，不放开编号/名称/描述/负责人
    status_only = (data.status is not None and data.code is None and data.name is None
                   and data.description is None and data.manager_id is None)
    if not await user_can_edit_project(db, current, p):
        if not (status_only and current.has_role("pm_lead")):
            raise HTTPException(403, "无权修改项目")
    if data.code is not None:
        new_code = data.code.strip()
        if not new_code:
            raise HTTPException(400, "项目编号不能为空")
        if new_code != p.code:
            # 唯一约束：只与活跃项目比较，已软删的占位 code 允许冲突
            res = await db.execute(
                select(models.Project).where(
                    models.Project.code == new_code,
                    models.Project.is_deleted == False,
                    models.Project.id != p.id,
                )
            )
            if res.scalar_one_or_none():
                raise HTTPException(400, "项目编号已存在")
            p.code = new_code
    if data.name is not None: p.name = data.name
    if data.description is not None: p.description = data.description
    if data.status is not None:
        old_status = p.status
        p.status = data.status
        # 状态切到「已完成」→ 冻结完成日期（已过时间/剩余制作时间从此不再实时计算）；
        # 切回非「已完成」→ 清除完成日期，恢复按 TODAY() 实时计算
        cd_key = f"{OVERVIEW_KEY_PREFIX}完成日期"
        if data.status == "已完成" and old_status != "已完成":
            extra = dict(p.extra or {})
            if not extra.get(cd_key):
                extra[cd_key] = date.today().strftime("%Y-%m-%d")
                p.extra = extra
        elif data.status != "已完成" and old_status == "已完成":
            extra = dict(p.extra or {})
            if extra.pop(cd_key, None) is not None:
                p.extra = extra
    if data.manager_id is not None and current.has_role("admin", "manager"):
        p.manager_id = data.manager_id
    await db.commit()
    await db.refresh(p)
    res = await db.execute(select(models.Project).where(models.Project.id == pid))
    p = res.scalar_one()
    return await _project_to_out(p, db)


@router.get("/_overview/template")
async def get_overview_template(
    _: models.User = Depends(get_current_user),
):
    """返回项目一览的固定列模板（由前端渲染表头）。"""
    from ..sheet_templates import OVERVIEW_FIELDS
    return {"fields": OVERVIEW_FIELDS}


@router.put("/{pid}/header-cell", response_model=schemas.Msg)
async def update_project_header_cell(
    pid: int, data: schemas.HeaderCellUpdate,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    """更新项目头表的单个字段（数量 / 销售 / 设计师 / 电器 / 下单日期 / 交货日期 / 制表日期 等）。
    存在 project.extra 的 __h__<key> 下，同项目所有 datasheet 共享。
    """
    res = await db.execute(select(models.Project).where(models.Project.id == pid))
    p = res.scalar_one_or_none()
    if not p or p.is_deleted:
        raise HTTPException(404, "项目不存在")
    if not await user_can_edit_project(db, current, p):
        raise HTTPException(403, "无权编辑此项目")

    key = data.key.strip()
    if not key:
        raise HTTPException(400, "key 不能为空")

    # 一览（is_overview=True）写到 __o__ 前缀
    prefix = OVERVIEW_KEY_PREFIX if data.is_overview else HEADER_KEY_PREFIX

    # JSONB 字段必须重新赋值才会被 SQLAlchemy 识别为脏
    extra = dict(p.extra or {})
    storage_key = f"{prefix}{key}"
    val = data.value
    normalized_val = val.strip() if isinstance(val, str) else val
    # 日期型字段统一规范化为 YYYY-MM-DD（用户填 2026/5/12、2026.5.12 等都自动转）
    if isinstance(normalized_val, str) and is_date_field(key):
        normalized_val = normalize_date_str(normalized_val)

    if val is None or (isinstance(val, str) and val.strip() == ""):
        extra.pop(storage_key, None)
        # 一览删除时也同步删除对应的 __h__（如果是 alias 字段）
        if data.is_overview:
            from ..sheet_templates import OVERVIEW_HEADER_ALIAS
            h_key = OVERVIEW_HEADER_ALIAS.get(key)
            if h_key:
                extra.pop(f"{HEADER_KEY_PREFIX}{h_key}", None)
    else:
        extra[storage_key] = normalized_val
        # 一览写入时同步到项目详情头表（仅对 alias 映射里的字段）
        if data.is_overview:
            from ..sheet_templates import OVERVIEW_HEADER_ALIAS
            h_key = OVERVIEW_HEADER_ALIAS.get(key)
            if h_key:
                extra[f"{HEADER_KEY_PREFIX}{h_key}"] = normalized_val
    p.extra = extra
    await db.commit()

    await write_audit(
        db, user=current, action="update_project_header",
        target_type="project", target_id=pid,
        detail=f"{key}={val}",
    )
    return schemas.Msg(message="ok")


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

    # 🆕 #3 防止软删导致财务「待开票」工作项静默丢失：有进行中开票流的台账先拦下
    lr = await db.execute(
        select(models.SalesLedger).where(
            models.SalesLedger.project_id == pid,
            models.SalesLedger.invoice_state.in_(("applying", "pending_invoice", "invoiced")),
        )
    )
    if lr.scalar_one_or_none() is not None:
        raise HTTPException(
            409, "该项目有开票记录（待审批/待开票/已开票），请先在财务处理发票后再删除")

    # 清理派生数据 + 作废各部门任务单 + 软删项目（统一走 soft_delete_project）
    counts = await soft_delete_project(db, p, void_dept_orders=True)
    await db.commit()
    await write_audit(
        db, user=_, action="delete_project",
        target_type="project", target_id=pid,
        detail=(
            f"{orig_code} · 清理 {counts['datasheets']} 数据表 / "
            f"{counts['fields']} 字段 / {counts['field_perms']} 权限 / "
            f"{counts['records']} 行 / {counts['members']} 成员 / "
            f"{counts['dept_orders_voided']} 任务单作废"
        )
    )
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
    if user.has_role("admin", "manager"):
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


@router.post("/{pid}/members/batch", response_model=List[schemas.ProjectMemberOut])
async def add_members_batch(
    pid: int, data: schemas.ProjectMemberBatchIn,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    """一次添加多个成员；已经是成员的跳过；返回本次新加进来的列表。"""
    res = await db.execute(select(models.Project).where(models.Project.id == pid))
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")
    if not await _can_manage_members(db, current, p):
        raise HTTPException(403, "无权添加成员")
    if data.permission not in ("edit", "view"):
        raise HTTPException(400, "权限取值必须是 edit 或 view")
    if not data.user_ids:
        return []

    # 去重 + 过滤已存在成员
    user_ids = list(dict.fromkeys(data.user_ids))  # 保序去重
    res = await db.execute(
        select(models.ProjectMember.user_id).where(
            models.ProjectMember.project_id == pid,
            models.ProjectMember.user_id.in_(user_ids),
        )
    )
    existing = {r[0] for r in res.all()}
    new_ids = [uid for uid in user_ids if uid not in existing]
    if not new_ids:
        return []

    # 校验候选用户存在
    res = await db.execute(
        select(models.User).where(
            models.User.id.in_(new_ids), models.User.is_active == True
        )
    )
    valid_users = {u.id: u for u in res.scalars().all()}

    created: List[models.ProjectMember] = []
    for uid in new_ids:
        if uid not in valid_users:
            continue
        m = models.ProjectMember(project_id=pid, user_id=uid, permission=data.permission)
        db.add(m)
        created.append(m)
    await db.commit()

    # 回查带 user/role 信息
    if created:
        res = await db.execute(
            select(models.ProjectMember).where(
                models.ProjectMember.id.in_([m.id for m in created])
            )
        )
        created = list(res.scalars().all())
    await write_audit(
        db, user=current, action="add_members_batch",
        target_type="project", target_id=pid,
        detail=f"+{len(created)} members (skipped {len(existing)} existed)"
    )
    return [_member_to_out(m) for m in created]


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
