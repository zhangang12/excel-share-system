"""字段级权限：管理员配置每个字段对每个角色的可见/可编辑"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete as sql_delete

from ..database import get_db
from .. import models, schemas
from ..deps import require_admin, require_admin_or_manager, get_current_user

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
    if current.role.code in ("admin", "manager"):
        return {str(f.id): {"can_view": True, "can_edit": True} for f in fields}

    # 取当前角色的所有权限
    pres = await db.execute(
        select(models.FieldPermission).where(
            models.FieldPermission.field_id.in_([f.id for f in fields]),
            models.FieldPermission.role_id == current.role_id,
        )
    )
    perm_map = {p.field_id: p for p in pres.scalars().all()}
    # 未配置默认 can_view/can_edit = True (向后兼容)
    out = {}
    for f in fields:
        p = perm_map.get(f.id)
        if p is None:
            out[str(f.id)] = {"can_view": True, "can_edit": True}
        else:
            out[str(f.id)] = {"can_view": p.can_view, "can_edit": p.can_edit}
    return out


@router.get("/me/overview")
async def my_overview_perms(
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    import logging
    log = logging.getLogger("perm")
    log.info("my_overview_perms by user=%s role=%s", current.username, current.role.code if current.role else None)
    fres = await db.execute(select(models.OverviewField))
    fields = fres.scalars().all()
    if current.role.code in ("admin", "manager"):
        return {str(f.id): {"can_view": True, "can_edit": True} for f in fields}
    pres = await db.execute(
        select(models.OverviewFieldPermission).where(
            models.OverviewFieldPermission.field_id.in_([f.id for f in fields]),
            models.OverviewFieldPermission.role_id == current.role_id,
        )
    )
    perm_map = {p.field_id: p for p in pres.scalars().all()}
    out = {}
    for f in fields:
        p = perm_map.get(f.id)
        if p is None:
            out[str(f.id)] = {"can_view": True, "can_edit": True}
        else:
            out[str(f.id)] = {"can_view": p.can_view, "can_edit": p.can_edit}
    return out



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
