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
from sqlalchemy import select, func, update as sa_update, delete as sa_delete

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user, require_roles
from ..notify import push_message
from ..utils import write_audit
from ..sheet_templates import normalize_date_str
from .attachments_router import save_upload, delete_attachment_file

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
    up = m.unit_price
    return schemas.WhMaterialOut(
        id=m.id, code=m.code, category_id=m.category_id, name=m.name, spec=m.spec, category=m.category,
        material_grade=m.material_grade,
        unit=m.unit, unit_price=up, location=m.location, safety_stock=m.safety_stock or 0,
        init_stock=m.init_stock or 0, status=m.status, stock=stock,
        stock_value=round(stock * up, 2) if up is not None else None,  # 🆕 需求三：库存总价
        low=stock < (m.safety_stock or 0),
        custom_values=m.custom_values or {},
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
        category=(data.category or "").strip() or None,
        material_grade=(data.material_grade or "").strip() or None, unit=data.unit or "个",
        unit_price=data.unit_price,   # 🆕 需求三：参考单价
        location=(data.location or "").strip() or None,
        safety_stock=data.safety_stock or 0, init_stock=data.init_stock or 0,
        code=(data.code or "").strip() or None,
        category_id=data.category_id,
        custom_values=await _clean_wh_custom(db, data.custom_values),
    )
    # 🆕 选了编码分类且未手填编码 → 自动发码（大类+中类+细分+4位流水）
    if data.category_id and not m.code:
        m.code = await _gen_material_code(db, data.category_id)
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
    m.material_grade = (data.material_grade or "").strip() or None
    m.unit_price = data.unit_price   # 🆕 需求三：参考单价
    m.location = (data.location or "").strip() or None
    m.safety_stock = data.safety_stock or 0
    # 🆕 编码分类：新选/改选细分类时自动重发码（原编码作废，编码跟分类走）
    if data.category_id and data.category_id != m.category_id:
        m.category_id = data.category_id
        m.code = await _gen_material_code(db, data.category_id)
    elif data.category_id and not m.code:
        m.code = await _gen_material_code(db, data.category_id)
    m.custom_values = await _clean_wh_custom(db, data.custom_values)
    await db.commit()
    return schemas.Msg(message="已保存")


@router.delete("/materials/{mid}", response_model=schemas.Msg)
async def delete_material(
    mid: int,
    current: models.User = Depends(require_roles(*WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """🆕 删除物料主数据。已有出入库流水的不允许硬删（会破坏库存勾稽），提示改用停用/先冲红。"""
    m = (await db.execute(select(models.WhMaterial).where(models.WhMaterial.id == mid))).scalar_one_or_none()
    if not m:
        raise HTTPException(404, "物料不存在")
    cnt = (await db.execute(select(func.count(models.WhTxn.id)).where(
        models.WhTxn.material_id == mid))).scalar() or 0
    if cnt:
        raise HTTPException(400, f"该物料已有 {cnt} 条出入库流水，不能删除（会破坏库存勾稽）")
    await db.delete(m)
    await db.commit()
    await write_audit(db, user=current, action="delete", target_type="wh_material", target_id=mid)
    return schemas.Msg(message="物料已删除")


# ==================== 🆕 仓库物料自定义字段（可配置列，跟采购 R6 同一套做法）====================
_WH_FIELD_ADMIN_ROLES = ("warehouse_lead",)   # 配置字段：仓库主管（admin/manager 由 require_roles 自动放行）


async def _wh_custom_fields(db: AsyncSession, enabled_only: bool = False):
    q = select(models.WhMaterialCustomField).order_by(
        models.WhMaterialCustomField.sort_order, models.WhMaterialCustomField.id)
    if enabled_only:
        q = q.where(models.WhMaterialCustomField.enabled == True)  # noqa: E712
    return list((await db.execute(q)).scalars().all())


async def _clean_wh_custom(db: AsyncSession, custom_values: Optional[dict]) -> dict:
    """校验必填、净化物料自定义字段值（只保留启用字段、去空）。"""
    fields = await _wh_custom_fields(db, enabled_only=True)
    cv = custom_values or {}
    clean: dict = {}
    missing: list[str] = []
    for f in fields:
        key = str(f.id)
        val = cv.get(key)
        sval = "" if val is None else str(val).strip()
        if f.required and not sval:
            missing.append(f.label)
        elif sval:
            clean[key] = val
    if missing:
        raise HTTPException(400, f"必填自定义字段未填写：{'、'.join(missing)}")
    return clean


@router.get("/material-custom-fields", response_model=List[schemas.WhMaterialCustomFieldOut])
async def list_wh_custom_fields(
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """仓库物料自定义字段列表（所有登录用户可读，用于渲染列与输入框）。"""
    return [schemas.WhMaterialCustomFieldOut.model_validate(f) for f in await _wh_custom_fields(db)]


@router.post("/material-custom-fields", response_model=schemas.WhMaterialCustomFieldOut)
async def create_wh_custom_field(
    body: schemas.WhMaterialCustomFieldIn,
    current: models.User = Depends(require_roles(*_WH_FIELD_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    f = models.WhMaterialCustomField(**body.model_dump())
    db.add(f)
    await db.commit()
    await db.refresh(f)
    await write_audit(db, user=current, action="create", target_type="wh_material_custom_field", target_id=f.id)
    return schemas.WhMaterialCustomFieldOut.model_validate(f)


@router.put("/material-custom-fields/{fid}", response_model=schemas.WhMaterialCustomFieldOut)
async def update_wh_custom_field(
    fid: int,
    body: schemas.WhMaterialCustomFieldIn,
    current: models.User = Depends(require_roles(*_WH_FIELD_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    f = (await db.execute(select(models.WhMaterialCustomField).where(
        models.WhMaterialCustomField.id == fid))).scalar_one_or_none()
    if not f:
        raise HTTPException(404, "字段不存在")
    for k, v in body.model_dump().items():
        setattr(f, k, v)
    await db.commit()
    await db.refresh(f)
    return schemas.WhMaterialCustomFieldOut.model_validate(f)


@router.delete("/material-custom-fields/{fid}", response_model=schemas.Msg)
async def delete_wh_custom_field(
    fid: int,
    current: models.User = Depends(require_roles(*_WH_FIELD_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """删除字段定义（已录入物料里的历史值保留在 custom_values 中，只是不再展示/校验）。"""
    f = (await db.execute(select(models.WhMaterialCustomField).where(
        models.WhMaterialCustomField.id == fid))).scalar_one_or_none()
    if not f:
        raise HTTPException(404, "字段不存在")
    await db.delete(f)
    await db.commit()
    await write_audit(db, user=current, action="delete", target_type="wh_material_custom_field", target_id=fid)
    return schemas.Msg(message="已删除该自定义字段")



# ==================== 🆕 物料编码分类(3级树) + 自动发码 ====================
# 编码 = 大类(1位)+中类(2位)+细分类(2位) 前缀 + 4位流水号，如 1·01·01 → 101010001。
# 树在「字典设置-物料编码分类」维护；物料主数据选到细分类，保存时自动发码。

_SEG_LEN = {1: 1, 2: 2, 3: 2}   # 各级段码位数
# 字典/编码分类的维护角色：采购主管（admin/manager 由 require_roles 自动放行）
_DICT_ADMIN_ROLES = ("buyer_lead",)


@router.get("/material-categories", response_model=List[schemas.MaterialCategoryOut])
async def list_material_categories(
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """物料编码分类（平铺全量，前端组树）。所有登录用户可读。"""
    r = await db.execute(select(models.MaterialCategory).order_by(
        models.MaterialCategory.level, models.MaterialCategory.sort_order, models.MaterialCategory.id))
    return [schemas.MaterialCategoryOut.model_validate(x) for x in r.scalars().all()]


@router.post("/material-categories", response_model=schemas.MaterialCategoryOut)
async def create_material_category(
    body: schemas.MaterialCategoryIn,
    current: models.User = Depends(require_roles(*_DICT_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    level = 1
    if body.parent_id:
        p = (await db.execute(select(models.MaterialCategory).where(
            models.MaterialCategory.id == body.parent_id))).scalar_one_or_none()
        if not p:
            raise HTTPException(404, "上级分类不存在")
        if p.level >= 3:
            raise HTTPException(400, "最多三级（大类→中类→细分类）")
        level = p.level + 1
    want = _SEG_LEN[level]
    if len(body.seg_code) != want:
        raise HTTPException(400, f"第{level}级段码须为 {want} 位数字（如 {'1' if want == 1 else '01'}）")
    dup = await db.execute(select(models.MaterialCategory).where(
        models.MaterialCategory.parent_id == body.parent_id,
        models.MaterialCategory.seg_code == body.seg_code))
    if dup.scalar_one_or_none():
        raise HTTPException(409, "同级下该段码已存在")
    c = models.MaterialCategory(parent_id=body.parent_id, level=level, seg_code=body.seg_code,
                                name=body.name.strip(), sort_order=body.sort_order, enabled=body.enabled)
    db.add(c)
    await db.commit()
    await db.refresh(c)
    await write_audit(db, user=current, action="create", target_type="material_category", target_id=c.id)
    return schemas.MaterialCategoryOut.model_validate(c)


@router.put("/material-categories/{cid}", response_model=schemas.MaterialCategoryOut)
async def update_material_category(
    cid: int, body: schemas.MaterialCategoryIn,
    current: models.User = Depends(require_roles(*_DICT_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """改段码只影响之后新发的编码，已发编码不追改（编码一经发出不变）。上级不可改。"""
    c = (await db.execute(select(models.MaterialCategory).where(
        models.MaterialCategory.id == cid))).scalar_one_or_none()
    if not c:
        raise HTTPException(404, "分类不存在")
    want = _SEG_LEN[c.level]
    if len(body.seg_code) != want:
        raise HTTPException(400, f"第{c.level}级段码须为 {want} 位数字")
    dup = await db.execute(select(models.MaterialCategory).where(
        models.MaterialCategory.parent_id == c.parent_id,
        models.MaterialCategory.seg_code == body.seg_code,
        models.MaterialCategory.id != cid))
    if dup.scalar_one_or_none():
        raise HTTPException(409, "同级下该段码已存在")
    c.seg_code = body.seg_code
    c.name = body.name.strip()
    c.sort_order = body.sort_order
    c.enabled = body.enabled
    await db.commit()
    await db.refresh(c)
    return schemas.MaterialCategoryOut.model_validate(c)


@router.delete("/material-categories/{cid}", response_model=schemas.Msg)
async def delete_material_category(
    cid: int,
    current: models.User = Depends(require_roles(*_DICT_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    child = (await db.execute(select(func.count(models.MaterialCategory.id)).where(
        models.MaterialCategory.parent_id == cid))).scalar() or 0
    if child:
        raise HTTPException(400, f"该分类下还有 {child} 个子分类，先删除/移走子分类")
    used = (await db.execute(select(func.count(models.WhMaterial.id)).where(
        models.WhMaterial.category_id == cid))).scalar() or 0
    if used:
        raise HTTPException(400, f"该分类已被 {used} 个物料使用，不能删除（可停用）")
    c = (await db.execute(select(models.MaterialCategory).where(
        models.MaterialCategory.id == cid))).scalar_one_or_none()
    if not c:
        raise HTTPException(404, "分类不存在")
    await db.delete(c)
    await db.commit()
    return schemas.Msg(message="已删除")


async def _gen_material_code(db: AsyncSession, category_id: int) -> str:
    """按细分类叶子生成物料编码：前缀=大类+中类+细分段码；流水=同前缀现有最大+1(4位)。"""
    cat = (await db.execute(select(models.MaterialCategory).where(
        models.MaterialCategory.id == category_id))).scalar_one_or_none()
    if not cat:
        raise HTTPException(404, "编码分类不存在")
    if cat.level != 3:
        raise HTTPException(400, "请选择到第三级（细分类）再发码")
    segs = [cat.seg_code]
    node = cat
    while node.parent_id:
        node = (await db.execute(select(models.MaterialCategory).where(
            models.MaterialCategory.id == node.parent_id))).scalar_one()
        segs.append(node.seg_code)
    prefix = "".join(reversed(segs))
    r = await db.execute(select(models.WhMaterial.code).where(
        models.WhMaterial.code.like(prefix + "%")))
    mx = 0
    for (c,) in r.all():
        tail = (c or "")[len(prefix):]
        if tail.isdigit():
            mx = max(mx, int(tail))
    return f"{prefix}{mx + 1:04d}"


# ==================== 🆕 字典维护（物料类别 / 计量单位 / 供应商分类 受管理取值）====================
# 同一张表(dtype 区分)、同一套 CRUD；三者取值语义各自独立，互不并入对方下拉。
# 维护：采购主管（admin/manager 由 require_roles 自动放行）；读取：所有登录用户
# （_DICT_ADMIN_ROLES 定义已前移到物料编码分类段之前——Depends 默认参数在 import 时求值，
#   定义在使用之后会让整个后端 NameError 起不来）


def _dict_ref(dtype: str):
    """字典取值被谁引用——用于改名级联 / 删除拦截。category/unit/material_grade 挂在物料上，
    supplier_category 是独立分类（不与物料类别混用，两边取值语义不同），挂在供应商上。"""
    if dtype == "category":
        return models.WhMaterial, models.WhMaterial.category, "category"
    if dtype == "unit":
        return models.WhMaterial, models.WhMaterial.unit, "unit"
    if dtype == "material_grade":
        return models.WhMaterial, models.WhMaterial.material_grade, "material_grade"
    if dtype == "supplier_category":
        return models.Supplier, models.Supplier.category, "category"
    return None, None, None


async def _dict_items(db: AsyncSession, dtype: Optional[str] = None, enabled_only: bool = False):
    q = select(models.MaterialDict)
    if dtype:
        q = q.where(models.MaterialDict.dtype == dtype)
    if enabled_only:
        q = q.where(models.MaterialDict.enabled == True)  # noqa: E712
    q = q.order_by(models.MaterialDict.dtype, models.MaterialDict.sort_order, models.MaterialDict.id)
    return list((await db.execute(q)).scalars().all())


@router.get("/material-dict", response_model=List[schemas.MaterialDictOut])
async def list_material_dict(
    dtype: Optional[str] = Query(None, description="category / unit / supplier_category / material_grade；空=全部"),
    enabled_only: bool = Query(False),
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """物料字典读取——物料表单渲染「类别 / 单位」下拉用（所有登录用户可读）。"""
    return [schemas.MaterialDictOut.model_validate(x) for x in await _dict_items(db, dtype, enabled_only)]


@router.post("/material-dict", response_model=schemas.MaterialDictOut)
async def create_material_dict(
    body: schemas.MaterialDictIn,
    current: models.User = Depends(require_roles(*_DICT_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    val = body.value.strip()
    if not val:
        raise HTTPException(400, "取值不能为空")
    dup = await db.execute(select(models.MaterialDict).where(
        models.MaterialDict.dtype == body.dtype, models.MaterialDict.value == val))
    if dup.scalar_one_or_none():
        raise HTTPException(409, "该取值已存在")
    it = models.MaterialDict(dtype=body.dtype, value=val,
                             sort_order=body.sort_order, enabled=body.enabled)
    db.add(it)
    await db.commit()
    await db.refresh(it)
    await write_audit(db, user=current, action="create", target_type="material_dict", target_id=it.id)
    return schemas.MaterialDictOut.model_validate(it)


@router.put("/material-dict/{did}", response_model=schemas.MaterialDictOut)
async def update_material_dict(
    did: int, body: schemas.MaterialDictIn,
    current: models.User = Depends(require_roles(*_DICT_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.MaterialDict).where(models.MaterialDict.id == did))
    it = r.scalar_one_or_none()
    if not it:
        raise HTTPException(404, "字典项不存在")
    val = body.value.strip()
    if not val:
        raise HTTPException(400, "取值不能为空")
    dup = await db.execute(select(models.MaterialDict).where(
        models.MaterialDict.dtype == body.dtype, models.MaterialDict.value == val,
        models.MaterialDict.id != did))
    if dup.scalar_one_or_none():
        raise HTTPException(409, "该取值已存在")
    old_val, old_dtype = it.value, it.dtype
    it.dtype = body.dtype
    it.value = val
    it.sort_order = body.sort_order
    it.enabled = body.enabled
    # 改名级联到存量引用方，避免旧值成孤儿、下次启动又被并入字典
    if old_dtype == body.dtype and val != old_val:
        model, col, field = _dict_ref(body.dtype)
        if model is not None:
            await db.execute(sa_update(model).where(col == old_val).values(**{field: val}))
    await db.commit()
    await db.refresh(it)
    return schemas.MaterialDictOut.model_validate(it)


@router.delete("/material-dict/{did}", response_model=schemas.Msg)
async def delete_material_dict(
    did: int,
    current: models.User = Depends(require_roles(*_DICT_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """删除字典项。若仍被物料使用则拦截（改用「停用」，避免下拉丢值/被重新并入）。"""
    r = await db.execute(select(models.MaterialDict).where(models.MaterialDict.id == did))
    it = r.scalar_one_or_none()
    if not it:
        raise HTTPException(404, "字典项不存在")
    model, col, _ = _dict_ref(it.dtype)
    if model is not None:
        used = await db.execute(select(func.count(model.id)).where(col == it.value))
        if used.scalar():
            raise HTTPException(400, "该取值已被使用，不能删除；可改为「停用」")
    await db.delete(it)
    await db.commit()
    await write_audit(db, user=current, action="delete", target_type="material_dict", target_id=did)
    return schemas.Msg(message="已删除该字典项")


# ==================== 🆕 需求十五：一键清空（试运行数据清理）====================
@router.post("/clear-all", response_model=schemas.Msg)
async def clear_all_warehouse(
    body: schemas.WhClearIn,
    current: models.User = Depends(require_roles("warehouse_lead")),
    db: AsyncSession = Depends(get_db),
):
    """仓库总监/管理层一键清空：清空全部出入库流水 + 物料主数据（试运行数据清理）。
    不动供应商/采购/项目/字典；高危操作需输入确认词「清空仓库」。"""
    if (body.confirm or "").strip() != "清空仓库":
        raise HTTPException(400, "请输入确认词「清空仓库」以确认此高危操作")
    txn_cnt = (await db.execute(select(func.count(models.WhTxn.id)))).scalar() or 0
    mat_cnt = (await db.execute(select(func.count(models.WhMaterial.id)))).scalar() or 0
    # 先删流水（wh_txns.material_id → wh_materials，且自引用 reversal_of），再删物料主数据
    await db.execute(sa_delete(models.WhTxn))
    await db.execute(sa_delete(models.WhMaterial))
    await db.commit()
    await write_audit(db, user=current, action="wh_clear_all", target_type="warehouse",
                      target_id=None, detail=f"清空流水 {txn_cnt} 条 + 物料 {mat_cnt} 种")
    return schemas.Msg(message=f"已清空：出入库流水 {txn_cnt} 条、物料主数据 {mat_cnt} 种")


# ==================== 出入库 ====================
async def _next_ref(db: AsyncSession, direction: str, biz_date: str) -> str:
    prefix = "RK" if direction == "in" else "CK"
    ymd = biz_date.replace("-", "")
    like = f"{prefix}{ymd}-%"
    r = await db.execute(select(func.count(models.WhTxn.id)).where(models.WhTxn.ref_no.like(like)))
    n = (r.scalar() or 0) + 1
    return f"{prefix}{ymd}-{n:03d}"


# ==================== 🆕 库位管理（仓库维护;采购下单/出入库流水共用取值） ====================
@router.get("/locations", response_model=List[schemas.WhLocationOut])
async def list_locations(
    enabled_only: bool = Query(False, description="True=只返回启用的(表单下拉用)"),
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """库位列表（所有登录用户可读——采购下单、物料表单的下拉取值）。"""
    q = select(models.WhLocation).order_by(models.WhLocation.sort_order, models.WhLocation.id)
    if enabled_only:
        q = q.where(models.WhLocation.enabled == True)  # noqa: E712
    locs = list((await db.execute(q)).scalars().all())
    # 在用物料数（删除保护提示）
    cnt = dict((await db.execute(
        select(models.WhMaterial.location, func.count(models.WhMaterial.id))
        .where(models.WhMaterial.location.isnot(None))
        .group_by(models.WhMaterial.location))).all())
    # 🆕 #204 占用/空闲：库位上有物料且现存>0 = 占用（跟着出入库流水的库存净值走）。
    stock = await _stock_map(db)
    occ: dict = {}
    for m in (await db.execute(select(models.WhMaterial).where(
            models.WhMaterial.location.isnot(None)))).scalars().all():
        st = stock.get(m.id, m.init_stock or 0)
        if m.location and st > 0:
            occ.setdefault(m.location, []).append(
                {"name": m.name, "spec": m.spec, "stock": st})
    out = []
    for l in locs:
        o = schemas.WhLocationOut.model_validate(l)
        o.mat_count = cnt.get(l.name, 0)
        items = occ.get(l.name, [])
        o.occupied = len(items) > 0
        o.occupied_items = items[:20]
        out.append(o)
    return out


@router.post("/locations", response_model=schemas.WhLocationOut)
async def create_location(
    body: schemas.WhLocationIn,
    current: models.User = Depends(require_roles(*WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "库位名称不能为空")
    ex = (await db.execute(select(models.WhLocation).where(
        models.WhLocation.name == name))).scalar_one_or_none()
    if ex:
        raise HTTPException(400, f"库位「{name}」已存在")
    l = models.WhLocation(name=name, note=(body.note or "").strip() or None,
                          sort_order=body.sort_order, enabled=body.enabled)
    db.add(l)
    await db.commit()
    await write_audit(db, user=current, action="wh_location_create", target_type="wh_location",
                      target_id=l.id, detail=name)
    return schemas.WhLocationOut.model_validate(l)


@router.put("/locations/{lid}", response_model=schemas.WhLocationOut)
async def update_location(
    lid: int, body: schemas.WhLocationIn,
    current: models.User = Depends(require_roles(*WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    l = (await db.execute(select(models.WhLocation).where(
        models.WhLocation.id == lid))).scalar_one_or_none()
    if not l:
        raise HTTPException(404, "库位不存在")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "库位名称不能为空")
    dup = (await db.execute(select(models.WhLocation).where(
        models.WhLocation.name == name, models.WhLocation.id != lid))).scalar_one_or_none()
    if dup:
        raise HTTPException(400, f"库位「{name}」已存在")
    old_name = l.name
    l.name, l.note = name, (body.note or "").strip() or None
    l.sort_order, l.enabled = body.sort_order, body.enabled
    # 改名级联：把挂在旧库位名下的物料/未收货采购单同步到新名（流水是历史快照,不改）
    if old_name != name:
        await db.execute(sa_update(models.WhMaterial).where(
            models.WhMaterial.location == old_name).values(location=name))
        await db.execute(sa_update(models.PurchaseItem).where(
            models.PurchaseItem.stock_location == old_name,
            models.PurchaseItem.arrival_date.is_(None)).values(stock_location=name))
    await db.commit()
    await write_audit(db, user=current, action="wh_location_update", target_type="wh_location",
                      target_id=l.id, detail=f"{old_name} → {name}")
    return schemas.WhLocationOut.model_validate(l)


@router.delete("/locations/{lid}", response_model=schemas.Msg)
async def delete_location(
    lid: int,
    current: models.User = Depends(require_roles(*WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    l = (await db.execute(select(models.WhLocation).where(
        models.WhLocation.id == lid))).scalar_one_or_none()
    if not l:
        raise HTTPException(404, "库位不存在")
    used_mat = (await db.execute(select(func.count(models.WhMaterial.id)).where(
        models.WhMaterial.location == l.name))).scalar() or 0
    used_po = (await db.execute(select(func.count(models.PurchaseItem.id)).where(
        models.PurchaseItem.stock_location == l.name,
        models.PurchaseItem.arrival_date.is_(None)))).scalar() or 0
    if used_mat or used_po:
        raise HTTPException(400, f"该库位仍有 {used_mat} 个物料 / {used_po} 条未收货采购在用，"
                                 f"请先转移或改用「停用」")
    name = l.name
    await db.delete(l)
    await db.commit()
    await write_audit(db, user=current, action="wh_location_delete", target_type="wh_location",
                      target_id=lid, detail=name)
    return schemas.Msg(message=f"已删除库位「{name}」")


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
    # 🆕 盈利改善1b·堵「无主领料」黑洞：出库必须挂项目，或明确勾「非项目领用」+原因——
    #   此前 project_id 选填，无主出库的材料钱在全系统蒸发（project-cost 直接丢弃）。
    src = (data.source or "").strip()
    party = (data.party or "").strip()
    if data.direction == "out" and not data.project_id:
        reason = (data.non_project_reason or "").strip()
        if not data.non_project or not reason:
            raise HTTPException(400, "出库必须选择领用项目；确属非项目领用请勾选「非项目领用」并填写原因")
        src = src or "非项目领用"
        party = (f"{party}〔非项目:{reason}〕" if party else f"非项目:{reason}")[:128]
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
    amount = round(data.qty * data.unit_price, 4) if data.unit_price is not None else None
    # 🆕 库位：入库=放到哪(选填,默认物料当前库位;填了回写物料当前库位)；出库=从物料当前库位领
    loc = (data.location or "").strip() or None
    if data.direction == "in":
        txn_loc = loc or m.location
        if loc:
            m.location = loc
    else:
        txn_loc = m.location
    txn = models.WhTxn(
        material_id=data.material_id, biz_date=bd, direction=data.direction, qty=data.qty,
        unit_price=data.unit_price, amount=amount,
        source=(src or ("采购入库" if data.direction == "in" else "领料出库")),
        party=party or None, project_id=data.project_id, location=txn_loc,
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
        unit_price=o.unit_price, amount=o.amount, location=o.location,
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
        biz_date=t.biz_date, direction=t.direction, qty=t.qty,
        unit_price=t.unit_price, amount=t.amount, source=t.source, party=t.party,
        project_id=t.project_id, project_code=pmap.get(t.project_id), location=t.location,
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


# ==================== 🆕 项目物料需求（清单→仓库）+ 库存金额 / 项目成本（→财务） ====================
async def _avg_price_map(db: AsyncSession) -> dict:
    """各物料入库加权平均单价 = Σ入库金额 / Σ入库数量（仅统计带金额的入库）。
    🆕 盈利改善1b·冲红口径：被冲红的原单(reversed=True)与冲红单本身都排除——
    此前只排除冲红单，被冲红的原入库仍计入，冲红越多加权价越歪。"""
    r = await db.execute(
        select(models.WhTxn.material_id,
               func.sum(models.WhTxn.amount), func.sum(models.WhTxn.qty))
        .where(models.WhTxn.direction == "in", models.WhTxn.amount.isnot(None),
               models.WhTxn.is_reversal == False,  # noqa: E712
               models.WhTxn.reversed == False)  # noqa: E712
        .group_by(models.WhTxn.material_id))
    out: dict = {}
    for mid, amt, qty in r.all():
        if qty:
            out[mid] = (amt or 0) / qty
    return out


def _dnum(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


async def _demand_rows(db: AsyncSession, project_id: int, *, stock=None, mats=None):
    """项目物料需求逐行（供 /demand 与 /demand-overview 复用）。
    stock/mats 可由调用方预先算好传入，避免 overview 逐项目重复扫全表。"""
    r = await db.execute(select(models.Datasheet).where(
        models.Datasheet.project_id == project_id, models.Datasheet.name == "标准件清单"))
    sheet = r.scalar_one_or_none()
    if not sheet:
        return []
    fr = await db.execute(select(models.Field).where(models.Field.datasheet_id == sheet.id))
    name2id = {f.name: str(f.id) for f in fr.scalars().all()}
    lr = await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.source_sheet_id == sheet.id))
    by_rec: dict = {}
    for pi in lr.scalars().all():
        by_rec.setdefault(pi.source_record_id, []).append(pi)
    if stock is None:
        stock = await _stock_map(db)
    if mats is None:
        mats = (await db.execute(select(models.WhMaterial))).scalars().all()
    mat_by_key = {(m.name, m.spec or None): m for m in mats}
    # 🆕 需求二：本项目各物料已领用出库数量（out 且 project_id=本项目，排除冲红）
    issued_map: dict[int, float] = defaultdict(float)
    ir = await db.execute(
        select(models.WhTxn.material_id, func.sum(models.WhTxn.qty))
        .where(models.WhTxn.direction == "out", models.WhTxn.project_id == project_id,
               models.WhTxn.is_reversal == False,  # noqa: E712
               models.WhTxn.reversed == False)  # noqa: E712
        .group_by(models.WhTxn.material_id))
    for mid, tot in ir.all():
        issued_map[mid] = tot or 0
    rr = await db.execute(select(models.Record).where(
        models.Record.datasheet_id == sheet.id).order_by(models.Record.sort_order, models.Record.id))
    out = []
    for rec in rr.scalars().all():
        v = rec.values or {}

        def gv(col):
            fid = name2id.get(col)
            x = v.get(fid) if fid else None
            if isinstance(x, list):
                x = "、".join(str(i) for i in x)
            return str(x).strip() if x not in (None, "") else None

        name = gv("项目")
        if not name:
            continue
        spec = gv("规格型号")
        demand = _dnum(gv("数量"))
        m = mat_by_key.get((name, spec or None))
        st = stock.get(m.id, 0) if m else 0
        suggest = max(0, (demand or 0) - st)
        pis = by_rec.get(rec.id, [])
        status = "未下单" if not pis else ("已到货" if all(p.arrival_date for p in pis) else "已下单")
        out.append(schemas.WarehouseDemandRow(
            item_name=name, spec=spec, material_id=(m.id if m else None),
            location=(m.location if m else None),
            demand_qty=demand, stock=st,
            suggest_purchase=suggest, purchase_status=status, in_stock=st > 0,
            issued_qty=(issued_map.get(m.id, 0) if m else 0)))
    return out


@router.get("/demand/{project_id}", response_model=List[schemas.WarehouseDemandRow])
async def project_demand(
    project_id: int,
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """项目物料需求（读「标准件清单」）：逐行显示 需求量 / 现有库存 / 建议采购量 / 采购状态。"""
    return await _demand_rows(db, project_id)


@router.get("/demand-overview", response_model=List[schemas.WarehouseDemandOverviewRow])
async def demand_overview(
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """🆕 #157：物料需求总览——直接列出「有标准件清单」的项目 + 待出库/已出库条数，
    免去先从下拉选项目。待出库=有货且仍有未领需求的物料行数；已出库=已领用过的物料行数。"""
    # 只取"有标准件清单"的项目，避免把无需求的项目也列出来
    sr = await db.execute(
        select(models.Datasheet.project_id).where(models.Datasheet.name == "标准件清单").distinct())
    sheet_pids = [p for (p,) in sr.all() if p is not None]
    if not sheet_pids:
        return []
    pr = await db.execute(
        select(models.Project).where(
            models.Project.id.in_(sheet_pids), models.Project.is_deleted == False)  # noqa: E712
        .order_by(models.Project.code.desc()))
    projects = pr.scalars().all()
    # 预算全局 stock / mats，避免逐项目重复扫全表
    stock = await _stock_map(db)
    mats = (await db.execute(select(models.WhMaterial))).scalars().all()
    out = []
    for p in projects:
        rows = await _demand_rows(db, p.id, stock=stock, mats=mats)
        if not rows:
            continue
        pending = sum(1 for r in rows if r.in_stock and (r.demand_qty or 0) - (r.issued_qty or 0) > 0)
        issued = sum(1 for r in rows if (r.issued_qty or 0) > 0)
        out.append(schemas.WarehouseDemandOverviewRow(
            project_id=p.id, code=p.code, name=p.name,
            total_lines=len(rows), pending_out=pending, issued_out=issued))
    return out


# ==================== 🆕 需求二：物料需求「一键领用出库」到项目 ====================
@router.post("/demand/{project_id}/issue", response_model=schemas.Msg)
async def issue_demand(
    project_id: int,
    body: schemas.DemandIssueIn,
    current: models.User = Depends(require_roles(*WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """按物料需求把有货的物料一键领用出库到本项目（自动登记出库、计入项目材料成本）。
    body.lines: [{material_id, qty}]；qty 超现存自动截断到现存，现存为 0 的跳过。"""
    pr = await db.execute(select(models.Project).where(
        models.Project.id == project_id, models.Project.is_deleted == False))  # noqa: E712
    p = pr.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")
    lines = [ln for ln in body.lines if ln.qty and ln.qty > 0 and ln.material_id]
    if not lines:
        raise HTTPException(400, "没有可领用的物料")
    bd = date.today().isoformat()
    mids = [ln.material_id for ln in lines]
    stock = await _stock_map(db, mids)
    mrows = {m.id: m for m in (await db.execute(
        select(models.WhMaterial).where(models.WhMaterial.id.in_(mids)))).scalars().all()}
    issued, skipped = 0, 0
    for ln in lines:
        m = mrows.get(ln.material_id)
        if not m:
            skipped += 1
            continue
        avail = stock.get(ln.material_id, m.init_stock or 0)
        take = min(ln.qty, avail)
        if take <= 0:
            skipped += 1
            continue
        ref = await _next_ref(db, "out", bd)
        up = m.unit_price
        db.add(models.WhTxn(
            material_id=m.id, biz_date=bd, direction="out", qty=take,
            unit_price=up, amount=(round(take * up, 4) if up is not None else None),
            source="领料出库", party=p.code, project_id=project_id, location=m.location,
            ref_no=ref, operator_id=current.id))
        issued += 1
    if not issued:
        raise HTTPException(400, "所选物料现存不足，无法领用出库")
    await db.commit()
    await write_audit(db, user=current, action="wh_issue_demand", target_type="project",
                      target_id=project_id, detail=f"领用出库 {issued} 项")
    msg = f"已领用出库 {issued} 项到 {p.code}"
    if skipped:
        msg += f"（{skipped} 项现存不足已跳过）"
    return schemas.Msg(message=msg)


@router.get("/inventory-value")
async def inventory_value(
    _: models.User = Depends(require_roles("finance", "finance_lead")),   # 🆕 权限统一:tab由二级菜单权限控
    db: AsyncSession = Depends(get_db),
):
    """库存金额：各物料 现存 × 入库加权平均单价，汇总总库存金额（仅管理层）。"""
    stock = await _stock_map(db)
    avg = await _avg_price_map(db)
    mats = (await db.execute(select(models.WhMaterial))).scalars().all()
    rows = []
    total = 0.0
    for m in mats:
        st = stock.get(m.id, 0)
        price = avg.get(m.id)
        val = round(st * price, 2) if price is not None else None
        if val:
            total += val
        rows.append({"material_id": m.id, "name": m.name, "spec": m.spec,
                     "unit": m.unit, "stock": st, "avg_price": price, "value": val})
    rows.sort(key=lambda x: (x["value"] or 0), reverse=True)
    return {"total_value": round(total, 2), "rows": rows}


@router.get("/project-cost")
async def project_cost(
    _: models.User = Depends(require_roles("finance", "finance_lead")),   # 🆕 权限统一:tab由二级菜单权限控
    db: AsyncSession = Depends(get_db),
):
    """项目材料成本：出库(领料)到各项目的数量 × 物料加权平均单价，按项目汇总（仅管理层）。"""
    avg = await _avg_price_map(db)
    r = await db.execute(
        select(models.WhTxn.project_id, models.WhTxn.material_id, func.sum(models.WhTxn.qty))
        .where(models.WhTxn.direction == "out", models.WhTxn.project_id.isnot(None),
               models.WhTxn.is_reversal == False,  # noqa: E712
               models.WhTxn.reversed == False)  # noqa: E712
        .group_by(models.WhTxn.project_id, models.WhTxn.material_id))
    by_proj: dict = defaultdict(float)
    for pid, mid, qty in r.all():
        price = avg.get(mid)
        if price:
            by_proj[pid] += (qty or 0) * price
    if not by_proj:
        return {"rows": []}
    pr = await db.execute(select(models.Project.id, models.Project.code, models.Project.name)
                          .where(models.Project.id.in_(list(by_proj.keys()))))
    pmap = {i: (c, n) for i, c, n in pr.all()}
    rows = [{"project_id": pid, "code": pmap.get(pid, ("", ""))[0],
             "name": pmap.get(pid, ("", ""))[1], "cost": round(cost, 2)}
            for pid, cost in by_proj.items()]
    rows.sort(key=lambda x: x["cost"], reverse=True)
    return {"rows": rows}


# ==================== 发货清单：设计推送 -> 仓库备货完成 -> 物流可见 ====================
@router.get("/ship-list/pending", response_model=List[schemas.ShipListPendingRow])
async def ship_list_pending(
    status: str = Query("requested", description="requested 待备货(默认) / ready 已备齐 / all 全部"),
    _: models.User = Depends(require_roles(*WRITE_ROLES, "admin", "manager")),
    db: AsyncSession = Depends(get_db),
):
    """🆕 发货清单目录：设计部推送的发货清单（含文件），仓库据此备货、点「已备齐」通知物流。
    status: requested=待备货 / ready=已备齐 / all=全部。仓库只看/下载/打印，不上传。"""
    stmt = select(models.Shipment)
    if status == "requested":
        stmt = stmt.where(models.Shipment.packlist_status == "requested")
    elif status == "ready":
        stmt = stmt.where(models.Shipment.packlist_status == "ready")
    else:  # all
        stmt = stmt.where(models.Shipment.packlist_status.in_(["requested", "ready"]))
    stmt = stmt.order_by(
        models.Shipment.packlist_ready_at.desc().nullsfirst()
        if status == "ready" else models.Shipment.packlist_requested_at.desc())
    rows = list((await db.execute(stmt)).scalars().all())
    if not rows:
        return []
    # 推送人 / 备货人 名称
    uids = {s.packlist_requested_by for s in rows if s.packlist_requested_by}
    uids |= {s.packlist_ready_by for s in rows if s.packlist_ready_by}
    names: dict[int, str] = {}
    if uids:
        ur = await db.execute(select(models.User).where(models.User.id.in_(uids)))
        names = {u.id: (u.full_name or u.username) for u in ur.scalars().all()}
    # 每个项目的发货清单文件（设计推送的附件），按项目分组
    pids = [s.project_id for s in rows]
    files_by_pid: dict[int, list] = {}
    ar = await db.execute(select(models.Attachment).where(
        models.Attachment.biz_type == "ship_list",
        models.Attachment.biz_id.in_(pids),
    ).order_by(models.Attachment.id.desc()))
    for a in ar.scalars().all():
        files_by_pid.setdefault(a.biz_id, []).append(schemas.AttachmentOut.model_validate(a))
    return [
        schemas.ShipListPendingRow(
            project_id=s.project_id, code=s.project.code, name=s.project.name,
            requested_at=s.packlist_requested_at,
            requested_by_name=names.get(s.packlist_requested_by),
            packlist_status=s.packlist_status,
            ready_at=s.packlist_ready_at,
            ready_by_name=names.get(s.packlist_ready_by),
            files=files_by_pid.get(s.project_id, []),
        )
        for s in rows
    ]


@router.post("/ship-list/{project_id}/ready", response_model=schemas.Msg)
async def ship_list_ready(
    project_id: int,
    current: models.User = Depends(require_roles(*WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """🆕 发货清单备货完成：仓库确认已按清单备好货，通知物流可安排发货。"""
    r = await db.execute(select(models.Shipment).where(models.Shipment.project_id == project_id))
    sh = r.scalar_one_or_none()
    if not sh:
        raise HTTPException(404, "该项目暂无发货单据")
    if sh.packlist_status == "ready":
        return schemas.Msg(message="该项目发货清单已是备货完成状态")
    sh.packlist_status = "ready"
    sh.packlist_ready_at = datetime.now(timezone.utc)
    sh.packlist_ready_by = current.id
    await db.commit()
    p = sh.project
    await push_message(db, to_role="logistics", kind="info",
                       text=f"【发货清单已备货】{p.code} {p.name} 仓库已备货完成，可安排发货。",
                       biz_type="project", biz_id=project_id)
    await write_audit(db, user=current, action="ship_list_ready", target_type="shipment",
                      target_id=sh.id)
    return schemas.Msg(message="已标记备货完成，已通知物流")


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


@router.get("/ship-list/{project_id}", response_model=List[schemas.AttachmentOut])
async def list_ship_lists(
    project_id: int,
    _: models.User = Depends(require_roles("warehouse", "warehouse_lead", "logistics", "admin", "manager")),
    db: AsyncSession = Depends(get_db),
):
    """🆕 #9 某项目历史发货清单列表（仓库/物流/管理层可查看，按上传时间倒序，最新在前）。"""
    r = await db.execute(select(models.Attachment).where(
        models.Attachment.biz_type == "ship_list",
        models.Attachment.biz_id == project_id,
    ).order_by(models.Attachment.id.desc()))
    return [schemas.AttachmentOut.model_validate(a) for a in r.scalars().all()]


@router.delete("/ship-list/item/{aid}", response_model=schemas.Msg)
async def delete_ship_list(
    aid: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """🆕 #9 删除某条发货清单（上传者本人或仓库主管/管理层）。删除后物流看板同步消失，可再传新清单以「更换」。"""
    r = await db.execute(select(models.Attachment).where(
        models.Attachment.id == aid, models.Attachment.biz_type == "ship_list"))
    a = r.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "发货清单不存在")
    if not (_can_write(current) or a.uploaded_by == current.id):
        raise HTTPException(403, "仅上传者本人或仓库主管/管理层可删除")
    name = a.name
    await delete_attachment_file(db, a)
    await db.commit()
    await write_audit(db, user=current, action="delete", target_type="ship_list",
                      target_id=aid, detail=name)
    return schemas.Msg(message="已删除该发货清单")
