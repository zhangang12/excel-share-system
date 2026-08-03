"""回款登记卡（type = ledger_settle）。

为什么做这张：查询工具 `receivable_blind` 找出了 49 笔 ¥253 万无人管的应收，
但光看没用——收到钱得回电脑上销账。这张卡把「查出来」和「销掉」闭在一屏里。

点「已收款」走的是 `PUT /api/sales/ledger/{id}/payment-note`，
跟他在台账页点批注是同一个端点、同一个 token，后端区分不出也不需要区分。

副作用（代码在 sales_router.update_payment_note）：
  field=balance      → balance 清零，原值存进 balance_contract，催办立即停
  field=before_ship  → ship_receivable 清零
两者都**可逆**：删掉批注即恢复原值。这条路生产上从未在有值时执行过
（balance_contract 全库 0 条），2026-08-03 已在本地跑通往返验证才敢做这张卡。
"""
from datetime import date, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ... import models
from ...routers.sales_router import _all_view
from . import token as card_token

_MAX_CARDS = 20        # 一次最多发这么多张；再多手机上划不完


def _money(x) -> str:
    return f"¥{float(x or 0):,.2f}"


def _age(d: datetime | None) -> int | None:
    return (date.today() - d.date()).days if d else None


async def blind_ledgers(db: AsyncSession, current: models.User,
                        refs: list[int] | None = None) -> list[models.SalesLedger]:
    """口径与 tools_sales.tool_receivable_blind 完全一致——查得到就要能销账，
    两处口径不一样会出现「卡片里有、点了说找不到」这种最伤信任的情况。"""
    q = (select(models.SalesLedger)
         .join(models.Project, models.Project.id == models.SalesLedger.project_id)
         .where(models.Project.is_deleted == False,  # noqa: E712
                or_(models.SalesLedger.ship_receivable > 0,
                    and_(models.SalesLedger.balance > 0,
                         or_(models.SalesLedger.balance_date.is_(None),
                             models.SalesLedger.balance_date == "")))))
    if not _all_view(current):
        q = q.where(models.SalesLedger.sales_uid == current.id)
    if refs is not None:
        q = q.where(models.SalesLedger.id.in_(refs))
    return list((await db.execute(q)).scalars().all())


async def assemble_settle_cards(db: AsyncSession, current: models.User,
                                refs: list[int] | None = None) -> list[dict]:
    """装配回款登记卡。

    flags 逐条对应 update_payment_note 里的 raise（手册 3.5.3）：
      sales_router.py:600  field 白名单        → 由 registry 的 action key 保证，不需 flag
      sales_router.py:604  非本人负责的台账行  → not_mine (block)
    """
    leds = await blind_ledgers(db, current, refs)
    leds.sort(key=lambda l: -((l.ship_receivable or 0) + (l.balance or 0)))

    cards = []
    for led in leds[:_MAX_CARDS]:
        p = led.project
        flags: list[dict] = []
        # 行级隔离：与端点同一条判断，前端据此置灰，别让人点了才吃 403
        if not _all_view(current) and led.sales_uid != current.id:
            flags.append({"code": "not_mine", "level": "block",
                          "msg": "这不是你负责的台账行，只能由负责销售登记"})

        actions = []
        # 明细要够判断：光有「客户 + 金额」没法决定要不要销账。
        # 补上合同额与已收的两笔，他一眼能看出这笔应收在整单里占多少、收到哪一步了。
        facts = [{"k": "客户", "v": led.customer or "—"},
                 {"k": "项目", "v": p.code if p else f"#{led.project_id}"}]
        if led.amount:
            facts.append({"k": "合同额", "v": _money(led.amount)})
        paid = []
        if led.prepay:
            paid.append(f"预付 {_money(led.prepay)}"
                        + ("（已收）" if led.prepay_note else ""))
        if led.before_ship:
            paid.append(f"发货前付 {_money(led.before_ship)}"
                        + ("（已收）" if led.before_ship_note else ""))
        if paid:
            facts.append({"k": "收款条款", "v": " · ".join(paid)})

        if (led.ship_receivable or 0) > 0:
            facts.append({"k": "发货款应收", "v": _money(led.ship_receivable),
                          "emphasis": True})
            actions.append({"key": "settle_ship", "primary": True,
                            "disabled_by": "not_mine" if flags else None,
                            "needs_reason": True})
        if (led.balance or 0) > 0 and not (led.balance_date or "").strip():
            facts.append({"k": "尾款", "v": _money(led.balance),
                          "emphasis": not any(f.get("emphasis") for f in facts)})
            actions.append({"key": "settle_balance", "primary": not actions,
                            "disabled_by": "not_mine" if flags else None,
                            "needs_reason": True})
            flags.append({"code": "no_due_date", "level": "warn",
                          "msg": "没填尾款到期日，催办扫不到这一笔"})

        age = _age(led.created_at)
        if age is not None:
            facts.append({"k": "建档至今", "v": f"{age} 天"})
        if led.sales_user:
            facts.append({"k": "负责销售",
                          "v": led.sales_user.full_name or led.sales_user.username})

        cards.append({
            "type": "ledger_settle",
            "ref": led.id,
            "token": card_token.issue(current.id, "ledger_settle", led.id),
            "facts": facts,
            "flags": flags,
            "actions": actions,
        })
    return cards


async def summarize(db: AsyncSession, current: models.User) -> dict:
    """汇总口径与 assemble_settle_cards 同源（都走 blind_ledgers）。

    做这个是因为一次弹 20 张卡没法看——先给一张「总账」，
    点开才逐条处理。合计要基于**全量**而不是截断后的前 20 条，
    否则「共 ¥253 万」和列表加起来对不上。
    """
    leds = await blind_ledgers(db, current)
    ship = [l for l in leds if (l.ship_receivable or 0) > 0]
    bal = [l for l in leds if (l.balance or 0) > 0 and not (l.balance_date or "").strip()]
    ages = [a for a in (_age(l.created_at) for l in leds) if a is not None]
    return {
        "count": len(leds),
        "total": round(sum((l.ship_receivable or 0) + (l.balance or 0) for l in leds), 2),
        "groups": [
            {"key": "ship", "label": "发货款应收", "count": len(ship),
             "amount": round(sum(l.ship_receivable or 0 for l in ship), 2),
             "note": "全系统没有任何提醒碰过这个字段"},
            {"key": "balance", "label": "尾款·没填到期日", "count": len(bal),
             "amount": round(sum(l.balance or 0 for l in bal), 2),
             "note": "催办按到期日扫，没填的一条都扫不到"},
        ],
        "oldest_days": max(ages) if ages else 0,
        "shown": min(len(leds), _MAX_CARDS),
    }
