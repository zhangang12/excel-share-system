"""🆕 v3 M07 仓库组：物料主数据 + 出入库（单据化+冲红）+ 实时库存 + 收发存汇总 + 发货清单。

- 实时库存 = init_stock + Σ(in.qty) − Σ(out.qty)（按需聚合，wh_stock 缓存 P1 不做）
- 出库服务端校验 ≤ 实时库存；自动单号 RK/CKyyyymmdd-NNN
- 冲红：生成反向单据(source=冲红)，原单标记 reversed，库存回滚，原单不物理删
- 写权限仅 warehouse / warehouse_lead；管理层只读；设计师只读「查库存」集成点
- 低于安全库存推送 warehouse_lead 池
"""
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user, require_roles
from ..notify import push_message
from ..utils import write_audit
from ..sheet_templates import normalize_date_str
from .attachments_router import save_upload

router = APIRouter(prefix="/api/wh", tags=["仓库组"])

WRITE_ROLES = ("warehouse", "warehouse_lead")


def _can_write(u: models.User) -> bool:
    return u.has_role(*WRITE_ROLES, "admin", "manager")


async def _stock_map(db: AsyncSession, material_ids: Optional[list[int]] = None,
                     upto: Optional[str] = None) -> dict[int, float]:
    """各物料实时库存 = init + Σin − Σout（可选 upto=YYYY-MM-DD 含当日，用于期初/期末）。"""
    q = select(models.WhTxn.material_id, models.WhTxn.direction, func.sum(models.WhTxn.qty))
    if material_ids:
        q = q.where(models.WhTxn.material_id.in_(material_ids))
    if upto:
        q = q.where(models.WhTxn.biz_date <= upto)
    q = q.group_by(models.WhTxn.material_id, models.WhTxn.direction)
    r = await db.execute(q)
    agg: dict[int, float] = defaultdict(float)
    for mid, direction, total in r.all():
        agg[mid] += (total or 0) if direction == "in" else -(total or 0)
    # 叠加期初
    mq = select(models.WhMaterial.id, models.WhMaterial.init_stock)
    if material_ids:
        mq = mq.where(models.WhMaterial.id.in_(material_ids))
    r = await db.execute(mq)
    stock: dict[int, float] = {}
    for mid, init in r.all():
        stock[mid] = (init or 0) + agg.get(mid, 0)
    return stock


def _mat_out(m: models.WhMaterial, stock: float) -> schemas.WhMaterialOut:
    return schemas.WhMaterialOut(
        id=m.id, code=m.code, name=m.name, spec=m.spec, category=m.category,
        unit=m.unit, location=m.location, safety_stock=m.safety_stock or 0,
        init_stock=m.init_stock or 0, status=m.status, stock=stock,
        low=stock < (m.safety_stock or 0),
    )


# ==================== 物料主数据 ====================
@router.get("/materials", response_model=schemas.WhStockOut)
async def list_materials(
    kw: Optional[str] = Query(None),
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """物料 + 实时库存（全员可读=查库存集成点；写操作另校验）。"""
    r = await db.execute(select(models.WhMaterial).order_by(models.WhMaterial.id))
    mats = list(r.scalars().all())
    if kw:
        k = kw.strip()
        mats = [m for m in mats if k in (m.name or "") or k in (m.spec or "") or k in (m.code or "")]
    stock = await _stock_map(db, [m.id for m in mats])
    outs = [_mat_out(m, stock.get(m.id, m.init_stock or 0)) for m in mats]
    return schemas.WhStockOut(materials=outs, total=len(outs),
                              low_count=sum(1 for o in outs if o.low))


@router.post("/materials", response_model=schemas.WhMaterialOut)
async def create_material(
    data: schemas.WhMaterialIn,
    current: models.User = Depends(require_roles(*WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.WhMaterial).where(
        models.WhMaterial.name == data.name.strip(),
        models.WhMaterial.spec == (data.spec or None)))
    if r.scalar_one_or_none():
        raise HTTPException(409, "同名同规格物料已存在")
    m = models.WhMaterial(
        name=data.name.strip(), spec=(data.spec or "").strip() or None,
        category=(data.category or "").strip() or None, unit=data.unit or "个",
        location=(data.location or "").strip() or None,
        safety_stock=data.safety_stock or 0, init_stock=data.init_stock or 0,
        code=(data.code or "").strip() or None,
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)
    await write_audit(db, user=current, action="create", target_type="wh_material", target_id=m.id)
    return _mat_out(m, m.init_stock or 0)


@router.put("/materials/{mid}", response_model=schemas.Msg)
async def update_material(
    mid: int, data: schemas.WhMaterialIn,
    current: models.User = Depends(require_roles(*WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.WhMaterial).where(models.WhMaterial.id == mid))
    m = r.scalar_one_or_none()
    if not m:
        raise HTTPException(404, "物料不存在")
    # 🆕 #85 改名/规格前查重(排除自身)，避免撞 uq_wh_material_name_spec 抛 500
    dup = await db.execute(select(models.WhMaterial).where(
        models.WhMaterial.name == data.name.strip(),
        models.WhMaterial.spec == ((data.spec or "").strip() or None),
        models.WhMaterial.id != mid))
    if dup.scalar_one_or_none():
        raise HTTPException(409, "同名同规格物料已存在")
    m.name = data.name.strip(); m.spec = (data.spec or "").strip() or None
    m.category = (data.category or "").strip() or None; m.unit = data.unit or "个"
    m.location = (data.location or "").strip() or None
    m.safety_stock = data.safety_stock or 0
    await db.commit()
    return schemas.Msg(message="已保存")


# ==================== 出入库 ====================
async def _next_ref(db: AsyncSession, direction: str, biz_date: str) -> str:
    prefix = "RK" if direction == "in" else "CK"
    ymd = biz_date.replace("-", "")
    like = f"{prefix}{ymd}-%"
    r = await db.execute(select(func.count(models.WhTxn.id)).where(models.WhTxn.ref_no.like(like)))
    n = (r.scalar() or 0) + 1
    return f"{prefix}{ymd}-{n:03d}"


@router.post("/txns", response_model=schemas.Msg)
async def create_txn(
    data: schemas.WhTxnIn,
    current: models.User = Depends(require_roles(*WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    if data.direction not in ("in", "out"):
        raise HTTPException(400, "方向必须是 in/out")
    if data.qty <= 0:
        raise HTTPException(400, "数量必须为正数")
    bd = normalize_date_str(data.biz_date) or date.today().isoformat()
    r = await db.execute(select(models.WhMaterial).where(models.WhMaterial.id == data.material_id))
    m = r.scalar_one_or_none()
    if not m:
        raise HTTPException(404, "物料不存在")
    # 出库超库存拦截
    if data.direction == "out":
        stock = (await _stock_map(db, [data.material_id])).get(data.material_id, m.init_stock or 0)
        if data.qty > stock:
            raise HTTPException(400, f"出库数量 {data.qty} 超过现存 {stock}")
    ref = await _next_ref(db, data.direction, bd)
    txn = models.WhTxn(
        material_id=data.material_id, biz_date=bd, direction=data.direction, qty=data.qty,
        source=(data.source or ("采购入库" if data.direction == "in" else "领料出库")),
        party=(data.party or "").strip() or None, project_id=data.project_id,
        ref_no=ref, operator_id=current.id,
    )
    db.add(txn)
    await db.commit()
    # 低库存预警
    stock = (await _stock_map(db, [data.material_id])).get(data.material_id, 0)
    if stock < (m.safety_stock or 0):
        await push_message(db, to_role="warehouse_lead", kind="warn",
                           text=f"【低库存预警】{m.name}{('·'+m.spec) if m.spec else ''} 现存 {stock} 低于安全库存 {m.safety_stock}",
                           biz_type="wh_material", biz_id=m.id)
    await write_audit(db, user=current, action="wh_txn", target_type="wh_txn",
                      target_id=txn.id, detail=f"{ref} {data.direction} {data.qty}")
    return schemas.Msg(message=f"已登记 {ref}")


@router.post("/txns/{tid}/reverse", response_model=schemas.Msg)
async def reverse_txn(
    tid: int,
    current: models.User = Depends(require_roles(*WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """冲红：生成反向单据，原单标记 reversed，库存回滚；原单不删（审计追溯）。"""
    r = await db.execute(select(models.WhTxn).where(models.WhTxn.id == tid))
    o = r.scalar_one_or_none()
    if not o:
        raise HTTPException(404, "单据不存在")
    if o.is_reversal:
        raise HTTPException(400, "冲红单不可再冲红")
    if o.reversed:
        raise HTTPException(400, "该单已被冲红")
    rev_dir = "out" if o.direction == "in" else "in"
    # 🆕 #83 冲红入库单(生成反向出库)需校验负库存：若该入库货已被领用，冲红会击穿“库存非负”
    if rev_dir == "out":
        cur = (await _stock_map(db, [o.material_id])).get(o.material_id, 0)
        if o.qty > cur:
            raise HTTPException(
                400, f"该入库已被领用，现存 {cur} 不足冲红 {o.qty}，请先冲红相关出库单")
    bd = date.today().isoformat()
    ref = await _next_ref(db, rev_dir, bd)
    rev = models.WhTxn(
        material_id=o.material_id, biz_date=bd, direction=rev_dir, qty=o.qty,
        source="冲红", party=f"冲销 {o.ref_no}", project_id=o.project_id,
        ref_no=ref, operator_id=current.id, is_reversal=True, reversal_of=o.id,
    )
    o.reversed = True
    db.add(rev)
    await db.commit()
    await write_audit(db, user=current, action="wh_reverse", target_type="wh_txn",
                      target_id=o.id, detail=f"冲红 {o.ref_no} → {ref}")
    return schemas.Msg(message=f"已冲红 {o.ref_no}（生成 {ref}）")


@router.get("/txns", response_model=List[schemas.WhTxnOut])
async def list_txns(
    direction: Optional[str] = Query(None),
    material_id: Optional[int] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(models.WhTxn)
    if direction in ("in", "out"):
        q = q.where(models.WhTxn.direction == direction)
    if material_id:
        q = q.where(models.WhTxn.material_id == material_id)
    r = await db.execute(q.order_by(models.WhTxn.id.desc()).limit(limit))
    txns = list(r.scalars().all())
    # 项目编号
    pids = {t.project_id for t in txns if t.project_id}
    pmap: dict[int, str] = {}
    if pids:
        r = await db.execute(select(models.Project.id, models.Project.code).where(models.Project.id.in_(pids)))
        pmap = dict(r.all())
    return [schemas.WhTxnOut(
        id=t.id, material_id=t.material_id,
        material_name=t.material.name if t.material else "", spec=t.material.spec if t.material else None,
        biz_date=t.biz_date, direction=t.direction, qty=t.qty, source=t.source, party=t.party,
        project_id=t.project_id, project_code=pmap.get(t.project_id),
        ref_no=t.ref_no, is_reversal=t.is_reversal, reversed=t.reversed, created_at=t.created_at,
    ) for t in txns]


@router.get("/summary", response_model=List[schemas.WhSummaryRow])
async def summary(
    period: str = Query(..., description="YYYY-MM"),
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """收发存汇总：期初(period 月初前)+本期入−本期出=期末，逐物料勾稽。"""
    try:
        y, mo = period.split("-")
        start = f"{int(y):04d}-{int(mo):02d}-01"
        end_mo = int(mo) + 1
        end_y = int(y)
        if end_mo > 12:
            end_mo = 1; end_y += 1
        nxt = f"{end_y:04d}-{end_mo:02d}-01"
    except Exception:
        raise HTTPException(400, "period 格式应为 YYYY-MM")

    r = await db.execute(select(models.WhMaterial).order_by(models.WhMaterial.id))
    mats = list(r.scalars().all())
    # 本期入/出
    r = await db.execute(
        select(models.WhTxn.material_id, models.WhTxn.direction, func.sum(models.WhTxn.qty))
        .where(models.WhTxn.biz_date >= start, models.WhTxn.biz_date < nxt)
        .group_by(models.WhTxn.material_id, models.WhTxn.direction))
    period_io: dict[tuple, float] = {}
    for mid, d, tot in r.all():
        period_io[(mid, d)] = tot or 0
    # 期初 = 月初前的实时库存（upto = start 前一天）
    before = await _stock_map(db, [m.id for m in mats], upto=_minus1(start))

    rows = []
    for m in mats:
        opening = before.get(m.id, m.init_stock or 0)
        in_q = period_io.get((m.id, "in"), 0)
        out_q = period_io.get((m.id, "out"), 0)
        rows.append(schemas.WhSummaryRow(
            material_id=m.id, name=m.name, spec=m.spec, unit=m.unit,
            opening=opening, in_qty=in_q, out_qty=out_q, closing=opening + in_q - out_q))
    return rows


def _minus1(d: str) -> str:
    from datetime import date as _d, timedelta as _t
    y, m, dd = d.split("-")
    return (_d(int(y), int(m), int(dd)) - _t(days=1)).isoformat()


# ==================== 发货清单上传（推物流，M08 看板消费） ====================
@router.post("/ship-list/{project_id}", response_model=schemas.Msg)
async def upload_ship_list(
    project_id: int,
    file: UploadFile = File(...),
    current: models.User = Depends(require_roles(*WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.Project).where(
        models.Project.id == project_id, models.Project.is_deleted == False))  # noqa: E712
    p = r.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")
    await save_upload(db, file, biz_type="ship_list", biz_id=project_id,
                      project_id=project_id, user=current)
    await db.commit()
    await push_message(db, to_role="logistics", kind="info",
                       text=f"【发货清单】{p.code} {p.name} 仓库组已上传发货清单，请安排发货。",
                       biz_type="project", biz_id=project_id)
    return schemas.Msg(message="发货清单已上传并推送物流发货部")
