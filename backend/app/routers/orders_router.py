"""🆕 v3 部门任务单：派单 → 分派 → 接单 → 完成 全状态机（M04/M17）。

状态机：pending_assign 待分派 → assigned 待接单 → in_progress 进行中
        → done 已完成（可 reopen 回 in_progress）；任意未完成态可 voided 作废。

口径：
- B2 仅「进行中」项目可下单
- B5 开始/预计时间一旦填写本人不可改（仅管理层）
- C1-C3 效率口径见 dept_config.compute_efficiency（报表共用）
- D1 设计完成前置校验=四表有 Excel 导入记录（datasheets.imported_at）
- P-13 作废留痕（不删单），管理层收通知后可重新下单
- 接单/换人回传一览「设计师/电工」列，设计开始/完成回传「制图开始/制图结束」
"""
from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user
from ..dept_config import DEPTS, compute_efficiency
from ..notify import push_message
from ..utils import write_audit
from ..sheet_templates import SHEET_TEMPLATES, OVERVIEW_HEADER_ALIAS
from .attachments_router import save_upload, delete_attachment_file
from .projects_router import OVERVIEW_KEY_PREFIX, HEADER_KEY_PREFIX

router = APIRouter(prefix="/api/orders", tags=["部门任务单"])


# ==================== 工具 ====================
def _is_mgr(u: models.User) -> bool:
    return bool(u.role and u.role.code in ("admin", "manager"))


def _is_lead(u: models.User, dept: str) -> bool:
    return bool(u.role and u.role.code == DEPTS[dept]["lead_role"])


def _is_worker_role(u: models.User, dept: str) -> bool:
    return bool(u.role and u.role.code == DEPTS[dept]["worker_role"])


def _dept_or_400(dept: str) -> dict:
    cfg = DEPTS.get(dept)
    if not cfg:
        raise HTTPException(400, f"未知部门: {dept}")
    return cfg


async def _order_or_404(db: AsyncSession, oid: int) -> models.DeptOrder:
    res = await db.execute(select(models.DeptOrder).where(models.DeptOrder.id == oid))
    o = res.scalar_one_or_none()
    if not o:
        raise HTTPException(404, "任务单不存在")
    return o


def _uname(u: Optional[models.User]) -> Optional[str]:
    if not u:
        return None
    return u.full_name or u.username


def _writeback_overview(p: models.Project, label: str, value) -> None:
    """回写一览列（__o__label），alias 字段同步双写 __h__（与 header-cell 接口同规则）。
    value=None 表示清除。调用方负责 commit。"""
    extra = dict(p.extra or {})
    o_key = f"{OVERVIEW_KEY_PREFIX}{label}"
    h_alias = OVERVIEW_HEADER_ALIAS.get(label)
    if value is None or value == "":
        extra.pop(o_key, None)
        if h_alias:
            extra.pop(f"{HEADER_KEY_PREFIX}{h_alias}", None)
    else:
        extra[o_key] = value
        if h_alias:
            extra[f"{HEADER_KEY_PREFIX}{h_alias}"] = value
    p.extra = extra


def _valid_date(s: str) -> bool:
    try:
        y, m, d = s.split("-")
        date(int(y), int(m), int(d))
        return True
    except Exception:
        return False


async def _files_of(db: AsyncSession, order_ids: list[int]) -> dict[int, dict[str, list]]:
    """批量取任务单附件并按 (order, 类别) 分组，避免 N+1。"""
    out: dict[int, dict[str, list]] = {
        oid: {"input": [], "start": [], "output": []} for oid in order_ids
    }
    if not order_ids:
        return out
    res = await db.execute(
        select(models.Attachment).where(
            models.Attachment.biz_type.in_(
                ("order_input", "order_start_output", "order_output")),
            models.Attachment.biz_id.in_(order_ids),
        ).order_by(models.Attachment.id)
    )
    group = {"order_input": "input", "order_start_output": "start", "order_output": "output"}
    for a in res.scalars().all():
        out[a.biz_id][group[a.biz_type]].append(schemas.AttachmentOut.model_validate(a))
    return out


def _order_to_out(o: models.DeptOrder, files: dict[str, list],
                  notify_name: Optional[str] = None) -> schemas.OrderOut:
    eff, on_time, _od = compute_efficiency(o.start_date, o.due_date, o.done_date)
    today_s = date.today().isoformat()
    overdue = bool(
        (o.status == "in_progress" and o.due_date and today_s > o.due_date)
        or (o.status == "done" and o.done_date and o.due_date and o.done_date > o.due_date)
    )
    return schemas.OrderOut(
        id=o.id, project_id=o.project_id,
        project_code=o.project.code if o.project else "",
        project_name=o.project.name if o.project else "",
        dept=o.dept, status=o.status,
        worker_id=o.worker_id, worker_name=_uname(o.worker),
        req_text=o.req_text,
        start_date=o.start_date, due_date=o.due_date, done_date=o.done_date,
        notify_user_id=o.notify_user_id, notify_user_name=notify_name,
        eff_pct=eff if o.status == "done" else None,
        on_time=on_time if o.status == "done" else None,
        overdue=overdue, created_at=o.created_at,
        input_files=files.get("input", []),
        start_files=files.get("start", []),
        output_files=files.get("output", []),
    )


# ==================== 查询 ====================
@router.get("", response_model=List[schemas.OrderOut])
async def list_orders(
    dept: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """工作台列表：工人=本人任务；部门负责人=本部门全量；管理层=全量（可按 dept 过滤）。"""
    q = select(models.DeptOrder)
    if dept:
        _dept_or_400(dept)
        q = q.where(models.DeptOrder.dept == dept)
    if status:
        q = q.where(models.DeptOrder.status == status)

    if _is_mgr(current):
        pass
    elif dept and _is_lead(current, dept):
        pass
    elif dept and _is_worker_role(current, dept):
        q = q.where(models.DeptOrder.worker_id == current.id)
    else:
        # 未指定 dept 的非管理层：按角色推断本部门
        my_dept = next(
            (k for k, c in DEPTS.items()
             if current.role and current.role.code in (c["worker_role"], c["lead_role"])),
            None,
        )
        if not my_dept:
            raise HTTPException(403, "无任务单查看权限")
        q = q.where(models.DeptOrder.dept == my_dept)
        if _is_worker_role(current, my_dept):
            q = q.where(models.DeptOrder.worker_id == current.id)

    res = await db.execute(q.order_by(models.DeptOrder.id.desc()).limit(limit))
    orders = list(res.scalars().all())
    files = await _files_of(db, [o.id for o in orders])

    # 通知人姓名批量取
    nids = {o.notify_user_id for o in orders if o.notify_user_id}
    names: dict[int, str] = {}
    if nids:
        r2 = await db.execute(select(models.User).where(models.User.id.in_(nids)))
        names = {u.id: _uname(u) for u in r2.scalars().all()}
    return [
        _order_to_out(o, files[o.id], names.get(o.notify_user_id))
        for o in orders
    ]


@router.get("/options", response_model=schemas.OrderOptionsOut)
async def order_options(
    dept: str = Query(...),
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """工作台下拉数据：可分派工人 / 完成通知人池 / 部门配置。"""
    cfg = _dept_or_400(dept)

    async def _role_users(code: str) -> list[schemas.OrderOptionUser]:
        res = await db.execute(
            select(models.User).join(models.Role).where(
                models.Role.code == code, models.User.is_active == True,  # noqa: E712
            ).order_by(models.User.id)
        )
        return [schemas.OrderOptionUser(id=u.id, name=_uname(u)) for u in res.scalars().all()]

    return schemas.OrderOptionsOut(
        workers=await _role_users(cfg["worker_role"]),
        notify_pool=await _role_users(cfg["notify_pool"]),
        notify_label=cfg["notify_label"],
        dept_name=cfg["name"],
        sheet_check=cfg["sheet_check"],
        start_outputs=cfg["start_outputs"],
        outputs=cfg["outputs"],
        start_label=cfg["start_label"], end_label=cfg["end_label"], done_label=cfg["done_label"],
    )


# ==================== 下单 ====================
@router.post("", response_model=schemas.OrderOut)
async def create_order(
    data: schemas.OrderCreate,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """下单（双入口）：管理层目录下单可带 worker_id 直接指派；
    销售下单（M02 内部调用 create_order_internal）建待分派单。"""
    if not _is_mgr(current):
        raise HTTPException(403, "仅管理层可在项目目录下单")
    cfg = _dept_or_400(data.dept)

    res = await db.execute(select(models.Project).where(
        models.Project.id == data.project_id, models.Project.is_deleted == False))  # noqa: E712
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")
    if p.status != "进行中":
        raise HTTPException(400, "仅「进行中」项目可下单")  # B2

    o = models.DeptOrder(
        project_id=p.id, dept=data.dept, req_text=(data.req_text or "").strip() or None,
        created_by=current.id,
    )
    if data.worker_id:
        res = await db.execute(select(models.User).where(models.User.id == data.worker_id))
        w = res.scalar_one_or_none()
        if not w or not w.is_active or not _is_worker_role(w, data.dept):
            raise HTTPException(400, f"指派对象必须是{cfg['name']}{DEPTS[data.dept]['worker_role']}角色")
        o.worker_id = w.id
        o.status = "assigned"
    db.add(o)
    await db.commit()
    await db.refresh(o)

    if o.status == "assigned":
        await push_message(db, to_user_id=o.worker_id, kind="info",
                           text=f"【分派】{p.code} {p.name} 已分派给你，请到{cfg['name']}工作台接单填写时间。",
                           biz_type="order", biz_id=o.id)
    else:
        await push_message(db, to_role=cfg["lead_role"], kind="info",
                           text=f"【待分派】{p.code} {p.name} 新{cfg['name']}任务待分派。",
                           biz_type="order", biz_id=o.id)
    await write_audit(db, user=current, action="create", target_type="dept_order",
                      target_id=o.id, detail=f"{p.code} {data.dept}")
    return _order_to_out(o, {"input": [], "start": [], "output": []})


async def create_order_internal(
    db: AsyncSession, *, project: models.Project, dept: str,
    req_text: Optional[str], created_by: int,
) -> models.DeptOrder:
    """供销售下单（M02）在同一事务内建待分派任务。调用方负责 commit + 推送。"""
    _dept_or_400(dept)
    o = models.DeptOrder(
        project_id=project.id, dept=dept,
        req_text=(req_text or "").strip() or None, created_by=created_by,
    )
    db.add(o)
    await db.flush()
    return o


# ==================== 附件 ====================
@router.post("/{oid}/input-files", response_model=List[schemas.AttachmentOut])
async def upload_input_files(
    oid: int,
    files: List[UploadFile] = File(...),
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """下发资料（下单方/管理层上传）。"""
    o = await _order_or_404(db, oid)
    if not (_is_mgr(current) or o.created_by == current.id):
        raise HTTPException(403, "仅下单方或管理层可传下发资料")
    outs = []
    for f in files:
        a = await save_upload(db, f, biz_type="order_input", biz_id=o.id,
                              project_id=o.project_id, user=current)
        outs.append(a)
    await db.commit()
    return [schemas.AttachmentOut.model_validate(a) for a in outs]


@router.post("/{oid}/start-upload", response_model=List[schemas.AttachmentOut])
async def start_upload(
    oid: int,
    kind: str = Query(...),
    files: List[UploadFile] = File(...),
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """接单后上传（设计图纸包→钣金 / 电工采购清单→采购），多文件累加。"""
    o = await _order_or_404(db, oid)
    cfg = DEPTS[o.dept]
    so = next((s for s in cfg["start_outputs"] if s["k"] == kind), None)
    if not so:
        raise HTTPException(400, f"{cfg['name']}没有 {kind} 类型的接单上传")
    if not (_is_mgr(current) or o.worker_id == current.id):
        raise HTTPException(403, "仅任务负责人可上传")
    if o.status not in ("in_progress", "assigned"):
        raise HTTPException(400, "任务未在进行中")

    outs = []
    for f in files:
        a = await save_upload(db, f, biz_type="order_start_output", biz_id=o.id,
                              kind=kind, project_id=o.project_id, user=current)
        outs.append(a)
    await db.commit()

    p = o.project
    await push_message(db, to_role=so["to_role"], kind="info",
                       text=f"【{so['label']}】{cfg['name']}已上传 {p.code} {so['label']} {len(outs)} 个文件，请查收。",
                       biz_type="order", biz_id=o.id)
    await write_audit(db, user=current, action="upload", target_type="dept_order",
                      target_id=o.id, detail=f"start:{kind} x{len(outs)}")
    return [schemas.AttachmentOut.model_validate(a) for a in outs]


@router.post("/{oid}/output-upload", response_model=List[schemas.AttachmentOut])
async def output_upload(
    oid: int,
    kind: str = Query(...),
    files: List[UploadFile] = File(...),
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """完成产物上传（完成弹窗内逐项上传，complete 时校验必传项）。"""
    o = await _order_or_404(db, oid)
    cfg = DEPTS[o.dept]
    if not any(x["k"] == kind for x in cfg["outputs"]):
        raise HTTPException(400, f"{cfg['name']}没有 {kind} 类型的产物")
    if not (_is_mgr(current) or o.worker_id == current.id):
        raise HTTPException(403, "仅任务负责人可上传")
    outs = []
    for f in files:
        a = await save_upload(db, f, biz_type="order_output", biz_id=o.id,
                              kind=kind, project_id=o.project_id, user=current)
        outs.append(a)
    await db.commit()
    return [schemas.AttachmentOut.model_validate(a) for a in outs]


@router.delete("/{oid}/attachments/{aid}", response_model=schemas.Msg)
async def remove_order_attachment(
    oid: int, aid: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    o = await _order_or_404(db, oid)
    if not (_is_mgr(current) or o.worker_id == current.id or o.created_by == current.id):
        raise HTTPException(403, "无权移除")
    res = await db.execute(select(models.Attachment).where(
        models.Attachment.id == aid, models.Attachment.biz_id == oid,
        models.Attachment.biz_type.in_(
            ("order_input", "order_start_output", "order_output")),
    ))
    a = res.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "附件不存在")
    name = a.name
    await delete_attachment_file(db, a)
    await db.commit()
    await write_audit(db, user=current, action="delete", target_type="dept_order",
                      target_id=oid, detail=f"附件:{name}")
    return schemas.Msg(message="已移除")


# ==================== 状态流转 ====================
@router.post("/{oid}/assign", response_model=schemas.Msg)
async def assign_order(
    oid: int, data: schemas.OrderAssignIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """部门负责人分派给具体工人。"""
    o = await _order_or_404(db, oid)
    cfg = DEPTS[o.dept]
    if not (_is_mgr(current) or _is_lead(current, o.dept)):
        raise HTTPException(403, f"仅{cfg['name']}负责人可分派")
    if o.status != "pending_assign":
        raise HTTPException(400, "仅待分派任务可分派")
    res = await db.execute(select(models.User).where(models.User.id == data.worker_id))
    w = res.scalar_one_or_none()
    if not w or not w.is_active or not _is_worker_role(w, o.dept):
        raise HTTPException(400, f"分派对象必须是{cfg['name']}工人角色")
    o.worker_id = w.id
    o.status = "assigned"
    await db.commit()
    p = o.project
    await push_message(db, to_user_id=w.id, kind="info",
                       text=f"【分派】{p.code} {p.name} 已分派给你，请到{cfg['name']}工作台接单填写时间。",
                       biz_type="order", biz_id=o.id)
    await write_audit(db, user=current, action="assign", target_type="dept_order",
                      target_id=o.id, detail=f"→{_uname(w)}")
    return schemas.Msg(message=f"已分派给 {_uname(w)}")


@router.post("/{oid}/start", response_model=schemas.Msg)
async def start_order(
    oid: int, data: schemas.OrderStartIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """工人接单：填开始/预计完成时间并开始；回传一览（设计师/电工/制图开始）。
    B5：时间一旦填写本人不可改（重开始被拒），仅管理层可改。"""
    o = await _order_or_404(db, oid)
    cfg = DEPTS[o.dept]
    if not (_is_mgr(current) or o.worker_id == current.id):
        raise HTTPException(403, "仅任务负责人可接单")
    if o.status != "assigned":
        raise HTTPException(400, "仅待接单任务可开始")
    if not _valid_date(data.start_date) or not _valid_date(data.due_date):
        raise HTTPException(400, "请填写有效日期 (YYYY-MM-DD)")
    if data.due_date < data.start_date:
        raise HTTPException(400, "预计完成不能早于开始")
    if (o.start_date or o.due_date) and not _is_mgr(current):
        raise HTTPException(403, "时间已填写，本人不可修改（仅管理层可改）")  # B5

    o.start_date, o.due_date = data.start_date, data.due_date
    o.status = "in_progress"

    # 回传一览：设计→设计师+制图开始；电工→电工（alias 双写 __h__ 电器）
    p = o.project
    wname = _uname(o.worker) or _uname(current)
    if cfg["writeback_worker"]:
        _writeback_overview(p, cfg["writeback_worker"], wname)
    if cfg["writeback_start"]:
        _writeback_overview(p, cfg["writeback_start"], data.start_date)
    await db.commit()
    await write_audit(db, user=current, action="start", target_type="dept_order",
                      target_id=o.id, detail=f"{data.start_date}~{data.due_date}")
    return schemas.Msg(message="已开始" + ("，已回传项目目录" if cfg["writeback_worker"] else ""))


@router.post("/{oid}/complete", response_model=schemas.Msg)
async def complete_order(
    oid: int, data: schemas.OrderCompleteIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """完成任务：D1 四表校验（设计）→ 必传产物校验 → 通知人必选 → done；
    逾期则推部门主管+抄送管理层（含效率%）。"""
    o = await _order_or_404(db, oid)
    cfg = DEPTS[o.dept]
    if not (_is_mgr(current) or o.worker_id == current.id):
        raise HTTPException(403, "仅任务负责人可完成")
    if o.status != "in_progress":
        raise HTTPException(400, "仅进行中任务可完成")

    # D1：设计完成前置校验四表已导入（P-16 读 imported_at）
    if cfg["sheet_check"]:
        res = await db.execute(select(models.Datasheet).where(
            models.Datasheet.project_id == o.project_id,
            models.Datasheet.name.in_(tuple(SHEET_TEMPLATES.keys())),
        ))
        sheets = {d.name: d for d in res.scalars().all()}
        missing = [n for n in SHEET_TEMPLATES.keys()
                   if n not in sheets or sheets[n].imported_at is None]
        if missing:
            raise HTTPException(400, f"四表未导入齐，无法完成：缺 {('、'.join(missing))}（请在项目详情导入 Excel）")

    # 必传产物校验
    res = await db.execute(select(models.Attachment).where(
        models.Attachment.biz_type == "order_output",
        models.Attachment.biz_id == o.id,
    ))
    kinds_present = {a.kind for a in res.scalars().all()}
    for ot in cfg["outputs"]:
        if ot.get("required") and ot["k"] not in kinds_present:
            raise HTTPException(400, f"请先上传【{ot['label']}】才能完成")

    # 通知人校验（必选，且属于通知池角色）
    res = await db.execute(select(models.User).join(models.Role).where(
        models.User.id == data.notify_user_id,
        models.Role.code == cfg["notify_pool"],
        models.User.is_active == True,  # noqa: E712
    ))
    notify_user = res.scalar_one_or_none()
    if not notify_user:
        raise HTTPException(400, f"请选择{cfg['notify_label']}")

    today_s = date.today().isoformat()
    o.status = "done"
    o.done_date = today_s
    o.notify_user_id = notify_user.id

    p = o.project
    if cfg["writeback_done"]:
        _writeback_overview(p, cfg["writeback_done"], today_s)
    await db.commit()

    eff, on_time, overdue_days = compute_efficiency(o.start_date, o.due_date, o.done_date)
    wname = _uname(o.worker)
    await push_message(db, to_user_id=notify_user.id, kind="wx",
                       text=f"【{cfg['name']}·{p.code}】{wname} 已完成{cfg['done_label']}。",
                       biz_type="order", biz_id=o.id)
    if overdue_days > 0:
        text = (f"【逾期完成】{cfg['name']} {p.code} {p.name}：预计 {o.due_date}，"
                f"实际 {o.done_date}，超 {overdue_days} 天，效率 {eff}%（负责人：{wname}）")
        await push_message(db, to_role=cfg["lead_role"], kind="warn",
                           text=text, biz_type="order", biz_id=o.id)
        await push_message(db, to_role="manager", kind="warn",
                           text=text, biz_type="order", biz_id=o.id)
    await write_audit(db, user=current, action="complete", target_type="dept_order",
                      target_id=o.id, detail=f"eff={eff}% on_time={on_time}")
    return schemas.Msg(message="已完成" + (f"，逾期 {overdue_days} 天已提醒主管" if overdue_days else ""))


@router.post("/{oid}/reopen", response_model=schemas.Msg)
async def reopen_order(
    oid: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """完成可逆（P-06）：done → in_progress，清完成日期；设计清一览制图结束。
    下游（发货资料/采购收件箱）按附件 ID 关联，M08/M06 落地时在此联动撤回。"""
    o = await _order_or_404(db, oid)
    cfg = DEPTS[o.dept]
    if not (_is_mgr(current) or o.worker_id == current.id or _is_lead(current, o.dept)):
        raise HTTPException(403, "仅任务负责人/部门负责人可改回")
    if o.status != "done":
        raise HTTPException(400, "仅已完成任务可改回进行中")
    o.status = "in_progress"
    o.done_date = None
    p = o.project
    if cfg["writeback_done"]:
        _writeback_overview(p, cfg["writeback_done"], None)
    await db.commit()
    await write_audit(db, user=current, action="reopen", target_type="dept_order",
                      target_id=o.id)
    return schemas.Msg(message="已改回进行中")


@router.post("/{oid}/void", response_model=schemas.Msg)
async def void_order(
    oid: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """作废单号（P-13 留痕；管理层收通知后可重新下单=新任务）。"""
    o = await _order_or_404(db, oid)
    cfg = DEPTS[o.dept]
    if not (_is_mgr(current) or _is_lead(current, o.dept)):
        raise HTTPException(403, f"仅{cfg['name']}负责人/管理层可作废")
    if o.status == "done":
        raise HTTPException(400, "已完成任务不可作废（请先改回进行中）")
    if o.status == "voided":
        raise HTTPException(400, "任务已作废")
    o.status = "voided"
    await db.commit()
    p = o.project
    await push_message(db, to_role="manager", kind="warn",
                       text=f"【作废】{cfg['name']} {p.code} 单号已作废，请管理层确认是否重新下单。",
                       biz_type="order", biz_id=o.id)
    await write_audit(db, user=current, action="void", target_type="dept_order", target_id=o.id)
    return schemas.Msg(message="已作废单号，已通知管理层")


@router.post("/{oid}/reassign", response_model=schemas.Msg)
async def reassign_order(
    oid: int, data: schemas.OrderReassignIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """🆕 M17 换人（防离职/请假）：待接单/进行中任务转交同部门他人；
    时间与已传产物保留；设计/电工回传项目目录。"""
    o = await _order_or_404(db, oid)
    cfg = DEPTS[o.dept]
    if not (_is_mgr(current) or _is_lead(current, o.dept)):
        raise HTTPException(403, f"仅{cfg['name']}负责人/管理层可换人")
    if o.status not in ("assigned", "in_progress"):
        raise HTTPException(400, "仅待接单/进行中任务可换人")
    if data.worker_id == o.worker_id:
        raise HTTPException(400, "转交对象不能是当前负责人")
    res = await db.execute(select(models.User).where(models.User.id == data.worker_id))
    w = res.scalar_one_or_none()
    if not w or not w.is_active or not _is_worker_role(w, o.dept):
        raise HTTPException(400, f"转交对象必须是{cfg['name']}工人角色")

    old_name = _uname(o.worker) or "—"
    o.worker_id = w.id
    p = o.project
    new_name = _uname(w)
    if cfg["writeback_worker"] and o.status == "in_progress":
        _writeback_overview(p, cfg["writeback_worker"], new_name)
    await db.commit()

    await push_message(db, to_user_id=w.id, kind="info",
                       text=f"【任务转交】{p.code} {p.name} 由 {old_name} 转交给你，请接续{cfg['done_label']}。",
                       biz_type="order", biz_id=o.id)
    await push_message(db, to_role=cfg["lead_role"], kind="info",
                       text=f"【已换人】{p.code} {cfg['name']} 负责人由 {old_name} → {new_name}。",
                       biz_type="order", biz_id=o.id)
    await write_audit(db, user=current, action="reassign", target_type="dept_order",
                      target_id=o.id, detail=f"{old_name}→{new_name}")
    return schemas.Msg(message=f"已将任务由 {old_name} 转交给 {new_name}")
