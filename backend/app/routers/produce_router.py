"""🆕 2026-06-19 生产部分组派发（钣金组 / 装配组）。

销售下单给生产部后，生产部主管把任务「派发」给两个组（取代旧的单人分派）：
- POST /api/produce/dispatch/{order_id}    主管派发：建钣金组+装配组两组任务，生产单转 in_progress
- POST /api/produce/group/{task_id}/done   组员/主管标记完成；两组都完成 → 父生产单 done
- GET  /api/produce/sheetmetal-projects     钣金组 tab：只看已派发给本组的项目（含钣金装配表引用）
- GET  /api/produce/assembly-projects        装配组 tab：同上 + 标准件清单/外协加工「进行中/已备齐」

两组都完成时把父生产任务单（dept_orders.dept==produce）置 done，保持发货闸门 D5 /
部门报表口径不变（它们只看生产单 status==done）。
"""
from datetime import date, datetime, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user, require_roles
from ..notify import push_message
from ..utils import write_audit

router = APIRouter(prefix="/api/produce", tags=["生产部分组"])

# 🆕 反馈#209：新增「封板组(sealing)」为第三生产小组。
#   钣金组/装配组为「必派」组(父生产单完成的前置)；封板组为「可选」组——
#   若某单派了封板组则它也须完成才算生产完成,没派封板组则不影响(向后兼容存量)。
GROUPS = ("sheetmetal", "assembly", "sealing")
REQUIRED_GROUPS = ("sheetmetal", "assembly")   # 父单完成必须两组都派且都完成
GROUP_NAME = {"sheetmetal": "钣金组", "assembly": "装配组", "sealing": "封板组"}
GROUP_ROLE = {"sheetmetal": "sheetmetal", "assembly": "assembler", "sealing": "sealing"}
# 装配组「备齐」判定依据的两张表（项目详单中「进度」列全为「完成」才算已备齐）
ASSEMBLY_READY_SHEETS = ("标准件清单", "外协加工")


def _uname(u: Optional[models.User]) -> str:
    if not u:
        return ""
    return u.full_name or u.username


async def _produce_order(db: AsyncSession, order_id: int) -> models.DeptOrder:
    res = await db.execute(select(models.DeptOrder).where(models.DeptOrder.id == order_id))
    o = res.scalar_one_or_none()
    if not o:
        raise HTTPException(404, "任务单不存在")
    if o.dept != "produce":
        raise HTTPException(400, "仅生产部任务单可派发到钣金/装配组")
    return o


# ==================== 派发可选人员（主管下拉） ====================
class DispatchOptions(BaseModel):
    sheetmetal: list[schemas.OrderOptionUser] = []
    assembly: list[schemas.OrderOptionUser] = []
    sealing: list[schemas.OrderOptionUser] = []   # 🆕 反馈#209 封板组


async def _role_users(db: AsyncSession, code: str) -> list[schemas.OrderOptionUser]:
    """某角色的在线可派发用户（锚点角色或多角色关联命中任一即算）。"""
    rid = (await db.execute(select(models.Role.id).where(models.Role.code == code))).scalar_one_or_none()
    if rid is None:
        return []
    sub = select(models.UserRole.user_id).where(models.UserRole.role_id == rid)
    res = await db.execute(select(models.User).where(
        models.User.is_active == True,  # noqa: E712
        (models.User.role_id == rid) | (models.User.id.in_(sub))).order_by(models.User.id))
    return [schemas.OrderOptionUser(id=u.id, name=_uname(u)) for u in res.scalars().all()]


@router.get("/dispatch-options", response_model=DispatchOptions)
async def dispatch_options(
    _: models.User = Depends(require_roles("pm_lead")),
    db: AsyncSession = Depends(get_db),
):
    """派发下拉：钣金组(sheetmetal)、装配组(assembler)、封板组(sealing) 各自的可派发人员。"""
    return DispatchOptions(
        sheetmetal=await _role_users(db, "sheetmetal"),
        assembly=await _role_users(db, "assembler"),
        sealing=await _role_users(db, "sealing"),   # 🆕 反馈#209 封板组
    )


# ==================== 派发（主管手动，分别指定两组的人） ====================
class DispatchIn(BaseModel):
    sheetmetal_worker_id: Optional[int] = None   # 派给钣金组的人（可选）
    assembly_worker_id: Optional[int] = None     # 派给装配组的人（可选）
    sealing_worker_id: Optional[int] = None      # 🆕 反馈#209 派给封板组的人（可选）


@router.post("/dispatch/{order_id}", response_model=schemas.Msg)
async def dispatch_produce(
    order_id: int,
    data: DispatchIn,
    current: models.User = Depends(require_roles("pm_lead")),
    db: AsyncSession = Depends(get_db),
):
    """生产部主管派发：钣金组、装配组各自可选，至少选一组。"""
    o = await _produce_order(db, order_id)
    if o.status in ("done", "voided"):
        raise HTTPException(400, "已完成/已作废的任务单不可派发")

    worker_of = {g: wid for g, wid in [
        ("sheetmetal", data.sheetmetal_worker_id),
        ("assembly",   data.assembly_worker_id),
        ("sealing",    data.sealing_worker_id),   # 🆕 反馈#209 封板组
    ] if wid is not None}
    if not worker_of:
        raise HTTPException(400, "至少选择一组（钣金组/装配组/封板组）进行派发")

    # 校验各组人员角色
    for g, wid in worker_of.items():
        w = (await db.execute(select(models.User).where(models.User.id == wid))).scalar_one_or_none()
        if not w or not w.is_active or not w.has_role(GROUP_ROLE[g]):
            raise HTTPException(400, f"{GROUP_NAME[g]}派发对象必须是「{GROUP_NAME[g]}」角色的在职人员")

    res = await db.execute(select(models.ProduceGroupTask).where(
        models.ProduceGroupTask.order_id == o.id))
    by_group = {t.group: t for t in res.scalars().all()}
    for g, wid in worker_of.items():
        t = by_group.get(g)
        if t:
            t.worker_id = wid
        else:
            db.add(models.ProduceGroupTask(
                order_id=o.id, project_id=o.project_id, group=g,
                status="dispatched", worker_id=wid, dispatched_by=current.id))

    o.status = "in_progress"
    if not o.start_date:
        o.start_date = date.today().isoformat()
    await db.commit()

    p = o.project
    for g, wid in worker_of.items():
        await push_message(db, to_user_id=wid, kind="info",
                           text=f"【生产派发】{p.code if p else ''} {p.name if p else ''} "
                                f"已派发给你（{GROUP_NAME[g]}，派发：{_uname(current)}）。",
                           biz_type="project", biz_id=o.project_id)
    groups_label = "、".join(GROUP_NAME[g] for g in worker_of)
    await write_audit(db, user=current, action="produce_dispatch", target_type="dept_order",
                      target_id=o.id, detail=f"派发 {groups_label}")
    return schemas.Msg(message=f"已派发到{groups_label}")


# ==================== 组员标记完成 ====================
class GroupDoneIn(BaseModel):
    done: bool = True


@router.post("/group/{task_id}/done", response_model=schemas.Msg)
async def group_mark_done(
    task_id: int,
    data: GroupDoneIn = GroupDoneIn(),
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """钣金组/装配组组员（或主管/管理层）标记本组完成；两组都完成 → 父生产单 done。"""
    res = await db.execute(select(models.ProduceGroupTask).where(
        models.ProduceGroupTask.id == task_id))
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(404, "分组任务不存在")
    # 权限：本组角色 或 生产主管/管理层
    if not current.has_role(GROUP_ROLE[t.group], "pm_lead", "admin", "manager"):
        raise HTTPException(403, f"仅{GROUP_NAME[t.group]}或生产主管/管理层可标记")

    t.status = "done" if data.done else "dispatched"
    t.done_by = current.id if data.done else None
    t.done_at = datetime.now(timezone.utc) if data.done else None

    # 重算父生产单：两组都 done → done；否则回 in_progress
    o = await _produce_order(db, t.order_id)
    res = await db.execute(select(models.ProduceGroupTask).where(
        models.ProduceGroupTask.order_id == o.id))
    tasks = list(res.scalars().all())
    # 🆕 反馈#209：必派两组(钣金+装配)都派且所有已派组(含可选封板组)都完成 → 父生产单完成。
    #   封板组为可选组：派了就要完成,没派不影响(与存量向后兼容)。
    present = {x.group for x in tasks}
    required_ok = set(REQUIRED_GROUPS) <= present
    all_done = required_ok and all(x.status == "done" for x in tasks)
    notify_done = False
    if all_done and o.status != "done":
        o.status = "done"
        o.done_date = date.today().isoformat()
        notify_done = True
    elif not all_done and o.status == "done":
        o.status = "in_progress"
        o.done_date = None
    await db.commit()

    if notify_done:
        p = o.project
        await push_message(db, to_role="logistics", kind="info",
                           text=f"【生产完成】{p.code if p else ''} {p.name if p else ''} "
                                f"钣金/装配两组均已完成，可安排发货。",
                           biz_type="project", biz_id=o.project_id)
    await write_audit(db, user=current, action="produce_group_done", target_type="produce_group_task",
                      target_id=t.id, detail=f"{GROUP_NAME[t.group]}={'完成' if data.done else '撤销完成'}")
    return schemas.Msg(message="已标记完成" if data.done else "已撤销完成")


# ==================== 组员设置本组「预计完成」 ====================
def _valid_ymd(s: str) -> bool:
    try:
        y, m, d = str(s).split("-")
        date(int(y), int(m), int(d))
        return True
    except Exception:
        return False


class GroupDueIn(BaseModel):
    due_date: str


@router.post("/group/{task_id}/due", response_model=schemas.Msg)
async def set_group_due(
    task_id: int,
    data: GroupDueIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """钣金组/装配组各自设置本组「预计完成」日期（部门报表按此算效率/逾期）。
    口径：组员（或主管/管理层）填写；填写一次后锁定，仅管理层可改正。"""
    res = await db.execute(select(models.ProduceGroupTask).where(
        models.ProduceGroupTask.id == task_id))
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(404, "分组任务不存在")
    if not current.has_role(GROUP_ROLE[t.group], "pm_lead", "admin", "manager"):
        raise HTTPException(403, f"仅{GROUP_NAME[t.group]}或生产主管/管理层可设置")
    # 填写一次后锁定（仅管理层可改正）
    if t.due_date and not current.has_role("admin", "manager"):
        raise HTTPException(403, "预计完成已填写，不可修改（仅管理层可改）")
    if not _valid_ymd(data.due_date):
        raise HTTPException(400, "请填写有效日期 (YYYY-MM-DD)")
    o = await _produce_order(db, t.order_id)
    if o.start_date and data.due_date < o.start_date:
        raise HTTPException(400, "预计完成不能早于生产开始")
    t.due_date = data.due_date
    await db.commit()
    await write_audit(db, user=current, action="produce_group_due", target_type="produce_group_task",
                      target_id=t.id, detail=f"{GROUP_NAME[t.group]} 预计完成={data.due_date}")
    return schemas.Msg(message="已设置本组预计完成")


# ==================== 🆕 #194 换人（重新指派本组负责人） ====================
class GroupReassignIn(BaseModel):
    worker_id: int


@router.post("/group/{task_id}/reassign", response_model=schemas.Msg)
async def group_reassign(
    task_id: int,
    data: GroupReassignIn,
    current: models.User = Depends(require_roles("pm_lead")),
    db: AsyncSession = Depends(get_db),
):
    """🆕 #194 生产主管/管理层给钣金组/装配组任务换人（已完成的组任务不可换）。"""
    res = await db.execute(select(models.ProduceGroupTask).where(
        models.ProduceGroupTask.id == task_id))
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(404, "分组任务不存在")
    if t.status == "done":
        raise HTTPException(400, "该组已标记完成，不可换人（可先撤销完成）")
    w = (await db.execute(select(models.User).where(models.User.id == data.worker_id))).scalar_one_or_none()
    if not w or not w.is_active or not w.has_role(GROUP_ROLE[t.group]):
        raise HTTPException(400, f"必须换给「{GROUP_NAME[t.group]}」角色的在职人员")
    if t.worker_id == w.id:
        raise HTTPException(400, "已经是该负责人")
    old = (await db.execute(select(models.User).where(models.User.id == t.worker_id))).scalar_one_or_none() \
        if t.worker_id else None
    t.worker_id = w.id
    await db.commit()
    p = t.project
    await push_message(db, to_user_id=w.id, kind="info",
                       text=f"【生产换人】{p.code if p else ''} {p.name if p else ''} "
                            f"（{GROUP_NAME[t.group]}）已改派给你（操作：{_uname(current)}）。",
                       biz_type="project", biz_id=t.project_id)
    await write_audit(db, user=current, action="produce_group_reassign", target_type="produce_group_task",
                      target_id=t.id, detail=f"{GROUP_NAME[t.group]} {_uname(old) or '—'} → {_uname(w)}")
    return schemas.Msg(message=f"已把{GROUP_NAME[t.group]}任务改派给 {_uname(w)}")


# ==================== 组项目列表（tab） ====================
class GroupProjectRow(BaseModel):
    project_id: int
    code: str
    name: str
    designer: Optional[str] = None
    task_id: int
    worker_name: Optional[str] = None   # 派给谁（主管/管理层视角展示）
    group_done: bool = False
    start_date: Optional[str] = None    # 🆕 生产开始(父单派发日)
    due_date: Optional[str] = None      # 🆕 本组预计完成(组员填，填后锁定)
    done_date: Optional[str] = None     # 🆕 本组完成日期(中国自然日)
    sheetmetal_datasheet_id: Optional[int] = None   # 钣金装配表（只读引用）
    sheetmetal_done: bool = False
    # 仅装配组用：标准件清单/外协加工 是否已备齐
    standard_ready: Optional[bool] = None
    outsource_ready: Optional[bool] = None
    material_locations: List[str] = []   # 🆕 #204 本项目材料所在库位(入库流水去重,供装配/钣金知道去哪拿料)
    # 🆕 反馈#209 封板组「推送激光图」：激光件清单(只读表)+ CAD激光图纸文件(设计产出,可下载)
    laser_datasheet_id: Optional[int] = None
    laser_files: List[dict] = []
    # 🆕 封板文件(机架图/横梁图)：设计推送给封板组的产出(order_start_output kind=sealing_pkg,可下载)
    sealing_files: List[dict] = []


async def _designer_by_pid(db: AsyncSession, pids: list[int]) -> dict[int, str]:
    """设计师名：优先一览 __o__设计师，否则回退设计任务单负责人（与采购页同口径）。"""
    res = await db.execute(
        select(models.DeptOrder.project_id, models.User.full_name, models.User.username)
        .join(models.User, models.DeptOrder.worker_id == models.User.id)
        .where(models.DeptOrder.dept == "design", models.DeptOrder.project_id.in_(pids)))
    by: dict[int, str] = {}
    for did, fn, un in res.all():
        by.setdefault(did, fn or un)
    return by


async def _sheets_by_pid(db: AsyncSession, pids: list[int], names: tuple[str, ...]) -> dict[int, dict[str, models.Datasheet]]:
    res = await db.execute(select(models.Datasheet).where(
        models.Datasheet.project_id.in_(pids), models.Datasheet.name.in_(names)))
    by: dict[int, dict[str, models.Datasheet]] = {}
    for d in res.scalars().all():
        by.setdefault(d.project_id, {})[d.name] = d
    return by


async def _sheet_ready(db: AsyncSession, ds: Optional[models.Datasheet]) -> bool:
    """该数据表是否「已备齐」：有记录且全部记录的「进度」列 == 完成。"""
    if not ds:
        return False
    res = await db.execute(select(models.Field).where(
        models.Field.datasheet_id == ds.id, models.Field.name == "进度"))
    fld = res.scalar_one_or_none()
    if not fld:
        return False
    res = await db.execute(select(models.Record).where(models.Record.datasheet_id == ds.id))
    recs = list(res.scalars().all())
    if not recs:
        return False
    key = str(fld.id)
    return all((r.values or {}).get(key) == "完成" for r in recs)


async def _laser_files_by_pid(db: AsyncSession, pids: list[int], kind: str = "sheetpkg") -> dict[int, list[dict]]:
    """🆕 反馈#209「推送设计产物」：项目的设计任务产出文件(order_start_output)。
    kind=sheetpkg→CAD激光图纸;kind=sealing_pkg→封板文件(机架图/横梁图)。封板组 tab 里可直接下载。"""
    if not pids:
        return {}
    dord = await db.execute(select(models.DeptOrder.id, models.DeptOrder.project_id).where(
        models.DeptOrder.dept == "design", models.DeptOrder.project_id.in_(pids)))
    oid2pid = {oid: pid for oid, pid in dord.all()}
    if not oid2pid:
        return {}
    ar = await db.execute(select(models.Attachment).where(
        models.Attachment.biz_type == "order_start_output",
        models.Attachment.kind == kind,
        models.Attachment.biz_id.in_(list(oid2pid.keys()))).order_by(models.Attachment.id))
    out: dict[int, list[dict]] = {}
    for a in ar.scalars().all():
        pid = oid2pid.get(a.biz_id)
        if pid:
            out.setdefault(pid, []).append({"id": a.id, "name": a.name})
    return out


async def _project_loc_map(db: AsyncSession, pids: list[int]) -> dict[int, list[str]]:
    """🆕 #204 项目材料库位：该项目入库流水(非冲红)去过的库位,去重保序。
    装配/钣金按项目一眼看到料在哪个库位。"""
    if not pids:
        return {}
    rows = (await db.execute(select(models.WhTxn.project_id, models.WhTxn.location).where(
        models.WhTxn.project_id.in_(pids), models.WhTxn.direction == "in",
        models.WhTxn.is_reversal == False,  # noqa: E712
        models.WhTxn.location.isnot(None)).order_by(models.WhTxn.id))).all()
    out: dict[int, list[str]] = {}
    for pid, loc in rows:
        loc = (loc or "").strip()
        if not loc:
            continue
        lst = out.setdefault(pid, [])
        if loc not in lst:
            lst.append(loc)
    return out


async def _group_rows(db: AsyncSession, current: models.User, group: str,
                      year: Optional[str] = None, proj_status: Optional[str] = None) -> List[GroupProjectRow]:
    # 范围：本组组员只看派给自己的项目；生产主管/管理层看本组全部
    q = (select(models.ProduceGroupTask, models.DeptOrder)
         .join(models.DeptOrder, models.ProduceGroupTask.order_id == models.DeptOrder.id)
         .where(models.ProduceGroupTask.group == group, models.DeptOrder.status != "voided"))
    is_boss = current.has_role("pm_lead", "manager", "admin")
    if not is_boss:
        q = q.where(models.ProduceGroupTask.worker_id == current.id)
    res = await db.execute(q)
    pairs = [(t, o) for t, o in res.all()]
    if not pairs:
        return []
    pids = [t.project_id for t, _ in pairs]
    # 派给谁（主管视角展示）
    wids = {t.worker_id for t, _ in pairs if t.worker_id}
    wname_by_id: dict[int, str] = {}
    if wids:
        wr = await db.execute(select(models.User).where(models.User.id.in_(wids)))
        wname_by_id = {u.id: _uname(u) for u in wr.scalars().all()}

    proj_q = select(models.Project).where(
        models.Project.id.in_(pids), models.Project.is_deleted == False)  # noqa: E712
    if year:
        proj_q = proj_q.where(models.Project.code.like(f"{year}-%"))
    if proj_status:
        proj_q = proj_q.where(models.Project.status == proj_status)
    res = await db.execute(proj_q)
    proj_by_id = {p.id: p for p in res.scalars().all()}
    designer_by_pid = await _designer_by_pid(db, pids)
    loc_by_pid = await _project_loc_map(db, pids)   # 🆕 #204 项目材料库位
    bj_by_pid = await _sheets_by_pid(db, pids, ("钣金装配",))
    ready_by_pid = {}
    if group == "assembly":
        ready_by_pid = await _sheets_by_pid(db, pids, ASSEMBLY_READY_SHEETS)
    # 🆕 反馈#209 封板组「推送激光图」：激光件清单(数据表)+ CAD激光图纸(设计产出文件)
    laser_ds_by_pid: dict = {}
    laser_files_by_pid: dict = {}
    sealing_files_by_pid: dict = {}   # 🆕 封板文件(机架图/横梁图)
    if group == "sealing":
        laser_ds_by_pid = await _sheets_by_pid(db, pids, ("激光件清单",))
        laser_files_by_pid = await _laser_files_by_pid(db, pids)
        sealing_files_by_pid = await _laser_files_by_pid(db, pids, "sealing_pkg")

    rows: List[GroupProjectRow] = []
    for t, _o in pairs:
        p = proj_by_id.get(t.project_id)
        if not p:
            continue
        bj = bj_by_pid.get(p.id, {}).get("钣金装配")
        row = GroupProjectRow(
            project_id=p.id, code=p.code, name=p.name,
            designer=(p.extra or {}).get("__o__设计师") or designer_by_pid.get(p.id),
            task_id=t.id, worker_name=wname_by_id.get(t.worker_id) if t.worker_id else None,
            group_done=(t.status == "done"),
            start_date=_o.start_date, due_date=t.due_date,
            done_date=(t.done_at + timedelta(hours=8)).strftime("%Y-%m-%d") if t.done_at else None,
            sheetmetal_datasheet_id=bj.id if bj else None,
            sheetmetal_done=bool(bj.done_flag) if bj else False,
            material_locations=loc_by_pid.get(p.id, []),
        )
        if group == "assembly":
            sheets = ready_by_pid.get(p.id, {})
            row.standard_ready = await _sheet_ready(db, sheets.get("标准件清单"))
            row.outsource_ready = await _sheet_ready(db, sheets.get("外协加工"))
        if group == "sealing":
            ld = laser_ds_by_pid.get(p.id, {}).get("激光件清单")
            row.laser_datasheet_id = ld.id if ld else None
            row.laser_files = laser_files_by_pid.get(p.id, [])
            row.sealing_files = sealing_files_by_pid.get(p.id, [])
        rows.append(row)
    rows.sort(key=lambda x: x.code, reverse=True)
    return rows


@router.get("/sheetmetal-projects", response_model=List[GroupProjectRow])
async def sheetmetal_projects(
    year: Optional[str] = None,
    proj_status: Optional[str] = None,
    current: models.User = Depends(require_roles("sheetmetal", "pm_lead")),
    db: AsyncSession = Depends(get_db),
):
    return await _group_rows(db, current, "sheetmetal", year=year, proj_status=proj_status)


@router.get("/assembly-projects", response_model=List[GroupProjectRow])
async def assembly_projects(
    year: Optional[str] = None,
    proj_status: Optional[str] = None,
    current: models.User = Depends(require_roles("assembler", "pm_lead")),
    db: AsyncSession = Depends(get_db),
):
    return await _group_rows(db, current, "assembly", year=year, proj_status=proj_status)


@router.get("/sealing-projects", response_model=List[GroupProjectRow])
async def sealing_projects(
    year: Optional[str] = None,
    proj_status: Optional[str] = None,
    current: models.User = Depends(require_roles("sealing", "pm_lead")),
    db: AsyncSession = Depends(get_db),
):
    """🆕 反馈#209 封板组 tab：被派发项目 + 激光件清单(只读) + CAD激光图纸(可下载)。"""
    return await _group_rows(db, current, "sealing", year=year, proj_status=proj_status)
