"""🆕 下发待办·草稿确认卡（type = mgmt_todo_send）。

模型只能**拟**草稿（`tools_entity.mgmt_todo_send` 写一行 `AgentDraft`），
真正的写由用户点这张卡上的「确认发出」触发，打
`POST /api/agent/drafts/{draft_id}/send`。

⚠️ 为什么非得这么绕：**工具结果不进跨轮 history**。第一版做成
「工具返回确认码 → 下一轮模型带回来」，当场死循环——用户点了两次「确认」，
模型每次只能重新拟一份草稿，一条都发不出去（2026-08-19 生产实证）。
模型看不见上一轮的工具返回，任何靠它传递凭据的设计都不成立。
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ... import models
from . import token as card_token

_MAX_CARDS = 5
# 草稿只在眼前这一小会儿有效。放太久，用户回头看到一张旧卡随手一点，
# 发出去的是十分钟前那件事——他自己都未必记得了。
_TTL_MIN = 15


def _fresh_after() -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=_TTL_MIN)


async def pending_drafts(db: AsyncSession, current: models.User,
                         refs: list[int] | None = None) -> list[models.AgentDraft]:
    """当前用户**自己的**、没用过的、没过期的待办草稿。

    ⚠️ `user_id == current.id` 这条不能少：草稿里带着收件人和事项，
       别人的草稿既不该看见，更不该点得动。
    """
    q = (select(models.AgentDraft)
         .where(models.AgentDraft.user_id == current.id,
                models.AgentDraft.action == "mgmt_todo_send",
                models.AgentDraft.used_at.is_(None),
                models.AgentDraft.created_at >= _fresh_after())
         .order_by(models.AgentDraft.id.desc()))
    if refs is not None:
        q = q.where(models.AgentDraft.id.in_(refs))
    return list((await db.execute(q)).scalars().all())


def _due_text(due: str) -> str:
    if not due:
        return "没定期限"
    try:
        n = (date.fromisoformat(due) - date.today()).days
    except (ValueError, TypeError):
        return due
    if n == 0:
        return f"{due}（今天）"
    if n == 1:
        return f"{due}（明天）"
    return f"{due}（{n} 天后）" if n > 0 else f"{due}（已过去 {-n} 天）"


async def assemble_send_cards(db: AsyncSession, current: models.User,
                              refs: list[int] | None = None) -> list[dict]:
    """flags 对应 `send_draft` 端点的前置校验：

      · 草稿不属于当前用户 / 已用过 / 过期 → 端点 404 或 400，这里干脆不出卡
      · 收件人被停用                      → block，点了也发不出去（创建接口会滤掉）
    """
    if not current.has_role("admin", "manager"):
        return []
    cards = []
    for dft in (await pending_drafts(db, current, refs))[:_MAX_CARDS]:
        p = dft.payload or {}
        who = p.get("worker") or "—"
        flags: list[dict] = []

        u = await db.get(models.User, int(p.get("uid") or 0))
        if u is None or not u.is_active:
            flags.append({"code": "worker_gone", "level": "block",
                          "msg": f"{who} 已停用，发了也收不到，换个人"})
        blocked = bool(flags)

        facts = [
            # ⚠️ 收件人放第一行且 emphasis：发错人是这里唯一真正的风险
            {"k": "派给", "v": who, "emphasis": True},
            {"k": "事项", "v": str(p.get("title") or "")[:40]},
            {"k": "截止", "v": _due_text(str(p.get("due") or ""))},
            {"k": "紧急档", "v": "紧急" if p.get("urgent") else "普通"},
        ]
        if p.get("note"):
            facts.append({"k": "说明", "v": str(p["note"])[:60]})
        if not p.get("due"):
            flags.append({"code": "no_due", "level": "warn",
                          "msg": "没定完成期限。催办和逾期提醒都按期限走，"
                                 "不填的话这条永远不会提醒"})

        cards.append({
            "type": "mgmt_todo_send",
            "ref": dft.id,
            "token": card_token.issue(current.id, "mgmt_todo_send", dft.id),
            "facts": facts,
            "flags": flags,
            # 只有一个动作：发。不给「改一下」——改就是重新说一句，
            # 模型会拟一份新草稿，比在卡片上做编辑器简单得多。
            "actions": [
                {"key": "send", "primary": True,
                 "disabled_by": "worker_gone" if blocked else None},
            ],
        })
    return cards
