"""销售订单审批卡（type = sales_order_approve）。

杨坛 2 个月做过 9 次订单审批。量不大但流程完整、规则清楚，
且端点 `POST /api/sales/ledger/{id}/order-approve` 已经跑了几个月。
"""
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ... import models
from ...routers.sales_router import _all_view
from . import token as card_token

_MAX_CARDS = 20


def _money(x) -> str:
    return f"¥{float(x or 0):,.2f}"


def _age(d: datetime | None) -> int | None:
    return (date.today() - d.date()).days if d else None


async def pending_orders(db: AsyncSession, current: models.User,
                         refs: list[int] | None = None) -> list[models.SalesLedger]:
    q = (select(models.SalesLedger)
         .join(models.Project, models.Project.id == models.SalesLedger.project_id)
         .where(models.Project.is_deleted == False,  # noqa: E712
                models.SalesLedger.order_state == "pending"))
    if not _all_view(current):
        q = q.where(models.SalesLedger.sales_uid == current.id)
    if refs is not None:
        q = q.where(models.SalesLedger.id.in_(refs))
    return list((await db.execute(q)).scalars().all())


async def assemble_order_cards(db: AsyncSession, current: models.User,
                               refs: list[int] | None = None) -> list[dict]:
    """flags 对应端点的前置校验：
      sales_router.py  order_state != 'pending'  → not_pending (block)
      require_roles("sales_lead")                → 非主管/管理层看不到这类卡（_allowed 已挡）
    """
    cards = []
    for led in (await pending_orders(db, current, refs))[:_MAX_CARDS]:
        p = led.project
        flags: list[dict] = []
        if led.order_state != "pending":
            flags.append({"code": "not_pending", "level": "block",
                          "msg": f"该单当前状态为「{led.order_state or '—'}」，不在待审批状态"})
        blocked = bool(flags)
        facts = [{"k": "项目", "v": p.code if p else f"#{led.project_id}"},
                 {"k": "客户", "v": led.customer or "—"},
                 {"k": "合同额", "v": _money(led.amount), "emphasis": True}]
        age = _age(led.created_at)
        if age is not None:
            facts.append({"k": "提交至今", "v": f"{age} 天"})
        if led.sales_user:
            facts.append({"k": "下单销售",
                          "v": led.sales_user.full_name or led.sales_user.username})
        if not (led.amount or 0):
            flags.append({"code": "no_amount", "level": "warn",
                          "msg": "合同额为 0，批下去会让这个项目的毛利算成假亏损"})
        cards.append({
            "type": "sales_order_approve",
            "ref": led.id,
            "token": card_token.issue(current.id, "sales_order_approve", led.id),
            "facts": facts,
            "flags": flags,
            "actions": [
                {"key": "approve", "primary": True,
                 "disabled_by": "not_pending" if blocked else None},
                {"key": "reject", "primary": False, "needs_reason": True,
                 "disabled_by": "not_pending" if blocked else None},
            ],
        })
    return cards
