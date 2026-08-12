"""🆕 反馈#363/#381/#382 个人待办：自己给自己记的事，只有自己看得见。

和「管理层待办」是两回事，别混（合表的坑见 models.PersonalTodo 的注释）：
管理层待办是别人交办、要回承诺时间、要留痕；个人待办随手记随手删、没有交代。

⚠️ **本文件每一个按 id 操作的接口都必须带 `user_id == current.id`**。
   只在列表接口过滤、详情/改/删按 id 直接取，是最典型的越权口子——
   换个 id 就能改别人的待办。下面统一走 `_own()` 取数据，不要绕过它。

业务确认（2026-08-12）：要挂项目、要紧急档、到期当天推一次企微、
右下角角标 = 管理层待办未回复 + 个人待办未完成（合成一个数，见 management_todo_router）。
"""
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, delete as sa_delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user

router = APIRouter(prefix="/api/personal-todos", tags=["个人待办"])


def _out(t: models.PersonalTodo, today: str) -> schemas.PersonalTodoOut:
    return schemas.PersonalTodoOut(
        id=t.id, title=t.title, note=t.note, due_date=t.due_date,
        priority=t.priority or "normal",
        project_id=t.project_id, project_code=(t.project.code if t.project else None),
        done=bool(t.done), done_at=t.done_at, sort_order=t.sort_order or 0,
        overdue=bool(not t.done and t.due_date and t.due_date < today),
        created_at=t.created_at,
    )


async def _own(db: AsyncSession, tid: int, uid: int) -> models.PersonalTodo:
    """按 id 取**自己的**待办；取不到一律 404（不区分"不存在"和"别人的"，免得探测）。"""
    r = await db.execute(select(models.PersonalTodo).where(
        models.PersonalTodo.id == tid, models.PersonalTodo.user_id == uid))
    t = r.scalar_one_or_none()
    if not t:
        raise HTTPException(404, "待办不存在")
    return t


def _norm_date(s: Optional[str]) -> Optional[str]:
    v = (s or "").strip()
    if not v:
        return None
    try:
        return date.fromisoformat(v[:10]).isoformat()
    except ValueError:
        raise HTTPException(400, f"日期格式不对：{v}（要 2026-08-20 这样）")


@router.get("", response_model=list[schemas.PersonalTodoOut])
async def list_mine(
    done: Optional[bool] = Query(None, description="不传=全部；false=只看未完成"),
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """我的个人待办。排序：未完成在前 → 手工排序 → 新的在前。"""
    q = select(models.PersonalTodo).where(models.PersonalTodo.user_id == current.id)
    if done is not None:
        q = q.where(models.PersonalTodo.done == done)
    q = q.order_by(models.PersonalTodo.done,
                   models.PersonalTodo.sort_order,
                   models.PersonalTodo.id.desc())
    today = date.today().isoformat()
    return [_out(t, today) for t in (await db.execute(q)).scalars().all()]


@router.get("/count")
async def my_count(
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """未完成条数（给右下角角标用）。"""
    n = (await db.execute(select(func.count(models.PersonalTodo.id)).where(
        models.PersonalTodo.user_id == current.id,
        models.PersonalTodo.done == False))).scalar() or 0   # noqa: E712
    return {"count": int(n)}


@router.post("", response_model=schemas.PersonalTodoOut)
async def create(
    body: schemas.PersonalTodoIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """新建。只有 title 必填——个人待办的成败全在录入成本，别加必填项。"""
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(400, "请填写待办内容")
    t = models.PersonalTodo(
        user_id=current.id, title=title, note=(body.note or "").strip() or None,
        due_date=_norm_date(body.due_date),
        priority=("urgent" if body.priority == "urgent" else "normal"),
        project_id=body.project_id, sort_order=0)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return _out(t, date.today().isoformat())


@router.put("/reorder", response_model=schemas.Msg)
async def reorder(
    body: schemas.PersonalTodoReorderIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """拖动排序：按传入顺序写 sort_order。
    ⚠️ 只认**自己名下**的 id，混进来别人的直接忽略（不是报错——前端传脏数据不该整批失败）。
    ⚠️ 本路由必须排在 `PUT /{tid}` **之前**，否则 "reorder" 会被当成 tid 解析成 422。
       （同采购路由 batch-expected-arrival 那个坑。）"""
    if not body.ids:
        return schemas.Msg(message="无变化")
    rows = {t.id: t for t in (await db.execute(select(models.PersonalTodo).where(
        models.PersonalTodo.id.in_(body.ids),
        models.PersonalTodo.user_id == current.id))).scalars().all()}
    n = 0
    for i, tid in enumerate(body.ids):
        t = rows.get(tid)
        if t:
            t.sort_order = i
            n += 1
    await db.commit()
    return schemas.Msg(message=f"已排序 {n} 条")


@router.put("/{tid}", response_model=schemas.PersonalTodoOut)
async def update(
    tid: int,
    body: schemas.PersonalTodoUpdate,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = await _own(db, tid, current.id)
    data = body.model_dump(exclude_unset=True)
    if "title" in data:
        title = (data["title"] or "").strip()
        if not title:
            raise HTTPException(400, "待办内容不能为空")
        t.title = title
    if "note" in data:
        t.note = (data["note"] or "").strip() or None
    if "due_date" in data:
        t.due_date = _norm_date(data["due_date"])
    if "priority" in data:
        t.priority = "urgent" if data["priority"] == "urgent" else "normal"
    if "project_id" in data:
        t.project_id = data["project_id"]      # 显式传 null 可摘掉项目
    await db.commit()
    await db.refresh(t)
    return _out(t, date.today().isoformat())


@router.post("/{tid}/toggle", response_model=schemas.PersonalTodoOut)
async def toggle(
    tid: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """打勾 / 取消打勾。手机上点一下就是这个接口。"""
    t = await _own(db, tid, current.id)
    t.done = not t.done
    t.done_at = datetime.now(timezone.utc) if t.done else None
    await db.commit()
    await db.refresh(t)
    return _out(t, date.today().isoformat())


@router.delete("/{tid}", response_model=schemas.Msg)
async def remove(
    tid: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除。个人的东西直接物理删——不做软删，没有留痕需求。"""
    t = await _own(db, tid, current.id)
    await db.execute(sa_delete(models.PersonalTodo).where(models.PersonalTodo.id == t.id))
    await db.commit()
    return schemas.Msg(message="已删除")
