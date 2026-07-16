"""🆕 管理层待办：admin/manager 录入待办 → 勾选收件人下发 → 收件人回复承诺完成时间
→ 承诺日到仍未「已完成」则每日逾期提醒；收件人可申请顺延承诺日(管理层审批)。

口径（2026-07-16 用户确认）：
- 收件人范围：管理层每次自己勾选（复用 /api/admin/users 名单）。
- 谁能建待办：仅 admin/manager。
- 逾期定义：承诺日到了还没点「已完成」→ 逾期，开始每日提醒（见 overdue.scan_management_todos）。
- 顺延：收件人回复后可申请顺延承诺日，需管理层同意才改。
- 浮动挂件：全部人可见（收件箱 + 未处理角标）。
"""
from datetime import date, datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, delete as sa_delete
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user, require_admin_or_manager
from ..notify import push_message
from ..utils import write_audit

router = APIRouter(prefix="/api/management-todos", tags=["管理层待办"])

_CN_TZ = timezone(timedelta(hours=8))


def _cn_today() -> date:
    return datetime.now(_CN_TZ).date()


def _is_overdue(t: models.ManagementTodoTarget, today: Optional[date] = None) -> bool:
    """承诺日已过且未完成 = 逾期（未回复承诺时间的 pending 不算 overdue，另有「待回复」态）。"""
    if t.status == "done" or not t.committed_at:
        return False
    today = today or _cn_today()
    try:
        return date.fromisoformat(t.committed_at) < today
    except (ValueError, TypeError):
        return False


def _uname(u: Optional[models.User]) -> Optional[str]:
    if not u:
        return None
    return u.full_name or u.username


def _target_out(t: models.ManagementTodoTarget, today: date) -> schemas.MgmtTodoTargetOut:
    return schemas.MgmtTodoTargetOut(
        id=t.id, user_id=t.user_id, user_name=_uname(t.user), status=t.status,
        committed_at=t.committed_at, progress=t.progress, reply_at=t.reply_at,
        done_at=t.done_at, overdue=_is_overdue(t, today),
        extend_status=t.extend_status, extend_to=t.extend_to, extend_reason=t.extend_reason,
    )


def _todo_out(todo: models.ManagementTodo, today: date) -> schemas.MgmtTodoOut:
    targets = [_target_out(t, today) for t in todo.targets]
    return schemas.MgmtTodoOut(
        id=todo.id, title=todo.title, content=todo.content, priority=todo.priority,
        created_by=todo.created_by, creator_name=_uname(todo.creator),
        created_at=todo.created_at, targets=targets,
        total=len(targets),
        done_count=sum(1 for x in targets if x.status == "done"),
        overdue_count=sum(1 for x in targets if x.overdue),
        pending_reply_count=sum(1 for x in targets if x.status == "pending"),
    )


def _my_row(t: models.ManagementTodoTarget, today: date) -> schemas.MyTodoRow:
    todo = t.todo
    return schemas.MyTodoRow(
        target_id=t.id, todo_id=t.todo_id, title=todo.title, content=todo.content,
        priority=todo.priority, creator_name=_uname(todo.creator), created_at=todo.created_at,
        status=t.status, committed_at=t.committed_at, progress=t.progress,
        done_at=t.done_at, overdue=_is_overdue(t, today),
        extend_status=t.extend_status, extend_to=t.extend_to, extend_reason=t.extend_reason,
    )


# ==================== 管理层：建待办 / 监控 ====================
@router.post("", response_model=schemas.MgmtTodoOut)
@router.post("/", response_model=schemas.MgmtTodoOut, include_in_schema=False)
async def create_todo(
    body: schemas.MgmtTodoCreate,
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    rid_set = list(dict.fromkeys(body.recipient_ids))  # 去重保序
    # 校验收件人存在且启用
    res = await db.execute(
        select(models.User).where(models.User.id.in_(rid_set), models.User.is_active == True))  # noqa: E712
    users = {u.id: u for u in res.scalars().all()}
    valid_ids = [rid for rid in rid_set if rid in users]
    if not valid_ids:
        raise HTTPException(400, "请选择有效的收件人")

    todo = models.ManagementTodo(
        title=title, content=(body.content or "").strip() or None,
        priority=("urgent" if body.priority == "urgent" else "normal"),
        created_by=current.id,
    )
    todo.targets = [models.ManagementTodoTarget(user_id=rid, status="pending") for rid in valid_ids]
    db.add(todo)
    await db.commit()
    await db.refresh(todo)

    # 通知每位收件人回复承诺完成时间
    tag = "【紧急】" if todo.priority == "urgent" else "【管理层待办】"
    for rid in valid_ids:
        await push_message(
            db, to_user_id=rid, kind="warn",
            text=f"{tag}{_uname(current)} 给你下达待办「{title}」，请尽快回复承诺完成时间。",
            biz_type="mgmt_todo", biz_id=todo.id)
    await write_audit(db, user=current, action="create", target_type="management_todo",
                      target_id=todo.id, detail=f"下发待办「{title}」给 {len(valid_ids)} 人")

    # 重新查回（带 targets/creator）
    return await _get_todo_out(db, todo.id)


async def _get_todo_out(db: AsyncSession, todo_id: int) -> schemas.MgmtTodoOut:
    res = await db.execute(
        select(models.ManagementTodo).where(models.ManagementTodo.id == todo_id))
    todo = res.scalar_one_or_none()
    if not todo:
        raise HTTPException(404, "待办不存在")
    return _todo_out(todo, _cn_today())


@router.get("/sent", response_model=list[schemas.MgmtTodoOut])
async def list_sent(
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """管理层监控：全部已下发的待办 + 每人处理态（最新在前）。"""
    res = await db.execute(
        select(models.ManagementTodo).order_by(models.ManagementTodo.created_at.desc()))
    today = _cn_today()
    return [_todo_out(t, today) for t in res.scalars().all()]


@router.delete("/{todo_id}", response_model=schemas.Msg)
async def delete_todo(
    todo_id: int,
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(models.ManagementTodo).where(models.ManagementTodo.id == todo_id))
    todo = res.scalar_one_or_none()
    if not todo:
        raise HTTPException(404, "待办不存在")
    await db.execute(sa_delete(models.ManagementTodo).where(models.ManagementTodo.id == todo_id))
    await db.commit()
    await write_audit(db, user=current, action="delete", target_type="management_todo",
                      target_id=todo_id, detail=f"撤销待办「{todo.title}」")
    return schemas.Msg(message="已撤销")


# ==================== 收件人：我收到的 ====================
def _mine_stmt(uid: int):
    return (
        select(models.ManagementTodoTarget)
        .options(
            joinedload(models.ManagementTodoTarget.todo).joinedload(models.ManagementTodo.creator),
            joinedload(models.ManagementTodoTarget.user),
        )
        .where(models.ManagementTodoTarget.user_id == uid)
    )


@router.get("/mine", response_model=list[schemas.MyTodoRow])
async def list_mine(
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(_mine_stmt(current.id))
    rows = list(res.scalars().all())
    today = _cn_today()
    # 未完成在前；其中待回复/逾期优先，其余按创建时间倒序
    def _order(t: models.ManagementTodoTarget):
        done = 1 if t.status == "done" else 0
        urgent = 0 if (t.status == "pending" or _is_overdue(t, today)) else 1
        return (done, urgent, -t.todo.created_at.timestamp())
    rows.sort(key=_order)
    return [_my_row(t, today) for t in rows]


@router.get("/mine/count")
async def my_count(
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """浮动挂件角标：需我处理的条数 = 待回复承诺时间 + 已逾期未完成。"""
    res = await db.execute(_mine_stmt(current.id))
    today = _cn_today()
    n = 0
    for t in res.scalars().all():
        if t.status == "done":
            continue
        if t.status == "pending" or _is_overdue(t, today):
            n += 1
    return {"count": n}


async def _my_target(db: AsyncSession, target_id: int, uid: int) -> models.ManagementTodoTarget:
    res = await db.execute(
        select(models.ManagementTodoTarget)
        .options(joinedload(models.ManagementTodoTarget.todo).joinedload(models.ManagementTodo.creator),
                 joinedload(models.ManagementTodoTarget.user))
        .where(models.ManagementTodoTarget.id == target_id))
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(404, "待办不存在")
    if t.user_id != uid:
        raise HTTPException(403, "这不是你的待办")
    return t


@router.post("/{target_id}/reply", response_model=schemas.MyTodoRow)
async def reply_commit(
    target_id: int,
    body: schemas.MgmtTodoReplyIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """收件人回复承诺完成时间（可再次调整；已完成的不再改）。"""
    t = await _my_target(db, target_id, current.id)
    if t.status == "done":
        raise HTTPException(400, "该待办已完成")
    try:
        date.fromisoformat(body.committed_at)
    except (ValueError, TypeError):
        raise HTTPException(400, "承诺日期格式应为 YYYY-MM-DD")
    first = t.reply_at is None
    t.committed_at = body.committed_at
    t.status = "committed"
    if body.progress is not None:
        t.progress = body.progress.strip() or None
    if first:
        t.reply_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(t)
    verb = "承诺" if first else "更新承诺"
    await push_message(
        db, to_user_id=t.todo.created_by, kind="info",
        text=f"{_uname(current)} {verb}「{t.todo.title}」于 {body.committed_at} 前完成。",
        biz_type="mgmt_todo", biz_id=t.todo_id)
    return _my_row(t, _cn_today())


@router.post("/{target_id}/progress", response_model=schemas.MyTodoRow)
async def update_progress(
    target_id: int,
    body: schemas.MgmtTodoProgressIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = await _my_target(db, target_id, current.id)
    t.progress = (body.progress or "").strip() or None
    await db.commit()
    await db.refresh(t)
    return _my_row(t, _cn_today())


@router.post("/{target_id}/done", response_model=schemas.MyTodoRow)
async def mark_done(
    target_id: int,
    body: Optional[schemas.MgmtTodoProgressIn] = None,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = await _my_target(db, target_id, current.id)
    if t.status == "done":
        return _my_row(t, _cn_today())
    if body and body.progress is not None:
        t.progress = body.progress.strip() or None
    t.status = "done"
    t.done_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(t)
    await push_message(
        db, to_user_id=t.todo.created_by, kind="info",
        text=f"{_uname(current)} 已完成待办「{t.todo.title}」。",
        biz_type="mgmt_todo", biz_id=t.todo_id)
    return _my_row(t, _cn_today())


@router.post("/{target_id}/extend", response_model=schemas.MyTodoRow)
async def request_extend(
    target_id: int,
    body: schemas.MgmtTodoExtendIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """收件人申请顺延承诺日（需管理层同意）。"""
    t = await _my_target(db, target_id, current.id)
    if t.status == "done":
        raise HTTPException(400, "该待办已完成，无需顺延")
    if t.status != "committed" or not t.committed_at:
        raise HTTPException(400, "请先回复承诺完成时间，再申请顺延")
    try:
        date.fromisoformat(body.extend_to)
    except (ValueError, TypeError):
        raise HTTPException(400, "顺延日期格式应为 YYYY-MM-DD")
    t.extend_status = "pending"
    t.extend_to = body.extend_to
    t.extend_reason = body.reason.strip()
    t.extend_decided_by = None
    t.extend_decided_at = None
    await db.commit()
    await db.refresh(t)
    await push_message(
        db, to_user_id=t.todo.created_by, kind="warn",
        text=f"{_uname(current)} 申请把「{t.todo.title}」顺延到 {body.extend_to}"
             f"（原 {t.committed_at}）：{t.extend_reason}，请审批。",
        biz_type="mgmt_todo_extend", biz_id=t.todo_id)
    return _my_row(t, _cn_today())


@router.post("/{target_id}/extend/decide", response_model=schemas.MgmtTodoTargetOut)
async def decide_extend(
    target_id: int,
    body: schemas.MgmtTodoExtendDecideIn,
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """管理层审批顺延申请：同意则改承诺日，否则维持原承诺日。"""
    res = await db.execute(
        select(models.ManagementTodoTarget)
        .options(joinedload(models.ManagementTodoTarget.todo).joinedload(models.ManagementTodo.creator),
                 joinedload(models.ManagementTodoTarget.user))
        .where(models.ManagementTodoTarget.id == target_id))
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(404, "待办不存在")
    if t.extend_status != "pending" or not t.extend_to:
        raise HTTPException(400, "没有待审批的顺延申请")
    new_date = t.extend_to
    t.extend_decided_by = current.id
    t.extend_decided_at = datetime.now(timezone.utc)
    if body.approve:
        t.committed_at = new_date
        t.extend_status = "approved"
        msg = f"管理层已同意「{t.todo.title}」顺延到 {new_date}。"
    else:
        t.extend_status = "rejected"
        msg = f"管理层未同意「{t.todo.title}」顺延{'：' + body.note if body.note else ''}，请按原承诺日 {t.committed_at} 完成。"
    await db.commit()
    await db.refresh(t)
    await push_message(db, to_user_id=t.user_id, kind="warn", text=msg,
                       biz_type="mgmt_todo_extend", biz_id=t.todo_id)
    return _target_out(t, _cn_today())
