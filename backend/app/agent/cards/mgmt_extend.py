"""🆕 管理层待办·顺延申请审批卡（type = mgmt_todo_extend）。

**为什么先做这一类**：生产库里挂着 7 条 `extend_status='pending'` 的顺延申请，
一条都没批过。收件人申请把承诺日往后挪、以为报上去了，管理层压根没看见——
整个「下发待办」闭环就断在这一环。

对应端点：`POST /api/management-todos/{target_id}/extend/decide`
⚠️ **ref 是 target_id（某个人对某条待办的处理行），不是 todo_id**。
   一条待办发给多个人时每人一行，各自独立申请顺延；用 todo_id 会批错人。
"""
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from ... import models
from . import token as card_token

_MAX_CARDS = 20


def _days_from_today(d: str | None) -> int | None:
    """YYYY-MM-DD → 距今天几天（正=还早，负=已过）。解析不了就返回 None。"""
    if not d:
        return None
    try:
        return (date.fromisoformat(d) - date.today()).days
    except (ValueError, TypeError):
        return None


async def pending_extends(db: AsyncSession, current: models.User,
                          refs: list[int] | None = None
                          ) -> list[models.ManagementTodoTarget]:
    """待审批的顺延申请。

    ⚠️ 只取 `extend_status='pending'` **且** `extend_to` 有值的：
       端点的前置校验就是这两条，装配时的口径必须和它一致，
       否则卡片上有按钮、点下去 400。
    """
    # ⚠️⚠️ **必须在这里挡住非管理层**。`GET /agent/cards/{type}` 只做登录校验，
    #    权限一律由各 assembler 自己按 current 过滤（pay_req 按销售归属、
    #    ledger_settle 按可见范围）。这一类没有「归属人」的概念——
    #    对应的动作端点是 `require_admin_or_manager`，装配这边不加同样的闸，
    #    任何登录用户都能读到全部顺延申请：谁在拖、事项标题、他写的说明。
    #    批不了但看得见，照样是越权。
    if not current.has_role("admin", "manager"):
        return []
    q = (select(models.ManagementTodoTarget)
         .options(joinedload(models.ManagementTodoTarget.todo)
                  .joinedload(models.ManagementTodo.creator),
                  joinedload(models.ManagementTodoTarget.user))
         .where(models.ManagementTodoTarget.extend_status == "pending",
                models.ManagementTodoTarget.extend_to.isnot(None))
         .order_by(models.ManagementTodoTarget.id.desc()))
    if refs is not None:
        q = q.where(models.ManagementTodoTarget.id.in_(refs))
    return list((await db.execute(q)).scalars().all())


async def assemble_extend_cards(db: AsyncSession, current: models.User,
                                refs: list[int] | None = None) -> list[dict]:
    """flags 逐条对上端点的 raise（management_todo_router.decide_extend）：

      · `extend_status != 'pending' or not extend_to` → 400「没有待审批的顺延申请」
      · `require_admin_or_manager`                    → 非管理层根本取不到这类卡

    ⚠️ 顺延**往前挪**（新日期比原承诺日还早）要单独标出来：这多半是填错了，
       批下去等于替对方把 deadline 提前，没人会想要这个结果。
    """
    cards = []
    for t in (await pending_extends(db, current, refs))[:_MAX_CARDS]:
        todo = t.todo
        who = t.user.full_name or t.user.username if t.user else f"#{t.user_id}"
        flags: list[dict] = []
        if t.extend_status != "pending" or not t.extend_to:
            flags.append({"code": "no_pending_extend", "level": "block",
                          "msg": "这条已经没有待审批的顺延申请了（可能刚被别人批过）"})
        blocked = bool(flags)

        facts = [
            {"k": "谁", "v": who},
            {"k": "事项", "v": (todo.title if todo else f"#{t.todo_id}")[:40]},
            {"k": "原承诺", "v": t.committed_at or "—"},
            {"k": "申请改到", "v": t.extend_to or "—", "emphasis": True},
        ]
        over = _days_from_today(t.committed_at)
        if over is not None and over < 0:
            facts.append({"k": "原承诺已过", "v": f"{-over} 天"})
        # ⚠️ 申请理由是 `extend_reason`，**不是 `progress`**。
        #    progress 是他平时报的进展，跟「为什么要顺延」是两回事——
        #    取错了就等于把无关的话当成申请理由摆在决策卡上。
        if t.extend_reason:
            facts.append({"k": "申请理由", "v": t.extend_reason[:60]})
        elif t.progress:
            facts.append({"k": "最近进展", "v": t.progress[:60]})

        # 顺延往前挪：极可能是填错日期
        if t.committed_at and t.extend_to and t.extend_to < t.committed_at:
            flags.append({"code": "extend_backwards", "level": "warn",
                          "msg": f"申请的新日期 {t.extend_to} 比原承诺 {t.committed_at} 还早，"
                                 f"多半是填错了，批之前先问一句"})
        # 顺延到的日期已经过了：批了也立刻又是逾期
        ahead = _days_from_today(t.extend_to)
        if ahead is not None and ahead < 0:
            flags.append({"code": "extend_into_past", "level": "warn",
                          "msg": f"申请顺延到的 {t.extend_to} 也已经过去了，批完还是逾期"})

        cards.append({
            "type": "mgmt_todo_extend",
            # ⚠️ target_id，不是 todo_id —— 一条待办发给多人时每人一行
            "ref": t.id,
            "token": card_token.issue(current.id, "mgmt_todo_extend", t.id),
            "facts": facts,
            "flags": flags,
            "actions": [
                {"key": "approve", "primary": True,
                 "disabled_by": "no_pending_extend" if blocked else None},
                {"key": "reject", "primary": False, "needs_reason": True,
                 "disabled_by": "no_pending_extend" if blocked else None},
            ],
        })
    return cards
