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
from sqlalchemy import select, func, or_, update as sa_update, delete as sa_delete

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
    """各物料实时库存 = init + Σin − Σout（可选 upto=YYYY-MM-DD 含当日，用于期初/期末）。

    ⚠️ material_ids=None / 空 都表示**全部物料**（返回超集，调用方本来就只按 id 取）。
       要"全部"时请传 None，别把 863 个 id 拼成 IN 传进来——那样 Postgres 走不了
       纯聚合的快路径，asyncpg 还会按占位符个数逐个 prepare，预编译缓存形同虚设。"""
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


async def _category_path_map(db: AsyncSession) -> dict[int, str]:
    """🆕 编码文字说明:category_id → 「大类/中类/细分」名称路径。给每个物料编码一句人话解释。"""
    cats = {c.id: c for c in (await db.execute(select(models.MaterialCategory))).scalars().all()}
    out: dict[int, str] = {}
    for cid, c in cats.items():
        names: list[str] = []
        cur = c
        seen: set[int] = set()
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            names.append(cur.name)
            cur = cats.get(cur.parent_id) if cur.parent_id else None
        out[cid] = "/".join(reversed(names))
    return out


def _mat_out(m: models.WhMaterial, stock: float, cat_path: Optional[str] = None,
             proj: Optional[tuple] = None, is_proj_mat: bool = False) -> schemas.WhMaterialOut:
    up = m.unit_price
    pid, pcode, pcodes = proj if proj else (None, None, [])
    return schemas.WhMaterialOut(
        id=m.id, code=m.code, category_id=m.category_id, category_path=cat_path,
        name=m.name, spec=m.spec, category=m.category,
        material_grade=m.material_grade,
        unit=m.unit, unit_price=up, location=m.location, safety_stock=m.safety_stock or 0,
        init_stock=m.init_stock or 0, status=m.status, stock=stock,
        stock_value=round(stock * up, 2) if up is not None else None,  # 🆕 需求三：库存总价
        low=stock < (m.safety_stock or 0),
        custom_values=m.custom_values or {},
        project_id=pid, project_code=pcode,   # 🆕 出库反显关联项目（只在单一项目时给）
        project_codes=pcodes,                 # 🆕 全部项目编号：出库下拉据此标出"这是别人项目的料"
        is_project_material=is_proj_mat,      # 🆕 #373/#374 项目物料不进库存总览/库存金额
    )


async def _material_projects(db: AsyncSession, material_ids: Optional[list[int]]) -> dict[int, tuple]:
    """物料关联了哪些项目（取挂项目的入库流水）。返回 {material_id: (project_id, project_code, [全部编号])}。

    两个用途，别搞混：
      · **出库反显**（前两项）：只有"所有入库都指向同一个项目"时才给 project_id/project_code，
        多项目的给 None——反显要么准要么别反显，猜一个填进去比不填更糟。
      · **出库下拉的标识**（第三项）：把**全部**项目编号都带上。
        🆕 反馈 2026-08-19：出库下拉里项目料本来带【项目编号】，但只有单一项目的才显示；
        生产上 399 个有货的项目料里 **135 个是多项目收过货的**，一个标签都没有，
        跟公司备货长得一模一样——仓库看不出这是别人项目上的料。

    material_ids=None 表示**全部物料**（不拼 IN，见 list_materials 的说明）；
    传空列表则是"一个都不要"，直接返回空。"""
    if material_ids is not None and not material_ids:
        return {}
    q = (select(models.WhTxn.material_id, models.WhTxn.project_id)
         .where(models.WhTxn.direction == "in",
                models.WhTxn.is_reversal == False,  # noqa: E712
                models.WhTxn.project_id.isnot(None)).distinct())
    if material_ids is not None:
        q = q.where(models.WhTxn.material_id.in_(material_ids))
    rows = (await db.execute(q)).all()
    by_mat: dict[int, set] = {}
    for mid, pid in rows:
        by_mat.setdefault(mid, set()).add(pid)
    if not by_mat:
        return {}
    all_pids = {p for pids in by_mat.values() for p in pids}
    pr = (await db.execute(select(models.Project.id, models.Project.code)
          .where(models.Project.id.in_(all_pids)))).all()
    code_by_pid = {i: c for i, c in pr}
    out: dict[int, tuple] = {}
    for mid, pids in by_mat.items():
        codes = sorted(c for c in (code_by_pid.get(p) for p in pids) if c)
        if len(pids) == 1:
            pid = next(iter(pids))
            out[mid] = (pid, code_by_pid.get(pid), codes)
        else:
            out[mid] = (None, None, codes)   # 多项目：不反显，但编号全带上供标识用
    return out


# ==================== 物料主数据 ====================
@router.get("/materials", response_model=schemas.WhStockOut)
async def list_materials(
    kw: Optional[str] = Query(None, description="名称/规格/编码/单位/库位，任一命中"),
    location: Optional[str] = Query(None, description="按库位精确筛"),
    low_only: bool = Query(False, description="只看低于安全库存的"),
    scope: str = Query("all", description="all 全部(默认) / general 只看通用物料 / project 只看项目物料"),
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """物料 + 实时库存（全员可读=查库存集成点；写操作另校验）。

    🆕 仓库反馈：551 个物料，只能按名称/规格/编码找，找一个"放在 A-03 的密封圈"
       还得自己一页页翻。搜索面扩到库位和单位，另加库位精确筛 + 只看缺料。
    ⚠️ 编码(code)全库只有 6/551 有值，所以**不能只按编码搜**——按编码搜等于搜不到。

    🆕 反馈#373/#374：每个物料带 `is_project_material`（判据见 `_project_material_ids`），
       「库存总览」用它只显示通用物料——买给具体项目的料归「项目物料需求总览」管，
       混在库存总览里既看不出真正的公司备货，也让人以为那些料还能随便领。
    ⚠️ scope 默认 **all**，不是 general。这个接口同时喂着「物料主数据」和出库选料，
       默认改成 general 会让项目物料在主数据里凭空消失、出库时搜不到。
       过滤是前端按 is_project_material 做的（一次请求分两个 tab 用），
       scope 只留给需要服务端过滤的调用方。
    """
    r = await db.execute(select(models.WhMaterial).order_by(models.WhMaterial.id))
    mats = list(r.scalars().all())
    n_all = len(mats)
    pm = await _project_material_ids(db)
    if scope in ("general", "project"):
        mats = [m for m in mats if (m.id in pm) == (scope == "project")]
    if kw:
        k = kw.strip().lower()
        def _hit(m):
            return any(k in (getattr(m, f, None) or "").lower()
                       for f in ("name", "spec", "code", "unit", "location", "material_grade"))
        mats = [m for m in mats if _hit(m)]
    if location:
        loc = location.strip()
        mats = [m for m in mats if (m.location or "") == loc]
    # ⚠️ 没筛掉任何东西时传 None（=全部），不要拼 863 个 id 的 IN 列表：
    #    既让 Postgres 走不了纯聚合的快路径，asyncpg 还会按占位符个数逐个 prepare
    #    ——每换一次筛选条件就是一条新语句，预编译缓存基本等于没有。
    mids = None if len(mats) == n_all else [m.id for m in mats]
    stock = await _stock_map(db, mids)
    cat_paths = await _category_path_map(db)   # 🆕 编码文字说明
    proj_by_mat = await _material_projects(db, mids)   # 🆕 出库反显关联项目
    outs = [_mat_out(m, stock.get(m.id, m.init_stock or 0), cat_paths.get(m.category_id),
                     proj_by_mat.get(m.id), m.id in pm) for m in mats]
    # ⚠️ low_only 必须在算完实时库存之后过滤——low 是 stock 与 safety_stock 比出来的，
    #    在取物料那一步过滤不到。
    if low_only:
        outs = [o for o in outs if o.low]
    return schemas.WhStockOut(materials=outs, total=len(outs),
                              low_count=sum(1 for o in outs if o.low))


@router.get("/materials/suggest", response_model=List[schemas.WhMaterialSuggestOut])
async def suggest_materials(
    q: Optional[str] = Query(None),
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """🆕 #278/#289 物料联想：按关键字模糊匹配物料主数据里已建档的物料，
    返回 name+spec（最多 20 条，前缀命中的排前面）。权限同物料主数据（登录即可读）。
    用途：采购下单/采购申请/项目详单的「名称」「规格型号」列 el-autocomplete，选中互相带出。

    🆕 反馈#411（李新新）：**名称和规格都要能搜**。原来只按名称匹配，她在「规格型号」列里
    敲「120F」什么也搜不出来，只能凭记忆手打——手打出的名称/规格和仓库里的对不上，
    收货时就按新料建档，这正是 #410 王利利说的重复物料的来源
    （线上：采购明细 1962 条里有 200 条的「名称+规格」在物料主数据里对不上）。"""
    k = (q or "").strip()
    if not k:
        return []
    r = await db.execute(select(models.WhMaterial).where(or_(
        models.WhMaterial.name.ilike(f"%{k}%"),
        models.WhMaterial.spec.ilike(f"%{k}%"))))
    mats = list(r.scalars().all())
    # 名称前缀命中的最靠前，其次规格前缀命中的，再按名称/规格排
    def _rank(m):
        nm, sp = m.name or "", m.spec or ""
        return (0 if nm.startswith(k) else (1 if sp.startswith(k) else 2), nm, sp, m.id)
    mats.sort(key=_rank)
    return [schemas.WhMaterialSuggestOut(name=m.name, spec=m.spec) for m in mats[:20]]


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
# 🆕 字典/编码分类（物料类别/单位/材质/供应商分类/订单编号）已放开给所有登录用户维护——
#   增删改端点均用 get_current_user（原限 buyer_lead+管理层，用户要求全员可编辑）。
#   仍需登录；未登录无法访问。前端菜单也已对所有人可见（MainLayout）。


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
    current: models.User = Depends(get_current_user),   # 🆕 字典设置放开给所有登录用户(增删改)
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
    current: models.User = Depends(get_current_user),   # 🆕 字典设置放开给所有登录用户(增删改)
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
    current: models.User = Depends(get_current_user),   # 🆕 字典设置放开给所有登录用户(增删改)
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
# 🆕 维护与读取均放开给所有登录用户（用户要求字典设置全员可用）。


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
    current: models.User = Depends(get_current_user),   # 🆕 字典设置放开给所有登录用户(增删改)
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
    current: models.User = Depends(get_current_user),   # 🆕 字典设置放开给所有登录用户(增删改)
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
    current: models.User = Depends(get_current_user),   # 🆕 字典设置放开给所有登录用户(增删改)
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


@router.post("/txns/batch-out", response_model=schemas.Msg)
async def batch_out_txns(
    data: schemas.WhBatchOutIn,
    current: models.User = Depends(require_roles(*WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """🆕 #325 批量出库（消耗品一次出多种物料）：共用 业务日期/用途/领用方/领用项目，
    每行生成一条出库流水。校验与超库存拦截口径同单条出库——先全量校验（不落库），
    任一行失败整体报错并指明第几行；全部通过再一次 commit（同事务）。
    单价随物料参考单价自动算金额（同 demand/issue 领用口径）。"""
    # 非项目领用口径与单条一致：出库必须挂项目，或明确勾「非项目领用」+原因
    src = (data.source or "").strip()
    party = (data.party or "").strip()
    if not data.project_id:
        reason = (data.non_project_reason or "").strip()
        if not data.non_project or not reason:
            raise HTTPException(400, "出库必须选择领用项目；确属非项目领用请勾选「非项目领用」并填写原因")
        src = src or "非项目领用"
        party = (f"{party}〔非项目:{reason}〕" if party else f"非项目:{reason}")[:128]
    bd = normalize_date_str(data.biz_date) or date.today().isoformat()
    # ---- 全量校验（不落库）：物料存在 / 数量>0 / 累计出库不超现存（同物料多行按剩余量递减校验）
    mids = [ln.material_id for ln in data.lines]
    mrows = {m.id: m for m in (await db.execute(
        select(models.WhMaterial).where(models.WhMaterial.id.in_(mids)))).scalars().all()}
    remain = await _stock_map(db, mids)
    for i, ln in enumerate(data.lines, 1):
        m = mrows.get(ln.material_id)
        label = f"第{i}行" + (f"「{m.name}{('·' + m.spec) if m.spec else ''}」" if m else "")
        if not m:
            raise HTTPException(400, f"{label}物料不存在")
        if not ln.qty or ln.qty <= 0:
            raise HTTPException(400, f"{label}数量必须为正数")
        stock = remain.get(ln.material_id, m.init_stock or 0)
        if ln.qty > stock:
            raise HTTPException(400, f"{label}出库数量 {ln.qty} 超过现存 {stock}")
        remain[ln.material_id] = stock - ln.qty
    # ---- 全部通过，统一落库（一次 commit）
    refs: list[str] = []
    for ln in data.lines:
        m = mrows[ln.material_id]
        ref = await _next_ref(db, "out", bd)
        up = m.unit_price
        db.add(models.WhTxn(
            material_id=m.id, biz_date=bd, direction="out", qty=ln.qty,
            unit_price=up, amount=(round(ln.qty * up, 4) if up is not None else None),
            source=src or "领料出库", party=party or None, project_id=data.project_id,
            location=m.location, ref_no=ref, operator_id=current.id))
        refs.append(ref)
    await db.commit()
    # 低库存预警（口径同单条：出库后现存低于安全库存推 warehouse_lead）
    stock = await _stock_map(db, mids)
    for m in mrows.values():
        cur = stock.get(m.id, 0)
        if cur < (m.safety_stock or 0):
            await push_message(db, to_role="warehouse_lead", kind="warn",
                               text=f"【低库存预警】{m.name}{('·'+m.spec) if m.spec else ''} 现存 {cur} 低于安全库存 {m.safety_stock}",
                               biz_type="wh_material", biz_id=m.id)
    await write_audit(db, user=current, action="wh_batch_out", target_type="wh_txn",
                      target_id=None, detail=f"批量出库 {len(refs)} 项：{refs[0]}~{refs[-1]}")
    return schemas.Msg(message=f"已批量出库 {len(refs)} 项（{refs[0]} ~ {refs[-1]}）")


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


@router.get("/txns", response_model=schemas.WhTxnListOut)
async def list_txns(
    direction: Optional[str] = Query(None),
    material_id: Optional[int] = Query(None),
    kw: Optional[str] = Query(None, description="单号/物料/规格/库位/来源/往来单位/项目编号"),
    date_from: Optional[str] = Query(None, description="业务日期 >= YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="业务日期 <= YYYY-MM-DD"),
    limit: int = Query(200, ge=1, le=1000),
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """出入库流水。

    ⚠️ **搜索必须在服务端做。** 原来只回最近 `limit` 条、由前端在这堆里过滤——
       生产上流水已经 1083 条，前端能搜到的最早只到昨天，
       仓库入完料第二天就搜不着了（正是 2026-08-06 报上来的问题）。
       现在 kw/日期都进 SQL，先筛后截断；并回 total 让前端知道有没有被截断。
    """
    base = select(models.WhTxn)
    if direction in ("in", "out"):
        base = base.where(models.WhTxn.direction == direction)
    if material_id:
        base = base.where(models.WhTxn.material_id == material_id)
    if date_from:
        base = base.where(models.WhTxn.biz_date >= date_from)
    if date_to:
        base = base.where(models.WhTxn.biz_date <= date_to)
    if kw and kw.strip():
        k = f"%{kw.strip()}%"
        # 物料名/规格在关联表上，项目编号在 projects 上——都要 join 进来搜，
        # 否则"搜密封圈"「搜 2026-071」这种最常用的搜法直接落空。
        mat_ids = select(models.WhMaterial.id).where(
            or_(models.WhMaterial.name.ilike(k), models.WhMaterial.spec.ilike(k)))
        proj_ids = select(models.Project.id).where(models.Project.code.ilike(k))
        base = base.where(or_(
            models.WhTxn.ref_no.ilike(k),
            models.WhTxn.source.ilike(k),
            models.WhTxn.party.ilike(k),
            models.WhTxn.location.ilike(k),
            models.WhTxn.biz_date.ilike(k),
            models.WhTxn.material_id.in_(mat_ids),
            models.WhTxn.project_id.in_(proj_ids),
        ))
    total = (await db.execute(
        select(func.count()).select_from(base.subquery()))).scalar() or 0
    r = await db.execute(base.order_by(models.WhTxn.id.desc()).limit(limit))
    txns = list(r.scalars().all())
    # 项目编号
    pids = {t.project_id for t in txns if t.project_id}
    pmap: dict[int, str] = {}
    if pids:
        r = await db.execute(select(models.Project.id, models.Project.code).where(models.Project.id.in_(pids)))
        pmap = dict(r.all())
    rows = [schemas.WhTxnOut(
        id=t.id, material_id=t.material_id,
        material_name=t.material.name if t.material else "", spec=t.material.spec if t.material else None,
        biz_date=t.biz_date, direction=t.direction, qty=t.qty,
        unit_price=t.unit_price, amount=t.amount, source=t.source, party=t.party,
        project_id=t.project_id, project_code=pmap.get(t.project_id), location=t.location,
        ref_no=t.ref_no, is_reversal=t.is_reversal, reversed=t.reversed, created_at=t.created_at,
    ) for t in txns]
    return schemas.WhTxnListOut(rows=rows, total=total, shown=len(rows))



@router.get("/po-items", response_model=List[schemas.WhPoItemOut])
async def po_items(
    po_no: Optional[str] = Query(None, description="采购单号，模糊匹配"),
    limit: int = Query(50, ge=1, le=200),
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """🆕 反馈 2026-08-07（杨坛）：「出库登记这里加一个功能，可搜索采购单号，
    然后选择里面的物料进行出库」。

    仓库出库时手头拿的是一张采购单，而登记表单只能从 551 个物料里翻着找——
    这里按单号把该单的物料行带出来，直接勾选出库。

    ⚠️ 只返回**已到货**(arrival_date 非空)的行：没到货的东西出不了库，
       列出来只会让人误选。
    ⚠️ 物料要匹配到 wh_materials 才能出库（出库扣的是物料库存，不是采购行），
       匹配不上的行照样返回但标 material_id=None，前端要禁选并说明原因，
       否则仓库会以为系统坏了。
    """
    q = select(models.PurchaseItem).where(models.PurchaseItem.arrival_date.isnot(None),
                                          models.PurchaseItem.arrival_date != "")
    if po_no and po_no.strip():
        q = q.where(models.PurchaseItem.po_no.ilike(f"%{po_no.strip()}%"))
    rows = list((await db.execute(
        q.order_by(models.PurchaseItem.id.desc()).limit(limit))).scalars().all())

    # 按 名称+规格 匹配物料主数据（出库扣的是它的库存）
    mats = list((await db.execute(select(models.WhMaterial))).scalars().all())
    by_key = {}
    for m in mats:
        by_key[((m.name or "").strip(), (m.spec or "").strip())] = m
    stock = await _stock_map(db, None)   # mats 就是全部物料，不拼 IN

    out = []
    for it in rows:
        key = ((it.item_name or "").strip(), (it.spec or "").strip())
        m = by_key.get(key) or by_key.get((key[0], ""))
        out.append(schemas.WhPoItemOut(
            id=it.id, po_no=it.po_no, item_name=it.item_name, spec=it.spec,
            qty=it.qty, arrival_date=it.arrival_date, project_code=it.project_code,
            stock_location=getattr(it, "stock_location", None),
            material_id=(m.id if m else None),
            stock=(stock.get(m.id, m.init_stock or 0) if m else 0),
            unmatched_reason=None if m else "物料主数据里没有同名同规格的料，出库前先在「物料主数据」建档",
        ))
    return out


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
    # 期初 = 月初前的实时库存（upto = start 前一天）。mats 就是全部物料，
    # 不用把 863 个 id 拼成 IN 传回去（理由同 list_materials）。
    before = await _stock_map(db, None, upto=_minus1(start))

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


async def _project_material_ids(db: AsyncSession) -> set[int]:
    """🆕 反馈#373/#374/#388「项目物料 vs 通用物料」二分法的唯一判据。

    **项目物料** = 有过任何一笔"挂了项目编号的有效入库"的物料。
    这批料是采购替某个项目专门买的，钱一到货就该算那个项目的成本，
    不该再躺在「库存金额」里当公司资产 —— 生产现场 2026-08-12 的实际比例：
    库存金额 ¥148,099 里有 ¥116,718(79%) 其实是这种已经名花有主的料。

    **口径保证（改这里之前先看懂）：每一笔入库金额有且只有一个去处，不重算也不漏。**
      · 挂了项目的入库        → 项目材料成本(收货腿)
      · 没挂项目 + 通用物料    → 留在库存金额；被领料出库时才转成那个项目的成本(领料腿)
      · 没挂项目 + 项目物料    → 归「未归集」，在接口里单列出来（生产现存 ¥2,729/40 笔，
                                 都是同一个料既有项目采购又有零星通用采购）。这笔钱不藏，
                                 藏起来两页数字就永远对不上，用户第一眼就会发现。

    为什么按**物料**整体分而不是按数量拆：拆数量会出现"A 项目收的料被 B 项目领走"，
    A 的收货腿和 B 的领料腿会把同一批物理料算两遍 —— 生产现存这种重算 ¥34,771，
    占超领总额的 99%。按物料整体归属就没有这个口子。
    """
    r = await db.execute(
        select(models.WhTxn.material_id).where(
            models.WhTxn.direction == "in",
            models.WhTxn.project_id.isnot(None),
            models.WhTxn.is_reversal == False,  # noqa: E712
            models.WhTxn.reversed == False).distinct())  # noqa: E712
    return {mid for (mid,) in r.all() if mid is not None}


async def _project_cost_core(db: AsyncSession, material_ids: Optional[list[int]] = None):
    """🆕 反馈#373 新口径「项目材料成本」的**唯一实现**——收货即计成本，不再等领料出库。

    旧口径只认「领料出库 × 加权均价」。现场的实情是：料到货就直接拉到工位用了，
    仓库的领料手续常常补不上 —— 2026-08-12 生产数据：收货 ¥421,444，
    旧口径只认出 ¥163,697，六成的钱在系统里蒸发，项目毛利全是假的。

    ── 两条腿（每一笔入库金额只落一处，不重算不漏）──────────────────
      腿A 收货  Σ 挂本项目编号的入库金额（金额缺失时退回 qty × 加权均价）
      腿B 领料  项目领得比自己收得多的部分（超领），**从通用池里按量扣**，扣多少算多少

    腿B 为什么要卡通用池，不能见到出库就算：
      · 不卡的话，「A 项目收了 100 → B 项目领走 30」会让 A 的收货腿和 B 的领料腿
        把同一批物理料算两遍。生产上这种重算 ¥34,771，占超领总额的 99%。
      · 但也不能因为"这个物料是项目物料"就一刀切把领料全不算 —— 那样
        「通用螺栓领 120 给甲项目，剩下 380 调给乙项目」会把甲项目已经发生的
        ¥144 成本倒扣掉。#377 调拨上线当天就会撞见（本地实测 13480 → 13336）。
      · 卡通用池两头都对：甲的 120 从通用池里扣得出来 → 照算；
        B 领 A 的料时通用池是 0 → 不算，钱留在 A 那边。

    通用池 = 期初 + 无项目入库 − 无项目出库（无项目出库含 #377 调拨的转出腿、无主领料）。
    多个项目同时超领而池子不够时按 project_id 升序先到先得——纯粹为了结果稳定可复现。

    返回 (rows, unassigned_by_material)：
      rows  逐 (project_id, material_id) 的成本行，带 leg/qty/amount，聚合与展开明细共用
            **同一份数据**，所以外面的合计和展开的逐行加总天然对得上
      unassigned  项目物料上没归到任何项目的通用池余额。这笔钱必须报出来：
                  藏掉的话「入库总额 = 项目成本 + 库存金额 + 未归集」永远配不平。

    material_ids 只在展开单个项目的明细时传（#389/#390），把扫描面收窄到那个项目
    碰过的物料——不传就是全量，总榜用。
    """
    avg = await _avg_price_map(db)
    pm = await _project_material_ids(db)

    def _flt(q):
        return q.where(models.WhTxn.material_id.in_(material_ids)) if material_ids else q

    # ── 腿A：挂项目的入库，直接取流水金额（与采购明细收货金额同一个数，见 _sync_txn_amount）
    rows: list[dict] = []
    in_by_pm: dict[tuple, float] = defaultdict(float)   # (pid, mid) → 收货数量，腿B 判超领要用
    r = await db.execute(_flt(
        select(models.WhTxn.project_id, models.WhTxn.material_id,
               func.sum(models.WhTxn.qty), func.sum(models.WhTxn.amount))
        .where(models.WhTxn.direction == "in", models.WhTxn.project_id.isnot(None),
               models.WhTxn.is_reversal == False,  # noqa: E712
               models.WhTxn.reversed == False))  # noqa: E712
        .group_by(models.WhTxn.project_id, models.WhTxn.material_id))
    for pid, mid, qty, amt in r.all():
        in_by_pm[(pid, mid)] += qty or 0
        price = avg.get(mid)
        if amt is None and price is not None:
            amt = (qty or 0) * price
        rows.append({"project_id": pid, "material_id": mid, "leg": "收货",
                     "qty": qty or 0, "avg_price": price, "amount": amt})

    # ── 通用池：期初 + 无项目入库 − 无项目出库
    pool: dict[int, float] = defaultdict(float)
    mq = select(models.WhMaterial.id, models.WhMaterial.init_stock)
    if material_ids:
        mq = mq.where(models.WhMaterial.id.in_(material_ids))
    for mid, init in (await db.execute(mq)).all():
        pool[mid] += init or 0
    r = await db.execute(_flt(
        select(models.WhTxn.material_id, models.WhTxn.direction, func.sum(models.WhTxn.qty))
        .where(models.WhTxn.project_id.is_(None),
               models.WhTxn.is_reversal == False,  # noqa: E712
               models.WhTxn.reversed == False))  # noqa: E712
        .group_by(models.WhTxn.material_id, models.WhTxn.direction))
    for mid, direction, qty in r.all():
        pool[mid] += (qty or 0) if direction == "in" else -(qty or 0)

    # ── 腿B：超领的部分从通用池扣（project_id 升序，先到先得）
    r = await db.execute(_flt(
        select(models.WhTxn.project_id, models.WhTxn.material_id, func.sum(models.WhTxn.qty))
        .where(models.WhTxn.direction == "out", models.WhTxn.project_id.isnot(None),
               models.WhTxn.is_reversal == False,  # noqa: E712
               models.WhTxn.reversed == False))  # noqa: E712
        .group_by(models.WhTxn.project_id, models.WhTxn.material_id)
        .order_by(models.WhTxn.material_id, models.WhTxn.project_id))
    for pid, mid, qty in r.all():
        over = (qty or 0) - in_by_pm.get((pid, mid), 0)
        take = min(over, max(0.0, pool.get(mid, 0)))
        if take <= 0:
            continue
        pool[mid] -= take
        price = avg.get(mid)
        rows.append({"project_id": pid, "material_id": mid, "leg": "领料",
                     "qty": take, "avg_price": price,
                     "amount": (take * price) if price is not None else None})

    # 未归集：项目物料上剩下的通用池（通用物料的余额是「库存金额」，不算未归集）
    unassigned = {mid: bal * avg[mid] for mid, bal in pool.items()
                  if mid in pm and bal > 0 and avg.get(mid) is not None}
    return rows, unassigned


async def _project_cost_map(db: AsyncSession) -> tuple[dict, dict]:
    """项目材料成本按项目汇总。口径与实现见 `_project_cost_core`。"""
    rows, unassigned = await _project_cost_core(db)
    by_proj: dict = defaultdict(float)
    noprice: dict = defaultdict(int)
    for r in rows:
        if r["amount"] is None:
            if r["qty"]:
                noprice[r["project_id"]] += 1
        else:
            by_proj[r["project_id"]] += r["amount"]
    return by_proj, {
        "unassigned": round(sum(unassigned.values()), 2),
        "noprice_by_project": dict(noprice),
        "note": "口径：挂项目编号的收货入库金额 + 超领部分从通用库存扣（收货即计成本，不等领料）",
    }


def _dnum(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _demand_sheet_cols() -> dict:
    """物料需求「清单需求」纳入的清单 → {表名: (名称列, 规格列, 数量列)}。

    用户 2026-07-16 确认：只纳入「走库存的 3 张材料清单」——标准件清单 / 电工采购单 /
    不锈钢原料下料单（都有数量列、是真正入库的材料）；外协加工 / 激光件清单是定制直发件，
    不算仓库物料需求，不列。列名各表不同，直接复用采购模块 `_PURCHASABLE_SHEETS`(唯一权威,
    避免二次硬编码)。惰性 import 规避与采购路由的循环依赖。"""
    from .purchase_mgmt_router import _PURCHASABLE_SHEETS
    return {
        _PURCHASABLE_SHEETS[k][0]: (_PURCHASABLE_SHEETS[k][1], _PURCHASABLE_SHEETS[k][2], _PURCHASABLE_SHEETS[k][3])
        for k in ("standard", "elec_po", "material")
    }


class _DemandCtx:
    """物料需求的一次性批量预取。**只为性能存在，口径必须跟逐项目算法逐字节一样。**

    原来 /demand-overview 对每个项目单独跑一遍 _demand_rows：每项目 2 条流水聚合 +
    1 条清单查询，再对每张清单查 字段/记录/采购项 各一条。生产实测 107 个项目、
    321 张清单 = **1333 条 SQL / 1.0 秒**，且随项目数线性膨胀（项目只会越来越多）。
    这里把同样的数据一次性按项目批量取回，**固定 9 条 SQL，与项目数无关**。

    ⚠️ 改这里要盯住的两个"最后一个赢"字典：`mat_by_key`(同名同规格的物料)
       和 `name2id`(同名字段)。逐项目版本靠数据库返回顺序决定谁赢，批量版本显式
       按 id 排序来定，这样两台机器/两次请求结果一致——本来就不该靠运气。
    """
    __slots__ = ("stock", "mat_by_key", "mat_by_id", "sheet_cols",
                 "issued", "recv", "sheets_by_pid", "name2id", "pi_by_rec", "recs_by_sheet")


async def _demand_ctx(db: AsyncSession, project_ids: list[int]) -> _DemandCtx:
    """一次性备齐 project_ids 这批项目算物料需求要用的全部数据（9 条 SQL）。"""
    ctx = _DemandCtx()
    ctx.sheet_cols = _demand_sheet_cols()          # {表名: (名称列, 规格列, 数量列)}
    ctx.stock = await _stock_map(db)               # 2 条
    mats = (await db.execute(select(models.WhMaterial)
                             .order_by(models.WhMaterial.id))).scalars().all()   # 1 条
    ctx.mat_by_key = {(m.name, m.spec or None): m for m in mats}
    ctx.mat_by_id = {m.id: m for m in mats}

    # 已领用出库(out) / 挂本项目的入库(in)：各 1 条按 (项目,物料) 聚合，原来是每项目各 1 条
    async def _agg(direction: str) -> dict[int, dict[int, float]]:
        if not project_ids:
            return {}
        r = await db.execute(
            select(models.WhTxn.project_id, models.WhTxn.material_id, func.sum(models.WhTxn.qty))
            .where(models.WhTxn.direction == direction,
                   models.WhTxn.project_id.in_(project_ids),
                   models.WhTxn.is_reversal == False,  # noqa: E712
                   models.WhTxn.reversed == False)  # noqa: E712
            .group_by(models.WhTxn.project_id, models.WhTxn.material_id))
        d: dict[int, dict[int, float]] = {}
        for pid, mid, tot in r.all():
            d.setdefault(pid, {})[mid] = tot or 0
        return d
    ctx.issued = await _agg("out")                 # 1 条
    ctx.recv = await _agg("in")                    # 1 条

    ctx.sheets_by_pid, ctx.name2id, ctx.pi_by_rec, ctx.recs_by_sheet = {}, {}, {}, {}
    if not project_ids:
        return ctx
    sheets = list((await db.execute(select(models.Datasheet).where(   # 1 条
        models.Datasheet.project_id.in_(project_ids),
        models.Datasheet.name.in_(list(ctx.sheet_cols.keys())))
        )).scalars().all())
    # ⚠️ 明细的行顺序 = 清单的遍历顺序，必须**定死**。原来这里没有 order by，靠数据库返回
    #    什么顺序就是什么顺序——同一个项目两次请求可能不一样（改过的行在 Postgres 里会挪位置），
    #    仓库看着列表顺序莫名其妙变。这里按 _demand_sheet_cols() 里写死的业务顺序排
    #    （标准件清单 → 电工采购单 → 不锈钢原料下料单），同名再按 id。
    #    ⚠️ 别改成按 id 排：电工采购单是老模板、id 通常比另外两张小，按 id 排会把它顶到最前面。
    _order = {nm: i for i, nm in enumerate(ctx.sheet_cols)}
    sheets.sort(key=lambda s: (_order.get(s.name, 99), s.id))
    for s in sheets:
        ctx.sheets_by_pid.setdefault(s.project_id, []).append(s)
    sids = [s.id for s in sheets]
    if not sids:
        return ctx
    ctx.name2id = {sid: {} for sid in sids}
    fr = await db.execute(select(models.Field).where(                 # 1 条
        models.Field.datasheet_id.in_(sids)).order_by(models.Field.id))
    for f in fr.scalars().all():
        ctx.name2id[f.datasheet_id][f.name] = str(f.id)
    lr = await db.execute(select(models.PurchaseItem).where(          # 1 条
        models.PurchaseItem.source_sheet_id.in_(sids)))
    for pi in lr.scalars().all():
        ctx.pi_by_rec.setdefault((pi.source_sheet_id, pi.source_record_id), []).append(pi)
    rr = await db.execute(select(models.Record).where(                # 1 条
        models.Record.datasheet_id.in_(sids))
        .order_by(models.Record.datasheet_id, models.Record.sort_order, models.Record.id))
    for rec in rr.scalars().all():
        ctx.recs_by_sheet.setdefault(rec.datasheet_id, []).append(rec)
    return ctx


def _demand_rows_from_ctx(ctx: _DemandCtx, project_id: int) -> list[schemas.WarehouseDemandRow]:
    """从预取好的 ctx 里算某个项目的物料需求逐行（纯内存，不再碰数据库）。

    两个来源合并（都可勾选领用出库到本项目）：
    ① 清单需求：读项目「走库存的 3 张材料清单」(标准件清单/电工采购单/不锈钢原料下料单)逐行
       (source="清单")——见 _demand_sheet_cols()；
    ② 采购单入库：经采购收货/入库登记**关联到本项目**(WhTxn.project_id) 但不在清单里的物料
       (source="采购")——解决"新建采购单的物料关联了项目号却无法在物料需求里汇总/出库"。"""
    stock = ctx.stock
    issued_map = ctx.issued.get(project_id, {})
    out: list[schemas.WarehouseDemandRow] = []
    bom_mat_ids: set[int] = set()

    # ---- 一、清单需求行（3 张材料清单：标准件清单/电工采购单/不锈钢原料下料单）----
    for sheet in ctx.sheets_by_pid.get(project_id, []):
        name_col, spec_col, qty_col = ctx.sheet_cols[sheet.name]
        name2id = ctx.name2id.get(sheet.id, {})
        for rec in ctx.recs_by_sheet.get(sheet.id, []):
            v = rec.values or {}

            def gv(col):
                fid = name2id.get(col)
                x = v.get(fid) if fid else None
                if isinstance(x, list):
                    x = "、".join(str(i) for i in x)
                return str(x).strip() if x not in (None, "") else None

            name = gv(name_col)
            if not name:
                continue
            spec = gv(spec_col)
            demand = _dnum(gv(qty_col)) if qty_col else None
            m = ctx.mat_by_key.get((name, spec or None))
            if m:
                bom_mat_ids.add(m.id)
            st = stock.get(m.id, 0) if m else 0
            # 🆕 反馈#393：建议采购要先扣掉**已经领用出库的量**。
            #   原来是 `需求 − 现存`：领完之后现存归 0，就又叫人再买一遍需求量，
            #   而这批料其实已经领到项目上用了。正确口径 = (还没领的需求) − 现存。
            issued_q = (issued_map.get(m.id, 0) if m else 0)
            remain = max(0, (demand or 0) - issued_q)
            suggest = max(0, remain - st)
            pis = ctx.pi_by_rec.get((sheet.id, rec.id), [])
            status = "未下单" if not pis else ("已到货" if all(p.arrival_date for p in pis) else "已下单")
            out.append(schemas.WarehouseDemandRow(
                item_name=name, spec=spec, material_id=(m.id if m else None),
                location=(m.location if m else None),
                demand_qty=demand, stock=st,
                suggest_purchase=suggest, purchase_status=status, in_stock=st > 0,
                issued_qty=issued_q, source="清单"))

    # ---- 二、采购单入库到本项目、但不在清单里的物料 ----
    for mid, got in ctx.recv.get(project_id, {}).items():
        if mid in bom_mat_ids:
            continue  # 清单已覆盖该物料，需求量以清单为准，不重复列
        m = ctx.mat_by_id.get(mid)
        if not m:
            continue
        st = stock.get(mid, 0)
        # 采购入库到本项目的量当作"需求量"，待出库 = 采购量 - 已领；已买到货，无需再采购
        out.append(schemas.WarehouseDemandRow(
            item_name=m.name, spec=m.spec, material_id=mid, location=m.location,
            demand_qty=got, stock=st, suggest_purchase=0,
            purchase_status="已到货", in_stock=st > 0,
            issued_qty=issued_map.get(mid, 0), source="采购"))
    return out


async def _demand_rows(db: AsyncSession, project_id: int, *, ctx: Optional[_DemandCtx] = None):
    """单个项目的物料需求逐行。ctx 由 /demand-overview 批量预取后传进来，单查时自己建。"""
    if ctx is None:
        ctx = await _demand_ctx(db, [project_id])
    return _demand_rows_from_ctx(ctx, project_id)


@router.get("/demand/{project_id}", response_model=List[schemas.WarehouseDemandRow])
async def project_demand(
    project_id: int,
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """项目物料需求：读 3 张材料清单(标准件清单/电工采购单/不锈钢原料下料单)+ 采购单入库到本项目的物料，
    逐行显示 需求量 / 现有库存 / 建议采购量 / 采购状态，来源列区分「清单」/「采购」。"""
    return await _demand_rows(db, project_id)


@router.get("/demand-overview", response_model=List[schemas.WarehouseDemandOverviewRow])
async def demand_overview(
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """🆕 #157：物料需求总览——直接列出有物料需求的项目 + 待出库/已出库条数，免去先从下拉选项目。
    待出库=有货且仍有未领需求的物料行数；已出库=已领用过的物料行数。
    🆕 项目来源两类并集：① 有 3 张材料清单(标准件清单/电工采购单/不锈钢原料下料单)任一的项目；
    ② 采购单入库**关联了项目号**的项目(哪怕没有清单)。"""
    pids: set[int] = set()
    sr = await db.execute(
        select(models.Datasheet.project_id).where(
            models.Datasheet.name.in_(list(_demand_sheet_cols().keys()))).distinct())
    pids |= {p for (p,) in sr.all() if p is not None}
    # 🆕 采购收货/入库登记关联到项目的入库(非冲红)：把这些项目也纳入总览
    pr2 = await db.execute(
        select(models.WhTxn.project_id).where(
            models.WhTxn.direction == "in", models.WhTxn.project_id.isnot(None),
            models.WhTxn.is_reversal == False).distinct())  # noqa: E712
    pids |= {p for (p,) in pr2.all() if p is not None}
    if not pids:
        return []
    pr = await db.execute(
        select(models.Project).where(
            models.Project.id.in_(pids), models.Project.is_deleted == False)  # noqa: E712
        .order_by(models.Project.code.desc()))
    projects = pr.scalars().all()
    # ⚠️ 一次性批量预取（见 _DemandCtx）。原来这里是 `for p in projects: await _demand_rows(...)`，
    #    生产 107 个项目跑出 1333 条 SQL / 1.0 秒；现在固定 9 条，与项目数无关。
    ctx = await _demand_ctx(db, [p.id for p in projects])
    out = []
    for p in projects:
        rows = _demand_rows_from_ctx(ctx, p.id)
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


@router.post("/transfer-to-project", response_model=schemas.Msg)
async def transfer_to_project(
    body: schemas.WhTransferToProjectIn,
    current: models.User = Depends(require_roles(*WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """🆕 反馈#377：库位上的存量物料 → 调到项目物料（中转）。

    场景：库里躺着的通用料，现在确定要给某个项目用了。以前只能等到出库时手填项目，
    在「项目物料需求总览」里根本看不到这批料，项目上的人不知道有货。

    实现是**两笔流水，不是改物料主数据**：
      ① 一笔无项目出库（source=调拨项目·转出）—— 从通用库存扣掉
      ② 一笔挂项目入库（source=调拨项目·转入）—— 进这个项目的账
    净库存不变，而这个物料从此变成「项目物料」，于是：
      · 自动出现在项目的物料需求里（`_demand_rows` 第二段专收挂项目的入库），后面统一领料出库
      · 自动退出「库存总览 / 库存金额」（`_project_material_ids` 认这笔挂项目的入库）
      · 成本自动归到这个项目（`_project_cost_map` 腿A）
    不用为它写任何新的过滤逻辑——这是选两笔流水而不是加一个 `project_id` 字段的原因。

    ⚠️ 入库那笔的金额按**加权均价**算，不是物料主数据上的参考单价 unit_price：
    库存金额/项目成本全系统都用加权均价，用参考价会让调拨前后总额对不上。
    """
    lines = [ln for ln in body.lines if ln.qty and ln.qty > 0 and ln.material_id]
    if not lines:
        raise HTTPException(400, "请选择要调拨的物料并填写数量")
    pr = await db.execute(select(models.Project).where(
        models.Project.id == body.project_id, models.Project.is_deleted == False))  # noqa: E712
    p = pr.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")
    mids = [ln.material_id for ln in lines]
    stock = await _stock_map(db, mids)
    avg = await _avg_price_map(db)
    mrows = {m.id: m for m in (await db.execute(
        select(models.WhMaterial).where(models.WhMaterial.id.in_(mids)))).scalars().all()}
    bd = normalize_date_str(body.biz_date) or date.today().isoformat()
    note = (body.note or "").strip()
    moved, skipped = 0, []
    for ln in lines:
        m = mrows.get(ln.material_id)
        if not m:
            skipped.append(f"物料{ln.material_id}不存在")
            continue
        avail = stock.get(m.id, m.init_stock or 0)
        if ln.qty > avail:
            skipped.append(f"{m.name} 现存 {avail} 不足 {ln.qty}")
            continue
        price = avg.get(m.id)
        amt = round(ln.qty * price, 4) if price is not None else None
        party = f"调拨至 {p.code}" + (f"（{note}）" if note else "")
        db.add(models.WhTxn(
            material_id=m.id, biz_date=bd, direction="out", qty=ln.qty,
            unit_price=price, amount=amt, source="调拨项目·转出", party=party,
            project_id=None, location=m.location,
            ref_no=await _next_ref(db, "out", bd), operator_id=current.id))
        db.add(models.WhTxn(
            material_id=m.id, biz_date=bd, direction="in", qty=ln.qty,
            unit_price=price, amount=amt, source="调拨项目·转入", party=party,
            project_id=p.id, location=(body.location or m.location),
            ref_no=await _next_ref(db, "in", bd), operator_id=current.id))
        moved += 1
    if not moved:
        raise HTTPException(400, "没有可调拨的物料：" + "；".join(skipped[:3]))
    await db.commit()
    await write_audit(db, user=current, action="wh_transfer_to_project", target_type="project",
                      target_id=p.id, detail=f"库位调项目物料 {moved} 项" + (f"，{len(skipped)} 项跳过" if skipped else ""))
    msg = f"已调 {moved} 项到 {p.code} 的项目物料，可在「物料需求」里领用出库"
    if skipped:
        msg += f"（{len(skipped)} 项跳过：{skipped[0]}）"
    return schemas.Msg(message=msg)


@router.get("/inventory-value")
async def inventory_value(
    _: models.User = Depends(require_roles("finance", "finance_lead")),   # 🆕 权限统一:tab由二级菜单权限控
    db: AsyncSession = Depends(get_db),
):
    """库存金额：各物料 现存 × 入库加权平均单价，汇总总库存金额（仅管理层）。

    🆕 反馈#388：**只算通用物料**。挂过项目编号的料在收货那一刻就已经计进
    「项目材料成本」了，再挂在库存金额里就是同一笔钱数两遍。
    被排除的部分不藏起来，用 excluded_value / excluded_count 单独报出来——
    上线当天这一刀砍掉 ¥116,718(79%)，不明说的话财务只会以为系统坏了。"""
    stock = await _stock_map(db)
    avg = await _avg_price_map(db)
    pm = await _project_material_ids(db)
    mats = (await db.execute(select(models.WhMaterial))).scalars().all()
    rows = []
    total = 0.0
    excluded_value, excluded_count = 0.0, 0
    for m in mats:
        st = stock.get(m.id, 0)
        price = avg.get(m.id)
        val = round(st * price, 2) if price is not None else None
        if m.id in pm:
            if val:
                excluded_value += val
            if st:
                excluded_count += 1
            continue
        if val:
            total += val
        rows.append({"material_id": m.id, "name": m.name, "spec": m.spec,
                     "unit": m.unit, "stock": st, "avg_price": price, "value": val})
    rows.sort(key=lambda x: (x["value"] or 0), reverse=True)
    return {"total_value": round(total, 2), "rows": rows,
            "excluded_value": round(excluded_value, 2), "excluded_count": excluded_count,
            "note": "只统计通用物料；挂过项目编号的料已在收货时计入项目材料成本，不重复计"}


@router.get("/project-cost")
async def project_cost(
    _: models.User = Depends(require_roles("finance", "finance_lead")),   # 🆕 权限统一:tab由二级菜单权限控
    db: AsyncSession = Depends(get_db),
):
    """项目材料成本（仅管理层）。口径见 `_project_cost_map` —— 收货即计成本。"""
    by_proj, extra = await _project_cost_map(db)
    if not by_proj:
        return {"rows": [], **extra}
    pr = await db.execute(select(models.Project.id, models.Project.code, models.Project.name)
                          .where(models.Project.id.in_(list(by_proj.keys()))))
    pmap = {i: (c, n) for i, c, n in pr.all()}
    rows = [{"project_id": pid, "code": pmap.get(pid, ("", ""))[0],
             "name": pmap.get(pid, ("", ""))[1], "cost": round(cost, 2)}
            for pid, cost in by_proj.items()]
    rows.sort(key=lambda x: x["cost"], reverse=True)
    return {"rows": rows, **extra}


async def _project_cost_detail_rows(db: AsyncSession, project_id: int) -> list[dict]:
    """🆕 反馈#389/#390：某个项目的材料成本**逐物料明细**——展开才查，不随总表一起拉。

    总表 500 个项目 × 每个几十行物料，一次全查出来是几万行；按项目单独取，
    展开哪个查哪个（前端 lazy expand）。两条腿分别标出来，好对账：
      收货  = 挂本项目编号的采购入库（钱在到货那一刻就算本项目的）
      领料  = 超领的部分，从通用库存里扣出来的（见 `_project_cost_core` 腿B）

    ⚠️ 明细和总表**走同一个 `_project_cost_core`**，不是各算一遍。腿B 的通用池分配
    依赖全局（谁先领谁扣得到），两处分头实现必然对不上；这里只是把扫描面收窄到
    本项目碰过的物料，算法一模一样，所以逐行加总恒等于外面那个合计。"""
    touched = (await db.execute(
        select(models.WhTxn.material_id).where(
            models.WhTxn.project_id == project_id,
            models.WhTxn.is_reversal == False,  # noqa: E712
            models.WhTxn.reversed == False).distinct())).all()   # noqa: E712
    mids = [m for (m,) in touched if m is not None]
    if not mids:
        return []
    core, _ = await _project_cost_core(db, material_ids=mids)
    mats = {m.id: m for m in (await db.execute(
        select(models.WhMaterial).where(models.WhMaterial.id.in_(mids)))).scalars().all()}
    rows: list[dict] = []
    for r in core:
        if r["project_id"] != project_id or not r["qty"]:
            continue
        m = mats.get(r["material_id"])
        if not m:
            continue
        rows.append({"material_id": r["material_id"], "name": m.name, "spec": m.spec,
                     "unit": m.unit, "qty": round(r["qty"], 4),
                     "avg_price": (round(r["avg_price"], 4) if r["avg_price"] is not None else None),
                     "amount": (round(r["amount"], 2) if r["amount"] is not None else None),
                     "leg": r["leg"]})
    rows.sort(key=lambda x: (x["amount"] or 0), reverse=True)
    return rows


@router.get("/project-cost/{project_id}/detail")
async def project_cost_detail(
    project_id: int,
    _: models.User = Depends(require_roles("finance", "finance_lead")),
    db: AsyncSession = Depends(get_db),
):
    """🆕 反馈#389：项目材料成本展开——逐物料明细，点开哪个项目才查哪个。"""
    rows = await _project_cost_detail_rows(db, project_id)
    return {"rows": rows, "total": round(sum(x["amount"] or 0 for x in rows), 2),
            "noprice_count": sum(1 for x in rows if x["amount"] is None)}


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
