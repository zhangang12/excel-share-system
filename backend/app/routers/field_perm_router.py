"""字段级权限：管理员配置每个字段对每个角色的可见/可编辑"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete as sql_delete, func

from ..database import get_db
from .. import models, schemas
from ..deps import (
    require_admin, require_admin_or_manager,
    get_current_user,
)
from ..utils import write_audit

router = APIRouter(prefix="/api/permissions", tags=["字段权限"])


# ===== 数据表字段权限 =====
async def _get_perms(db, fid: int) -> list[schemas.FieldPermissionItem]:
    res = await db.execute(
        select(models.FieldPermission, models.Role)
        .join(models.Role, models.Role.id == models.FieldPermission.role_id)
        .where(models.FieldPermission.field_id == fid)
    )
    return [
        schemas.FieldPermissionItem(
            role_id=r.id, role_name=r.name,
            can_view=fp.can_view, can_edit=fp.can_edit
        )
        for fp, r in res.all()
    ]


@router.get("/fields/{fid}", response_model=List[schemas.FieldPermissionItem])
async def list_field_perms(
    fid: int,
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    return await _get_perms(db, fid)


@router.put("/fields/{fid}", response_model=List[schemas.FieldPermissionItem])
async def set_field_perms(
    fid: int, data: schemas.FieldPermissionSetIn,
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    # 删除旧的，写入新的
    await db.execute(sql_delete(models.FieldPermission).where(models.FieldPermission.field_id == fid))
    for it in data.permissions:
        db.add(models.FieldPermission(
            field_id=fid, role_id=it.role_id,
            can_view=it.can_view, can_edit=it.can_edit,
        ))
    await db.commit()
    return await _get_perms(db, fid)


# ===== 一览字段权限 =====
async def _get_ovw_perms(db, fid: int) -> list[schemas.FieldPermissionItem]:
    res = await db.execute(
        select(models.OverviewFieldPermission, models.Role)
        .join(models.Role, models.Role.id == models.OverviewFieldPermission.role_id)
        .where(models.OverviewFieldPermission.field_id == fid)
    )
    return [
        schemas.FieldPermissionItem(
            role_id=r.id, role_name=r.name,
            can_view=fp.can_view, can_edit=fp.can_edit
        )
        for fp, r in res.all()
    ]


@router.get("/overview-fields/{fid}", response_model=List[schemas.FieldPermissionItem])
async def list_ovw_field_perms(
    fid: int,
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    return await _get_ovw_perms(db, fid)


@router.put("/overview-fields/{fid}", response_model=List[schemas.FieldPermissionItem])
async def set_ovw_field_perms(
    fid: int, data: schemas.FieldPermissionSetIn,
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(sql_delete(models.OverviewFieldPermission).where(
        models.OverviewFieldPermission.field_id == fid
    ))
    for it in data.permissions:
        db.add(models.OverviewFieldPermission(
            field_id=fid, role_id=it.role_id,
            can_view=it.can_view, can_edit=it.can_edit,
        ))
    await db.commit()
    return await _get_ovw_perms(db, fid)


# ===== 多角色字段权限并集（最宽松：任一角色放行即放行；未配置角色=默认放行）=====
async def _union_field_perms(db, fields, current, PermModel) -> dict:
    role_ids = current.role_ids
    n = len(role_ids)
    by_field: dict[int, list] = {}
    if fields and role_ids:
        pres = await db.execute(
            select(PermModel).where(
                PermModel.field_id.in_([f.id for f in fields]),
                PermModel.role_id.in_(role_ids),
            )
        )
        for p in pres.scalars().all():
            by_field.setdefault(p.field_id, []).append(p)
    out = {}
    for f in fields:
        rowsf = by_field.get(f.id, [])
        # 有角色未配置该字段 → 该角色默认全放行 → 并集即全放行
        if len(rowsf) < n:
            out[str(f.id)] = {"can_view": True, "can_edit": True}
        else:
            out[str(f.id)] = {
                "can_view": any(p.can_view for p in rowsf),
                "can_edit": any(p.can_edit for p in rowsf),
            }
    return out


# ===== 给前端用：按当前用户的角色，返回字段可见性 map =====
@router.get("/me/datasheet/{did}")
async def my_datasheet_perms(
    did: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """返回 {field_id: {can_view, can_edit}}。admin 始终全权。"""
    fres = await db.execute(
        select(models.Field).where(models.Field.datasheet_id == did)
    )
    fields = fres.scalars().all()
    if current.has_role("admin", "manager"):
        return {str(f.id): {"can_view": True, "can_edit": True} for f in fields}

    # 多角色取并集：取用户全部角色的字段权限，按字段 OR 合并
    return await _union_field_perms(db, fields, current, models.FieldPermission)


@router.get("/me/overview")
async def my_overview_perms(
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    import logging
    log = logging.getLogger("perm")
    log.info("my_overview_perms by user=%s roles=%s", current.username, current.role_codes)
    fres = await db.execute(select(models.OverviewField))
    fields = fres.scalars().all()
    if current.has_role("admin", "manager"):
        return {str(f.id): {"can_view": True, "can_edit": True} for f in fields}
    # 多角色取并集
    return await _union_field_perms(db, fields, current, models.OverviewFieldPermission)



# ============== 权限总览矩阵 ==============
@router.get("/matrix")
async def permission_matrix(
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """返回字段 × 角色 的权限矩阵，用于管理页一览。"""
    from sqlalchemy import select as _sel
    # 所有角色（除 admin，admin 自动全权）
    rres = await db.execute(_sel(models.Role).where(models.Role.code.notin_(["admin", "manager"])).order_by(models.Role.id))
    roles = rres.scalars().all()

    # ========= 一览字段 =========
    ofres = await db.execute(
        _sel(models.OverviewField).order_by(models.OverviewField.sort_order)
    )
    ovw_fields = ofres.scalars().all()
    ofpres = await db.execute(_sel(models.OverviewFieldPermission))
    ofp_map: dict[tuple[int, int], models.OverviewFieldPermission] = {
        (p.field_id, p.role_id): p for p in ofpres.scalars().all()
    }

    overview_matrix = []
    for f in ovw_fields:
        row = {"field_id": f.id, "field_name": f.name, "field_type": f.type, "perms": {}}
        for r in roles:
            p = ofp_map.get((f.id, r.id))
            row["perms"][r.code] = {
                "role_name": r.name,
                "can_view": p.can_view if p else True,
                "can_edit": p.can_edit if p else True,
                "customized": p is not None,
            }
        overview_matrix.append(row)

    # ========= 数据表字段（按 datasheet 分组） =========
    dres = await db.execute(_sel(models.Datasheet).order_by(models.Datasheet.project_id, models.Datasheet.sort_order))
    datasheets = dres.scalars().all()
    # 项目名
    pres = await db.execute(_sel(models.Project))
    proj_map = {p.id: p for p in pres.scalars().all()}

    fpres = await db.execute(_sel(models.FieldPermission))
    fp_map: dict[tuple[int, int], models.FieldPermission] = {
        (p.field_id, p.role_id): p for p in fpres.scalars().all()
    }

    fres = await db.execute(_sel(models.Field).order_by(models.Field.datasheet_id, models.Field.sort_order))
    fields_by_ds: dict[int, list[models.Field]] = {}
    for f in fres.scalars().all():
        fields_by_ds.setdefault(f.datasheet_id, []).append(f)

    datasheet_matrix = []
    for d in datasheets:
        p = proj_map.get(d.project_id)
        if not p:
            continue
        ds_entry = {
            "datasheet_id": d.id,
            "datasheet_name": d.name,
            "project_id": d.project_id,
            "project_code": p.code,
            "project_name": p.name,
            "fields": [],
        }
        for f in fields_by_ds.get(d.id, []):
            field_row = {"field_id": f.id, "field_name": f.name, "field_type": f.type, "perms": {}}
            for r in roles:
                fp = fp_map.get((f.id, r.id))
                field_row["perms"][r.code] = {
                    "role_name": r.name,
                    "can_view": fp.can_view if fp else True,
                    "can_edit": fp.can_edit if fp else True,
                    "customized": fp is not None,
                }
            ds_entry["fields"].append(field_row)
        datasheet_matrix.append(ds_entry)

    return {
        "roles": [{"code": r.code, "name": r.name} for r in roles],
        "overview": overview_matrix,
        "datasheets": datasheet_matrix,
    }


# ============== 🆕 矩阵异步分片加载（解决全量 /matrix 大数据下白屏卡顿） ==============
async def _matrix_roles(db: AsyncSession):
    """矩阵展示用角色（admin/manager 自动全权，不入矩阵）。"""
    rres = await db.execute(
        select(models.Role).where(models.Role.code.notin_(["admin", "manager"]))
        .order_by(models.Role.id)
    )
    return rres.scalars().all()


def _perm_cell(p, role) -> dict:
    return {
        "role_name": role.name,
        "can_view": p.can_view if p else True,
        "can_edit": p.can_edit if p else True,
        "customized": p is not None,
    }


@router.get("/matrix/overview")
async def matrix_overview(
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """🆕 仅返回「项目目录字段」矩阵（轻量，进页即拉）。"""
    roles = await _matrix_roles(db)
    ofres = await db.execute(select(models.OverviewField).order_by(models.OverviewField.sort_order))
    ovw_fields = ofres.scalars().all()
    ofpres = await db.execute(select(models.OverviewFieldPermission))
    ofp_map = {(p.field_id, p.role_id): p for p in ofpres.scalars().all()}

    overview_matrix = []
    for f in ovw_fields:
        row = {"field_id": f.id, "field_name": f.name, "field_type": f.type, "perms": {}}
        for r in roles:
            row["perms"][r.code] = _perm_cell(ofp_map.get((f.id, r.id)), r)
        overview_matrix.append(row)
    return {
        "roles": [{"code": r.code, "name": r.name} for r in roles],
        "overview": overview_matrix,
    }


@router.get("/matrix/datasheet-projects")
async def matrix_datasheet_projects(
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """🆕 有数据表的项目列表（含数据表数量），供「项目进度字段」tab 做项目选择器。"""
    res = await db.execute(
        select(
            models.Project.id, models.Project.code, models.Project.name,
            func.count(models.Datasheet.id).label("ds_count"),
        )
        .join(models.Datasheet, models.Datasheet.project_id == models.Project.id)
        .group_by(models.Project.id, models.Project.code, models.Project.name)
        .order_by(models.Project.id)
    )
    return [
        {"project_id": pid, "project_code": code, "project_name": name, "datasheet_count": cnt}
        for (pid, code, name, cnt) in res.all()
    ]


@router.get("/matrix/datasheets")
async def matrix_datasheets_of_project(
    project_id: int = Query(...),
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """🆕 按需返回「单个项目」的数据表字段矩阵（异步加载，避免一次性算全部项目）。"""
    roles = await _matrix_roles(db)
    pres = await db.execute(select(models.Project).where(models.Project.id == project_id))
    proj = pres.scalar_one_or_none()
    if not proj:
        raise HTTPException(404, "项目不存在")

    dres = await db.execute(
        select(models.Datasheet).where(models.Datasheet.project_id == project_id)
        .order_by(models.Datasheet.sort_order)
    )
    datasheets = dres.scalars().all()
    ds_ids = [d.id for d in datasheets]

    fields_by_ds: dict[int, list[models.Field]] = {}
    fp_map: dict[tuple[int, int], models.FieldPermission] = {}
    if ds_ids:
        fres = await db.execute(
            select(models.Field).where(models.Field.datasheet_id.in_(ds_ids))
            .order_by(models.Field.datasheet_id, models.Field.sort_order)
        )
        all_fields = fres.scalars().all()
        for f in all_fields:
            fields_by_ds.setdefault(f.datasheet_id, []).append(f)
        fids = [f.id for f in all_fields]
        if fids:
            fpres = await db.execute(
                select(models.FieldPermission).where(models.FieldPermission.field_id.in_(fids))
            )
            fp_map = {(p.field_id, p.role_id): p for p in fpres.scalars().all()}

    datasheet_matrix = []
    for d in datasheets:
        ds_entry = {
            "datasheet_id": d.id, "datasheet_name": d.name,
            "project_id": proj.id, "project_code": proj.code, "project_name": proj.name,
            "fields": [],
        }
        for f in fields_by_ds.get(d.id, []):
            field_row = {"field_id": f.id, "field_name": f.name, "field_type": f.type, "perms": {}}
            for r in roles:
                field_row["perms"][r.code] = _perm_cell(fp_map.get((f.id, r.id)), r)
            ds_entry["fields"].append(field_row)
        datasheet_matrix.append(ds_entry)
    return {
        "roles": [{"code": r.code, "name": r.name} for r in roles],
        "datasheets": datasheet_matrix,
    }


# ============== 项目权限克隆（admin / manager） ==============
@router.post("/clone-project/{target_pid}", response_model=schemas.ClonePermsResult)
async def clone_project_permissions(
    target_pid: int,
    data: schemas.ClonePermsIn,
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """把源项目的字段级权限克隆到目标项目，按 (数据表名, 字段名) 匹配。

    - admin 或 manager 角色可调用。
    - 不修改源项目，不删除/新增任何字段；只覆盖目标项目"已匹配字段"的权限记录。
    - 未匹配的字段保留原权限不变（向后兼容存量数据）。
    """
    if data.source_project_id == target_pid:
        raise HTTPException(400, "源项目与目标项目不能相同")

    # 取两个项目
    res = await db.execute(
        select(models.Project).where(
            models.Project.id.in_([data.source_project_id, target_pid]),
            models.Project.is_deleted == False,
        )
    )
    projects = {p.id: p for p in res.scalars().all()}
    src = projects.get(data.source_project_id)
    tgt = projects.get(target_pid)
    if not src:
        raise HTTPException(404, "源项目不存在或已被删除")
    if not tgt:
        raise HTTPException(404, "目标项目不存在或已被删除")

    # 取两个项目的数据表
    res = await db.execute(
        select(models.Datasheet).where(
            models.Datasheet.project_id.in_([src.id, tgt.id])
        )
    )
    all_ds = res.scalars().all()
    src_ds = [d for d in all_ds if d.project_id == src.id]
    tgt_ds = [d for d in all_ds if d.project_id == tgt.id]

    if not src_ds:
        raise HTTPException(400, "源项目没有数据表，无可克隆的权限")
    if not tgt_ds:
        raise HTTPException(400, "目标项目没有数据表，无法克隆")

    # 取所有相关字段（一次性查回，避免 N+1）
    all_ds_ids = [d.id for d in src_ds] + [d.id for d in tgt_ds]
    res = await db.execute(
        select(models.Field).where(models.Field.datasheet_id.in_(all_ds_ids))
    )
    all_fields = res.scalars().all()
    fields_by_ds: dict[int, list[models.Field]] = {}
    for f in all_fields:
        fields_by_ds.setdefault(f.datasheet_id, []).append(f)

    # 取源项目所有字段权限（注意：仅源项目的字段范围）
    src_field_ids = [f.id for d in src_ds for f in fields_by_ds.get(d.id, [])]
    src_perms_by_field: dict[int, list[models.FieldPermission]] = {}
    if src_field_ids:
        res = await db.execute(
            select(models.FieldPermission).where(
                models.FieldPermission.field_id.in_(src_field_ids)
            )
        )
        for p in res.scalars().all():
            src_perms_by_field.setdefault(p.field_id, []).append(p)

    # 数据表名 -> 字段名 -> field（源 / 目标）
    def build_index(ds_list):
        idx: dict[str, dict[str, models.Field]] = {}
        for d in ds_list:
            # 同名数据表只取第一个
            if d.name in idx:
                continue
            idx[d.name] = {f.name: f for f in fields_by_ds.get(d.id, [])}
        return idx

    src_index = build_index(src_ds)
    tgt_index = build_index(tgt_ds)

    matched_datasheets: list[str] = []
    unmatched_target_datasheets: list[str] = []
    skipped_target_fields: list[str] = []
    cloned_count = 0

    # 按目标项目的数据表逐个匹配
    for ds_name, tgt_fields in tgt_index.items():
        if ds_name not in src_index:
            unmatched_target_datasheets.append(ds_name)
            continue
        src_fields = src_index[ds_name]
        ds_has_clone = False
        for fname, tgt_field in tgt_fields.items():
            src_field = src_fields.get(fname)
            if not src_field:
                skipped_target_fields.append(f"{ds_name} / {fname}")
                continue
            # 覆盖目标字段的权限：先删后插
            await db.execute(
                sql_delete(models.FieldPermission).where(
                    models.FieldPermission.field_id == tgt_field.id
                )
            )
            for p in src_perms_by_field.get(src_field.id, []):
                db.add(models.FieldPermission(
                    field_id=tgt_field.id,
                    role_id=p.role_id,
                    can_view=p.can_view,
                    can_edit=p.can_edit,
                ))
            cloned_count += 1
            ds_has_clone = True
        if ds_has_clone and ds_name not in matched_datasheets:
            matched_datasheets.append(ds_name)

    await db.commit()

    await write_audit(
        db, user=current,
        action="clone_project_permissions",
        target_type="project", target_id=target_pid,
        detail=f"from project#{src.id}({src.code}) -> {target_pid}({tgt.code}) · {cloned_count} fields"
    )

    return schemas.ClonePermsResult(
        cloned_field_count=cloned_count,
        matched_datasheets=matched_datasheets,
        unmatched_target_datasheets=unmatched_target_datasheets,
        skipped_target_fields=skipped_target_fields,
        message=(
            f"已克隆 {cloned_count} 个字段的权限配置" if cloned_count
            else "没有匹配到可克隆的字段（数据表名 / 字段名都需一致）"
        ),
    )
