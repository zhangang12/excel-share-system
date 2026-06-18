"""🆕 v3 站内消息推送服务（企微通道降级兜底）。

- push_message(to_user_id=...) 单人推送
- push_message(to_role=...)    角色池推送：写入时按角色扇出成每用户一行
  （替代原型硬编码个人 lo1/sm1/fin1-3；已读状态天然按人独立）
- 企微通道：用户已绑 wxid 且 settings.wecom_* 配置齐 → 尝试外发；
  失败/未配置仅记日志，绝不阻塞主业务事务（F3 口径）。

调用约定：在业务事务 commit **之后**调用（避免幻影通知）；本服务自行 commit。
"""
import logging
from typing import Optional

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from . import models
from .config import settings

log = logging.getLogger("notify")


async def push_message(
    db: AsyncSession,
    *,
    to_user_id: Optional[int] = None,
    to_role: Optional[str] = None,
    kind: str = "info",          # wx / warn / info
    text: str,
    biz_type: Optional[str] = None,
    biz_id: Optional[int] = None,
) -> int:
    """推送站内消息。返回写入条数。to_user_id 与 to_role 至少给一个。"""
    user_ids: list[int] = []
    if to_user_id is not None:
        user_ids.append(to_user_id)
    if to_role:
        # 多角色：锚点 role_id 命中 或 user_roles 关联命中任一即推送
        # （否则「该角色仅为副角色」的用户收不到按角色的待办/提醒推送）
        rid = (await db.execute(
            select(models.Role.id).where(models.Role.code == to_role))).scalar_one_or_none()
        if rid is not None:
            sub = select(models.UserRole.user_id).where(models.UserRole.role_id == rid)
            res = await db.execute(
                select(models.User.id).where(
                    models.User.is_active == True,  # noqa: E712
                    or_(models.User.role_id == rid, models.User.id.in_(sub)),
                )
            )
            user_ids.extend(r[0] for r in res.all())
    if not user_ids:
        log.info("push_message: 角色 %s 无在线用户，消息丢弃: %s", to_role, text[:50])
        return 0

    seen: set[int] = set()
    rows = []
    for uid in user_ids:
        if uid in seen:
            continue
        seen.add(uid)
        rows.append(models.Message(
            to_user_id=uid, kind=kind, text=text,
            biz_type=biz_type, biz_id=biz_id,
        ))
    db.add_all(rows)
    await db.commit()

    # 企微外发（尽力而为，不影响站内）
    if settings.wecom_corp_id and settings.wecom_secret:
        try:
            await _send_wecom(db, list(seen), text)
        except Exception as e:  # noqa: BLE001
            log.warning("企微推送失败（站内消息已落）: %s", e)
    return len(rows)


async def _send_wecom(db: AsyncSession, user_ids: list[int], text: str) -> None:
    """企微应用消息外发占位：取已绑 wxid 的用户调企微 API。
    凭证/网络未就绪时由调用方捕获降级。"""
    res = await db.execute(
        select(models.User.wxid).where(
            models.User.id.in_(user_ids), models.User.wxid.isnot(None),
        )
    )
    wxids = [r[0] for r in res.all() if r[0]]
    if not wxids:
        return
    # TODO(企微凭证就绪后): httpx 调 gettoken + message/send；当前仅记录
    log.info("[企微占位] 将推送给 %s: %s", "|".join(wxids), text[:80])
