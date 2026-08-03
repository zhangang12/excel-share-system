"""每日简报：把「有多少」变成「今天该管哪几件、为什么是这几件」。

为什么做这个
------------
智能体原来只在被问时答，答的是数量：「49 笔 ¥253 万」。他得先记得打开、
还得知道该问什么；就算问了，49 笔平铺出来跟报表没区别，还是他自己挑。
生产上「催办」推过 83 条（balance_due 31 + balance_overdue 52）没解决问题——
**推的是清单不是判断**。所以这里只做两件事：排序、给理由。

⚠️ 拿真实生产数据跑过之后推翻了两版设计，踩过的坑记在这，别再犯：

1. **金额权重曾经用 log10** —— 结果 ¥22 万得 13.11 分、¥4.2 万得 11.34 分，
   差 5 倍金额只差 14% 分。排序实际由「天数×盲区」决定，而这两项对同一批
   数据几乎是常量，于是排序退化成随机。已改**线性**：同样没人管、同样账龄，
   当然先追大的，这也最好解释。

2. **账龄字段试了三个才落地**：
   - `sales_ledger.ship_date` —— 38 项里 **0 项**有值，没人填
   - `shipments.shipped_at` —— 全库 90 条只填了 **3 条**
   - `sales_ledger.created_at` —— 唯一全有值的，分布 6~48 天
   所以文案只说「**台账建了 N 天**」这个能站住的事实，**不说「欠了 N 天」
   「逾期 N 天」**——数据根本不支持，说了就是编。

3. **发货款应收的真问题不是催款** —— 35 张发货单里 **32 张还是 pending**。
   要么货没发（那就不该催客户），要么发了没人在系统里点确认。
   直接推「去要钱」是错的，得先让他分清是哪种。这条判断才是简报的价值，
   否则跟催办没区别。

排序
----
`score = 金额 × 时间系数 × 盲区加成`

- **时间**：`1 + min(天数, 90)/90`（1.0~2.0）。90 天封顶——挂 200 天和 300 天
  紧迫性没本质差别，不封顶老单会永远霸榜，新单永远排不上。
- **盲区加成 ×1.6**：`ship_receivable` 与「没填到期日的尾款」这两类，
  **全系统没有任何提醒扫得到**（催办按 balance_date 扫，没填的一条都扫不到）。
  没人管的必须排在有人管的前面，否则简报只是在重复催办已经做过的事。
- **配额**：纯按分数排，前三名会被大额应收包圆，审批永远排不上——
  可审批是卡着别人干活的。所以固定「应收 2 + 审批 1」，见 `_compose`。
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models
from ..database import SessionLocal
from .cards import ledger_settle, pay_req, sales_order

log = logging.getLogger("agent.briefing")

_BLIND_BOOST = 1.6         # 无人管的加成
_AGE_CAP_DAYS = 90         # 时间系数封顶
_REPEAT_COOLDOWN_DAYS = 5  # 同一条推过之后多少天内不再推（见 build 的 skip_refs）


def _age_days(d: datetime | None) -> int:
    if not d:
        return 0
    ts = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return max(0, (date.today() - ts.astimezone(timezone.utc).date()).days)


def _score(amount: float, age: int, blind: bool) -> float:
    """金额线性（见模块注释里为什么不用 log10），时间封顶，盲区乘加成。"""
    t = 1.0 + min(age, _AGE_CAP_DAYS) / _AGE_CAP_DAYS
    return float(amount or 0) * t * (_BLIND_BOOST if blind else 1.0)


def _money(x) -> str:
    v = float(x or 0)
    return f"¥{v / 10000:.1f} 万" if v >= 10000 else f"¥{v:,.0f}"


async def _ledger_items(db: AsyncSession, current: models.User) -> list[dict]:
    """应收：发货款应收 + 没填到期日的尾款。口径与卡片、与查询工具三处同源
    （都走 ledger_settle.blind_ledgers，查得到就一定能处理）。"""
    leds = await ledger_settle.blind_ledgers(db, current)
    if not leds:
        return []

    # 发货状态决定「该不该催」，一次取完避免逐条查
    pids = {l.project_id for l in leds}
    ship_status: dict[int, str] = {}
    for sh in (await db.execute(
            select(models.Shipment)
            .where(models.Shipment.project_id.in_(pids))
            .order_by(models.Shipment.project_id, models.Shipment.id.desc()))).scalars().all():
        ship_status.setdefault(sh.project_id, sh.status or "")

    out: list[dict] = []
    for led in leds:
        age = _age_days(led.created_at)
        cust = led.customer or "(未填客户)"
        aged = f"台账建了 {age} 天" if age else "今天建的台账"

        if (led.ship_receivable or 0) > 0:
            st = ship_status.get(led.project_id)
            # ⚠️ 这三个分支是本模块的核心判断。生产上 35 张发货单 32 张是 pending，
            #    无脑推「去催款」会让人去追一笔可能还没发货的钱。
            if st is None:
                why = f"{aged}·没有发货单，这笔发货款的依据要先确认"
                act = "先核对"
            elif st == "pending":
                why = f"{aged}·发货单还是「待发货」，先确认货到底发了没，再决定催不催"
                act = "先核对"
            else:
                why = f"{aged}·已发货，发货款还挂着，全系统没有任何提醒扫过它"
                act = "已收款"
            out.append({
                "cat": "recv", "card": "ledger_settle", "ref": led.id,
                "title": f"{cust} 发货款应收 {_money(led.ship_receivable)}",
                "why": why, "action": act,
                "amount": float(led.ship_receivable or 0),
                "score": _score(led.ship_receivable, age, blind=True),
            })

        if (led.balance or 0) > 0 and not (led.balance_date or "").strip():
            out.append({
                "cat": "recv", "card": "ledger_settle", "ref": led.id,
                "title": f"{cust} 尾款 {_money(led.balance)}",
                "why": f"{aged}·没填到期日，催办按到期日扫，这一笔永远扫不到",
                "action": "补到期日",
                "amount": float(led.balance or 0),
                "score": _score(led.balance, age, blind=True),
            })
    return out


async def _pay_req_items(db: AsyncSession, current: models.User) -> list[dict]:
    """待审请款。字段以 cards/pay_req.py 为准：金额是 requested_amount。"""
    prs = await pay_req.pending_pay_reqs(db, current)
    sup_ids = {pr.supplier_id for pr in prs if pr.supplier_id}
    names: dict[int, str] = {}
    if sup_ids:
        names = {s.id: s.name for s in (await db.execute(
            select(models.Supplier).where(models.Supplier.id.in_(sup_ids)))).scalars().all()}
    out = []
    for pr in prs:
        age = _age_days(pr.created_at)
        amt = float(pr.requested_amount or 0)
        out.append({
            "cat": "approve", "card": "pay_req_approve", "ref": pr.id,
            "title": f"{names.get(pr.supplier_id) or '请款单'} {_money(amt)} 待你审批",
            "why": f"已等 {age} 天，供应商在等回款" if age >= 2 else "今天提交的",
            "action": "审批", "amount": amt,
            "score": _score(amt, age, blind=False),
        })
    return out


async def _order_items(db: AsyncSession, current: models.User) -> list[dict]:
    """待审销售订单（SalesLedger.order_state == pending）。"""
    out = []
    for led in await sales_order.pending_orders(db, current):
        age = _age_days(led.created_at)
        amt = float(led.amount or 0)
        out.append({
            "cat": "approve", "card": "sales_order_approve", "ref": led.id,
            "title": f"{led.customer or '销售订单'} {_money(amt)} 待你审批",
            "why": f"已等 {age} 天，下游排产等着它" if age >= 2 else "新提交，下游排产等着它",
            "action": "审批", "amount": amt,
            "score": _score(amt, age, blind=False),
        })
    return out


def _compose(items: list[dict], top: int) -> list[dict]:
    """按配额挑：应收 2 + 审批 1，不够的用总榜补齐。

    纯按分数排的话前三名会被大额应收包圆——审批单金额小得多，永远排不上。
    但审批是**卡着别人干活**的，性质不同，必须留一个位置。
    """
    picked: list[dict] = []
    quota = {"recv": max(1, top - 1), "approve": 1}
    for cat, n in quota.items():
        picked += [i for i in items if i["cat"] == cat][:n]
    if len(picked) < top:                       # 某一类没有 → 用总榜补
        chosen = {(i["card"], i["ref"], i["title"]) for i in picked}
        for i in items:
            if (i["card"], i["ref"], i["title"]) not in chosen:
                picked.append(i)
                if len(picked) >= top:
                    break
    picked.sort(key=lambda x: -x["score"])
    return picked[:top]


# 三路取数互不依赖 → 并发跑（定义在三个函数之后）
_FETCHERS = (_ledger_items, _pay_req_items, _order_items)


async def build(current: models.User, top: int = 3,
                skip_refs: set[tuple[str, int]] | None = None) -> dict[str, Any]:
    """给某个人生成今日简报。

    `skip_refs` 是最近推过的 (card, ref)，用来避免天天推同样三条——
    催办就是栽在这上面（83 条推送没人理）。调用方从 messages 表取，见 daily.py。

    ⚠️ 三路取数**并发**跑，而且每路必须用**自己的** AsyncSession ——
    同一个 session 不能并发复用（SQLAlchemy 会报 concurrent operations not permitted）。
    这也是 agent_router 里工具并行的写法，别再退回 `for fn in ...: await fn(db, ...)`
    那种串行版：三路本来互不依赖，串起来白等两轮往返。
    所以这里不收外部 session，自己开——收了反而会诱使人复用它。
    """
    async def _one(fn):
        async with SessionLocal() as s:
            return await fn(s, current)

    results = await asyncio.gather(*(_one(fn) for fn in _FETCHERS), return_exceptions=True)
    items: list[dict] = []
    for fn, r in zip(_FETCHERS, results):
        if isinstance(r, Exception):
            # 一路炸掉不能让整份简报发不出去；缺的那类下次再补
            log.warning("[briefing] %s 取数失败: %s", fn.__name__, r)
            continue
        items.extend(r)
    items.sort(key=lambda x: -x["score"])

    fresh = items
    if skip_refs:
        fresh = [i for i in items if (i["card"], i["ref"]) not in skip_refs]
        # ⚠️ 这里**不能**回退成「全推过了就再推一遍」。
        #    重复推同一批正是催办失败的原因（83 条推送没人理）。
        #    冷却期内全推过 → 今天就不打扰，等冷却到期或有新的再说。
        #    他随时能从 H5 首页/`/briefing/me` 看全量，不会因此漏掉什么。

    picked = _compose(fresh, top)
    return {
        "items": picked,
        "total_count": len(items),
        "total_amount": round(sum(i["amount"] for i in items), 2),
        "rest": max(0, len(items) - len(picked)),
    }


def render(brief: dict[str, Any], name: str = "") -> str:
    """渲染成一条企微/站内消息。手机上看：短、一条一行、每条带动作。"""
    items = brief["items"]
    if not items:
        return ""
    lines = [f"{name}，今天这 {len(items)} 件事该管："]
    for i, it in enumerate(items, 1):
        lines.append(f"{'①②③④⑤'[i - 1] if i <= 5 else f'{i}.'} {it['title']}")
        lines.append(f"   {it['why']} → 可「{it['action']}」")
    if brief["rest"]:
        lines.append(f"另有 {brief['rest']} 项（合计 {_money(brief['total_amount'])}），"
                     f"打开 AI 助手看全部。")
    return "\n".join(lines)


def recent_cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=_REPEAT_COOLDOWN_DAYS)
