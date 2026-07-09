"""🆕 v3 站内消息：列表 / 未读数 / 标记已读。"""
from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user

router = APIRouter(prefix="/api/messages", tags=["消息"])


@router.get("", response_model=List[schemas.MessageOut])
async def list_messages(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(models.Message)
        .where(models.Message.to_user_id == current.id)
        # #179：按推送时间倒序（最新在最上），同一时刻再按 id 兜底
        .order_by(models.Message.created_at.desc(), models.Message.id.desc())
        .limit(limit).offset(offset)
    )
    return [schemas.MessageOut.model_validate(m) for m in res.scalars().all()]


@router.get("/unread-count", response_model=schemas.UnreadCountOut)
async def unread_count(
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(func.count(models.Message.id)).where(
            models.Message.to_user_id == current.id,
            models.Message.read == False,  # noqa: E712
        )
    )
    return schemas.UnreadCountOut(count=res.scalar() or 0)


@router.post("/read-all", response_model=schemas.Msg)
async def read_all(
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        update(models.Message)
        .where(models.Message.to_user_id == current.id,
               models.Message.read == False)  # noqa: E712
        .values(read=True)
    )
    await db.commit()
    return schemas.Msg(message="已全部标为已读")
