"""质检部工作台。任务以已交接的装配任务为准。"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models, schemas
from ..database import get_db
from ..deps import require_roles
from ..menus import has_capability
from ..production_quality import quality_queue_conditions
from ..utils import write_audit

router = APIRouter(prefix="/api/quality", tags=["质检部"])


async def require_quality(current: models.User = Depends(require_roles("quality"))) -> models.User:
    if not has_capability(current, "quality"):
        raise HTTPException(403, "质检部菜单未开通")
    return current


class QualityRow(BaseModel):
    task_id: int
    code: str
    name: str
    assembly_done_at: datetime | None
    quality_done_at: datetime | None
    quality_done_by: str | None


@router.get("/tasks", response_model=list[QualityRow])
async def list_tasks(
    _: models.User = Depends(require_quality),
    db: AsyncSession = Depends(get_db),
):
    task = models.ProduceGroupTask
    user = models.User
    stmt = (
        select(task, func.coalesce(func.nullif(user.full_name, ""), user.username))
        .outerjoin(user, user.id == task.quality_done_by)
        .where(*quality_queue_conditions())
        .order_by(task.done_at.desc().nullslast(), task.id.desc())
    )
    return [
        QualityRow(
            task_id=t.id, code=t.project.code, name=t.project.name,
            assembly_done_at=_as_utc(t.done_at), quality_done_at=_as_utc(t.quality_done_at),
            quality_done_by=inspector,
        )
        for t, inspector in (await db.execute(stmt)).all()
    ]


def _as_utc(timestamp: datetime | None) -> datetime | None:
    if timestamp is not None and timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp


class QualityDoneIn(BaseModel):
    done: bool


@router.post("/tasks/{task_id}/done", response_model=schemas.Msg)
async def mark_done(
    task_id: int,
    data: QualityDoneIn,
    current: models.User = Depends(require_quality),
    db: AsyncSession = Depends(get_db),
):
    task = models.ProduceGroupTask
    stmt = select(task).where(task.id == task_id, *quality_queue_conditions()).with_for_update(of=task)
    t = (await db.execute(stmt)).scalar_one_or_none()
    if t is None:
        raise HTTPException(404, "该任务未进入质检或已撤回")
    if (t.quality_done_at is not None) == data.done:
        return schemas.Msg(message="质检已完成" if data.done else "该任务待质检")

    t.quality_done_at = datetime.now(timezone.utc) if data.done else None
    t.quality_done_by = current.id if data.done else None
    await db.commit()
    message = "已标记质检完成" if data.done else "已撤销质检完成"
    await write_audit(db, user=current, action="quality_done", target_type="produce_group_task",
                      target_id=t.id, detail=message)
    return schemas.Msg(message=message)
