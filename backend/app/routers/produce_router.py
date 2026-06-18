"""🆕 2026-06-19 生产部分组派发（钣金组 / 装配组）。

销售下单给生产部后，生产部主管把任务「派发」给两个组（取代旧的单人分派）：
- POST /api/produce/dispatch/{order_id}    主管派发：建钣金组+装配组两组任务，生产单转 in_progress
- POST /api/produce/group/{task_id}/done   组员/主管标记完成；两组都完成 → 父生产单 done
- GET  /api/produce/sheetmetal-projects     钣金组 tab：只看已派发给本组的项目（含钣金装配表引用）
- GET  /api/produce/assembly-projects        装配组 tab：同上 + 标准件清单/外协加工「进行中/已备齐」

两组都完成时把父生产任务单（dept_orders.dept==produce）置 done，保持发货闸门 D5 /
部门报表口径不变（它们只看生产单 status==done）。
"""
from datetime import date, datetime, timezone
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

GROUPS = ("sheetmetal", "assembly")
GROUP_NAME = {"sheetmetal": "钣金组", "assembly": "装配组"}
GROUP_ROLE = {"sheetmetal": "sheetmetal", "assembly": "assembler"}
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


# ==================== 派发（主管手动） ====================
class DispatchIn(BaseModel):
    due_date: Optional[str] = None   # 预计完成（选填，供部门报表/逾期口径）


@router.post("/dispatch/{order_id}", response_model=schemas.Msg)
async def dispatch_produce(
    order_id: int,
    data: DispatchIn = DispatchIn(),
    current: models.User = Depends(require_roles("pm_lead")),
    db: AsyncSession = Depends(get_db),
):
    """生产部主管派发：把待分派的生产任务同时派给钣金组、装配组两个角色。"""
    o = await _produce_order(db, order_id)
    if o.status in ("done", "voided"):
        raise HTTPException(400, "已完成/已作废的任务单不可派发")

    # 已派发过的组不重复建（幂等）
    res = await db.execute(select(models.ProduceGroupTask).where(
        models.ProduceGroupTask.order_id == o.id))
    existing = {t.group for t in res.scalars().all()}
    created = 0
    for g in GROUPS:
        if g in existing:
            continue
        db.add(models.ProduceGroupTask(
            order_id=o.id, project_id=o.project_id, group=g,
            status="dispatched", dispatched_by=current.id))
        created += 1

    o.status = "in_progress"
    if not o.start_date:
        o.start_date = date.today().isoformat()
    if data.due_date:
        o.due_date = data.due_date
    await db.commit()

    p = o.project
    for g in GROUPS:
        await push_message(db, to_role=GROUP_ROLE[g], kind="info",
                           text=f"【生产派发】{p.code if p else ''} {p.name if p else ''} "
                                f"已派发到{GROUP_NAME[g]}（派发：{_uname(current)}）。",
                           biz_type="project", biz_id=o.project_id)
    await write_audit(db, user=current, action="produce_dispatch", target_type="dept_order",
                      target_id=o.id, detail=f"派发到钣金组+装配组（新建{created}组）")
    return schemas.Msg(message="已派发到钣金组、装配组")


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
    all_done = len(tasks) >= len(GROUPS) and all(x.status == "done" for x in tasks)
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


# ==================== 组项目列表（tab） ====================
class GroupProjectRow(BaseModel):
    project_id: int
    code: str
    name: str
    designer: Optional[str] = None
    task_id: int
    group_done: bool = False
    sheetmetal_datasheet_id: Optional[int] = None   # 钣金装配表（只读引用）
    sheetmetal_done: bool = False
    # 仅装配组用：标准件清单/外协加工 是否已备齐
    standard_ready: Optional[bool] = None
    outsource_ready: Optional[bool] = None


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


async def _group_rows(db: AsyncSession, current: models.User, group: str) -> List[GroupProjectRow]:
    # 范围：本组角色看已派发给本组的项目；生产主管/管理层看全部
    res = await db.execute(
        select(models.ProduceGroupTask, models.DeptOrder)
        .join(models.DeptOrder, models.ProduceGroupTask.order_id == models.DeptOrder.id)
        .where(models.ProduceGroupTask.group == group, models.DeptOrder.status != "voided"))
    pairs = [(t, o) for t, o in res.all()]
    if not pairs:
        return []
    pids = [t.project_id for t, _ in pairs]

    res = await db.execute(select(models.Project).where(
        models.Project.id.in_(pids), models.Project.is_deleted == False))  # noqa: E712
    proj_by_id = {p.id: p for p in res.scalars().all()}
    designer_by_pid = await _designer_by_pid(db, pids)
    bj_by_pid = await _sheets_by_pid(db, pids, ("钣金装配",))
    ready_by_pid = {}
    if group == "assembly":
        ready_by_pid = await _sheets_by_pid(db, pids, ASSEMBLY_READY_SHEETS)

    rows: List[GroupProjectRow] = []
    for t, _o in pairs:
        p = proj_by_id.get(t.project_id)
        if not p:
            continue
        bj = bj_by_pid.get(p.id, {}).get("钣金装配")
        row = GroupProjectRow(
            project_id=p.id, code=p.code, name=p.name,
            designer=(p.extra or {}).get("__o__设计师") or designer_by_pid.get(p.id),
            task_id=t.id, group_done=(t.status == "done"),
            sheetmetal_datasheet_id=bj.id if bj else None,
            sheetmetal_done=bool(bj.done_flag) if bj else False,
        )
        if group == "assembly":
            sheets = ready_by_pid.get(p.id, {})
            row.standard_ready = await _sheet_ready(db, sheets.get("标准件清单"))
            row.outsource_ready = await _sheet_ready(db, sheets.get("外协加工"))
        rows.append(row)
    rows.sort(key=lambda x: x.code)
    return rows


@router.get("/sheetmetal-projects", response_model=List[GroupProjectRow])
async def sheetmetal_projects(
    current: models.User = Depends(require_roles("sheetmetal", "pm_lead")),
    db: AsyncSession = Depends(get_db),
):
    return await _group_rows(db, current, "sheetmetal")


@router.get("/assembly-projects", response_model=List[GroupProjectRow])
async def assembly_projects(
    current: models.User = Depends(require_roles("assembler", "pm_lead")),
    db: AsyncSession = Depends(get_db),
):
    return await _group_rows(db, current, "assembly")
