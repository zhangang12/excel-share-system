"""数据表 / 字段 / 行（Sprint 3 核心）"""
import json
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete as sql_delete

from ..database import get_db
from .. import models, schemas
from .ws_router import broadcast_cell_changed
from ..deps import (
    get_current_user, require_not_viewer,
    user_can_view_project, user_can_edit_project,
)

router = APIRouter(prefix="/api", tags=["数据表"])


# ---------- helpers ----------
async def _get_project_or_404(db: AsyncSession, pid: int) -> models.Project:
    res = await db.execute(
        select(models.Project).where(models.Project.id == pid, models.Project.is_deleted == False)
    )
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")
    return p


async def _get_datasheet_or_404(db: AsyncSession, did: int) -> models.Datasheet:
    res = await db.execute(select(models.Datasheet).where(models.Datasheet.id == did))
    d = res.scalar_one_or_none()
    if not d:
        raise HTTPException(404, "数据表不存在")
    return d


async def _datasheet_to_out(d: models.Datasheet, db: AsyncSession) -> schemas.DatasheetOut:
    fres = await db.execute(
        select(func.count(models.Field.id)).where(models.Field.datasheet_id == d.id)
    )
    rres = await db.execute(
        select(func.count(models.Record.id)).where(models.Record.datasheet_id == d.id)
    )
    header_lines = None
    if d.header_lines:
        try:
            header_lines = json.loads(d.header_lines)
        except Exception:
            header_lines = None
    return schemas.DatasheetOut(
        id=d.id, project_id=d.project_id, name=d.name, sort_order=d.sort_order,
        field_count=fres.scalar() or 0, record_count=rres.scalar() or 0,
        header_lines=header_lines,
        created_at=d.created_at, updated_at=d.updated_at,
    )


def _field_to_out(f: models.Field) -> schemas.FieldOut:
    cfg: Optional[dict] = None
    if f.config:
        try:
            cfg = json.loads(f.config)
        except Exception:
            cfg = None
    return schemas.FieldOut(
        id=f.id, datasheet_id=f.datasheet_id, name=f.name, type=f.type,
        sort_order=f.sort_order, config=cfg, created_at=f.created_at,
    )


def _record_to_out(r: models.Record) -> schemas.RecordOut:
    return schemas.RecordOut(
        id=r.id, datasheet_id=r.datasheet_id, sort_order=r.sort_order,
        values=r.values or {}, created_at=r.created_at, updated_at=r.updated_at,
    )


# ====================================================
# 数据表 CRUD
# ====================================================
@router.get("/projects/{pid}/datasheets", response_model=List[schemas.DatasheetOut])
async def list_datasheets(
    pid: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await _get_project_or_404(db, pid)
    # 🆕 v3 详单闸门：与项目详情同源（销售/电工/装配/售后角色 403）
    from ..menus import user_can_view_detail
    if not user_can_view_detail(current):
        raise HTTPException(403, "你没有项目详单权限")
    if not await user_can_view_project(db, current, p):
        raise HTTPException(403, "无权访问")
    res = await db.execute(
        select(models.Datasheet)
        .where(models.Datasheet.project_id == pid)
        .order_by(models.Datasheet.sort_order, models.Datasheet.id)
    )
    return [await _datasheet_to_out(d, db) for d in res.scalars().all()]


@router.post("/projects/{pid}/datasheets", response_model=schemas.DatasheetOut)
async def create_datasheet(
    pid: int, data: schemas.DatasheetCreate,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    p = await _get_project_or_404(db, pid)
    if not await user_can_edit_project(db, current, p):
        raise HTTPException(403, "无权创建数据表")
    # 计算 sort_order
    res = await db.execute(
        select(func.max(models.Datasheet.sort_order)).where(models.Datasheet.project_id == pid)
    )
    max_order = res.scalar() or -1
    d = models.Datasheet(project_id=pid, name=data.name, sort_order=max_order + 1)
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return await _datasheet_to_out(d, db)


@router.put("/datasheets/{did}", response_model=schemas.DatasheetOut)
async def update_datasheet(
    did: int, data: schemas.DatasheetUpdate,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    d = await _get_datasheet_or_404(db, did)
    p = await _get_project_or_404(db, d.project_id)
    if not await user_can_edit_project(db, current, p):
        raise HTTPException(403, "无权修改")
    if data.name is not None:
        d.name = data.name
    if data.sort_order is not None:
        d.sort_order = data.sort_order
    await db.commit()
    await db.refresh(d)
    return await _datasheet_to_out(d, db)


@router.delete("/datasheets/{did}", response_model=schemas.Msg)
async def delete_datasheet(
    did: int,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    d = await _get_datasheet_or_404(db, did)
    p = await _get_project_or_404(db, d.project_id)
    if not await user_can_edit_project(db, current, p):
        raise HTTPException(403, "无权删除")
    await db.delete(d)
    await db.commit()
    return schemas.Msg(message="已删除")


# ====================================================
# 字段（列） CRUD
# ====================================================
@router.get("/datasheets/{did}/fields", response_model=List[schemas.FieldOut])
async def list_fields(
    did: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    d = await _get_datasheet_or_404(db, did)
    p = await _get_project_or_404(db, d.project_id)
    if not await user_can_view_project(db, current, p):
        raise HTTPException(403, "无权访问")
    res = await db.execute(
        select(models.Field).where(models.Field.datasheet_id == did)
        .order_by(models.Field.sort_order, models.Field.id)
    )
    return [_field_to_out(f) for f in res.scalars().all()]


@router.post("/datasheets/{did}/fields", response_model=schemas.FieldOut)
async def create_field(
    did: int, data: schemas.FieldCreate,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    from ..sheet_templates import is_known_sheet
    d = await _get_datasheet_or_404(db, did)
    p = await _get_project_or_404(db, d.project_id)
    if not await user_can_edit_project(db, current, p):
        raise HTTPException(403, "无权创建字段")
    if is_known_sheet(d.name):
        raise HTTPException(400, f"「{d.name}」是系统固定模板，字段不能新增")
    if data.type not in schemas.FIELD_TYPES:
        raise HTTPException(400, f"字段类型必须是 {schemas.FIELD_TYPES}")
    res = await db.execute(
        select(func.max(models.Field.sort_order)).where(models.Field.datasheet_id == did)
    )
    max_order = res.scalar() or -1
    f = models.Field(
        datasheet_id=did, name=data.name, type=data.type,
        sort_order=data.sort_order if data.sort_order is not None else max_order + 1,
        config=json.dumps(data.config, ensure_ascii=False) if data.config else None,
    )
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return _field_to_out(f)


@router.put("/fields/{fid}", response_model=schemas.FieldOut)
async def update_field(
    fid: int, data: schemas.FieldUpdate,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    from ..sheet_templates import is_known_sheet
    res = await db.execute(select(models.Field).where(models.Field.id == fid))
    f = res.scalar_one_or_none()
    if not f:
        raise HTTPException(404, "字段不存在")
    d = await _get_datasheet_or_404(db, f.datasheet_id)
    p = await _get_project_or_404(db, d.project_id)
    if not await user_can_edit_project(db, current, p):
        raise HTTPException(403, "无权修改")
    if is_known_sheet(d.name) and data.name is not None and data.name != f.name:
        raise HTTPException(400, f"「{d.name}」是系统固定模板，字段名不能改")
    if data.name is not None:
        f.name = data.name
    if data.type is not None:
        if data.type not in schemas.FIELD_TYPES:
            raise HTTPException(400, f"字段类型必须是 {schemas.FIELD_TYPES}")
        f.type = data.type
    if data.sort_order is not None:
        f.sort_order = data.sort_order
    if data.config is not None:
        f.config = json.dumps(data.config, ensure_ascii=False) if data.config else None
    await db.commit()
    await db.refresh(f)
    return _field_to_out(f)


@router.delete("/fields/{fid}", response_model=schemas.Msg)
async def delete_field(
    fid: int,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    from ..sheet_templates import is_known_sheet
    res = await db.execute(select(models.Field).where(models.Field.id == fid))
    f = res.scalar_one_or_none()
    if not f:
        raise HTTPException(404, "字段不存在")
    d = await _get_datasheet_or_404(db, f.datasheet_id)
    p = await _get_project_or_404(db, d.project_id)
    if not await user_can_edit_project(db, current, p):
        raise HTTPException(403, "无权删除")
    if is_known_sheet(d.name):
        raise HTTPException(400, f"「{d.name}」是系统固定模板，字段不能删")
    await db.delete(f)
    await db.commit()
    return schemas.Msg(message="已删除")


# ====================================================
# Record（行） CRUD
# ====================================================
@router.get("/datasheets/{did}/records", response_model=List[schemas.RecordOut])
async def list_records(
    did: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    d = await _get_datasheet_or_404(db, did)
    p = await _get_project_or_404(db, d.project_id)
    if not await user_can_view_project(db, current, p):
        raise HTTPException(403, "无权访问")
    res = await db.execute(
        select(models.Record).where(models.Record.datasheet_id == did)
        .order_by(models.Record.sort_order, models.Record.id)
    )
    return [_record_to_out(r) for r in res.scalars().all()]


@router.post("/datasheets/{did}/records", response_model=schemas.RecordOut)
async def create_record(
    did: int, data: schemas.RecordCreate,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    d = await _get_datasheet_or_404(db, did)
    p = await _get_project_or_404(db, d.project_id)
    if not await user_can_edit_project(db, current, p):
        raise HTTPException(403, "无权创建行")
    res = await db.execute(
        select(func.max(models.Record.sort_order)).where(models.Record.datasheet_id == did)
    )
    max_order = res.scalar() or -1
    r = models.Record(
        datasheet_id=did, sort_order=max_order + 1, values=data.values or {},
        created_by=current.id, updated_by=current.id,
    )
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return _record_to_out(r)


@router.put("/records/{rid}", response_model=schemas.RecordOut)
async def update_record(
    rid: int, data: schemas.RecordUpdate,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(models.Record).where(models.Record.id == rid))
    r = res.scalar_one_or_none()
    if not r:
        raise HTTPException(404, "行不存在")
    d = await _get_datasheet_or_404(db, r.datasheet_id)
    p = await _get_project_or_404(db, d.project_id)
    if not await user_can_edit_project(db, current, p):
        raise HTTPException(403, "无权修改")
    if data.values is not None:
        r.values = data.values
    if data.sort_order is not None:
        r.sort_order = data.sort_order
    r.updated_by = current.id
    await db.commit()
    await db.refresh(r)
    return _record_to_out(r)


@router.put("/records/{rid}/cell", response_model=schemas.RecordOut)
async def update_cell(
    rid: int, data: schemas.RecordCellUpdate,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    """更新单个单元格。"""
    res = await db.execute(select(models.Record).where(models.Record.id == rid))
    r = res.scalar_one_or_none()
    if not r:
        raise HTTPException(404, "行不存在")
    d = await _get_datasheet_or_404(db, r.datasheet_id)
    p = await _get_project_or_404(db, d.project_id)
    if not await user_can_edit_project(db, current, p):
        raise HTTPException(403, "无权修改")
    # 字段级权限：admin / manager 跳过；其他角色检查 FieldPermission
    if current.role.code not in ("admin", "manager"):
        from sqlalchemy import select as _sel
        fp_res = await db.execute(_sel(models.FieldPermission).where(
            models.FieldPermission.field_id == data.field_id,
            models.FieldPermission.role_id == current.role_id,
        ))
        fp = fp_res.scalar_one_or_none()
        if fp is not None and (not fp.can_view or not fp.can_edit):
            raise HTTPException(403, "无权编辑该字段")
    # 必须重新赋值整个 dict 才能让 SQLAlchemy 感知到 JSONB 变化
    values = dict(r.values or {})
    if data.value is None or data.value == "":
        values.pop(str(data.field_id), None)
    else:
        values[str(data.field_id)] = data.value
    r.values = values
    r.updated_by = current.id
    await db.commit()
    await db.refresh(r)
    await broadcast_cell_changed(
        scope="datasheet", scope_id=r.datasheet_id,
        record_id=r.id, project_id=None,
        field_id=data.field_id, value=data.value, by_user_id=current.id,
    )
    return _record_to_out(r)


@router.delete("/records/{rid}", response_model=schemas.Msg)
async def delete_record(
    rid: int,
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(models.Record).where(models.Record.id == rid))
    r = res.scalar_one_or_none()
    if not r:
        raise HTTPException(404, "行不存在")
    d = await _get_datasheet_or_404(db, r.datasheet_id)
    p = await _get_project_or_404(db, d.project_id)
    if not await user_can_edit_project(db, current, p):
        raise HTTPException(403, "无权删除")
    await db.delete(r)
    await db.commit()
    return schemas.Msg(message="已删除")
