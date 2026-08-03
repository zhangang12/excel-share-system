"""Skills —— 智能体 v2 阶段三（见 docs/agent-architecture-v2.md）。

一个 Skill = **固定的工具编排 + 固定的输出模板**。

为什么要有它
------------
可预测性来自编排，不来自模型即兴发挥。反复问、答法有定式的问题走 Skill：
省一次规划、答案稳定、**能写测试**。ReAct 只负责没被 Skill 覆盖的长尾。

选哪些技能：拿两位管理层近 30 天真实操作定的
--------------------------------------------
  杨坛   188 次：销售台账+订单 32%、收货人 12、派活 8、请款审批 4
         → 客户回款画像、项目体检
  赵仁辉 635 次：**仓库 36%**、物料字典 170、派活 43、采购开票 18
         → 库存预警、供应商拖期复盘

⚠️ Skill 命中就**不进 ReAct**。所以命中判定要严——宁可漏判走 ReAct，
   也不要把用户真正的问题劫持成一个预设编排（v1 那个贪婪正则把
   「查询一下所有的待审批的待办?」劫持成查请款单，教训还在）。
"""
import re
from typing import Any, Callable, Awaitable

from sqlalchemy.ext.asyncio import AsyncSession

from .. import models
from . import render as _rd
from . import tools_entity as _te

# 技能定义：命中词 → 编排函数。
# 命中词必须**足够具体**，含糊的词一律不放进来（宁可走 ReAct）。
_SKILLS: dict[str, dict] = {}


def skill(key: str, name: str, triggers: list[str], needs: list[str]):
    """注册一个技能。needs 是所依赖的菜单权限，任一命中即可用。"""
    def deco(fn: Callable[..., Awaitable[dict]]):
        _SKILLS[key] = {"key": key, "name": name, "triggers": triggers,
                        "needs": needs, "run": fn}
        return fn
    return deco


def match(message: str) -> dict | None:
    """判断这句话该走哪个技能。**全词命中才算**，不做模糊匹配。"""
    t = (message or "").strip()
    if not t:
        return None
    for sk in _SKILLS.values():
        for kw in sk["triggers"]:
            if kw in t:
                return sk
    return None


def available(menu_keys: set[str]) -> list[dict]:
    return [{"key": s["key"], "name": s["name"], "triggers": s["triggers"]}
            for s in _SKILLS.values() if not s["needs"] or (set(s["needs"]) & menu_keys)]


# ══════════════════════════ 杨坛：销售/回款 ══════════════════════════

@skill("customer_profile", "客户回款画像",
       ["回款画像", "这个客户怎么样", "客户画像"], ["finance", "sales"])
async def _customer_profile(db: AsyncSession, user: models.User, message: str) -> dict:
    """哪个客户 → 全景 → 用一段固定结构说清「该不该催、催什么」。"""
    name = _pick_entity(message, ["回款画像", "客户画像", "这个客户怎么样"])
    if not name:
        return {"text": "要看哪个客户？说个名字，比如「迈克斯 回款画像」。"}
    found = await _te.find_entity(db, user, name, kind="customer")
    cands = found["matches"].get("customer") or []
    if not cands:
        return {"text": f"系统里没有叫「{name}」的客户台账。名字可能对不上，"
                        f"到销售台账页面确认一下准确写法。"}
    c = await _te.get_customer(db, user, cands[0]["name"])
    if not c.get("found"):
        return {"text": f"没查到「{name}」的台账。"}

    lines = [f"**{c['customer']} · {c['ledger_count']} 单 · 合同 {_rd.money(c['contract_total'])}"
             f" · 未收 {_rd.money(c['unpaid_total'])}"
             + (f" · 已收 {c['paid_ratio']*100:.0f}%**" if c.get("paid_ratio") is not None else "**")]

    # 判断段：这才是画像的价值，不是把台账重列一遍
    judge = []
    if c["ship_receivable_but_not_shipped"]:
        judge.append(f"⚠️ {c['ship_receivable_but_not_shipped']} 笔发货款对应的发货单还停在"
                     f"「待发货」——**货没发出去，这钱还不到该收的时候**，别直接催客户。")
    if c["balance_without_due_date"]:
        judge.append(f"⚠️ {c['balance_without_due_date']} 笔尾款没填到期日，"
                     f"催办按到期日扫，**这几笔永远扫不到**，要手工盯。")
    zero = [i for i in c["items"] if not i.get("contract")]
    if zero:
        judge.append(f"⚠️ {len(zero)} 行合同额为 0，这些项目的毛利会被算成**假亏损**，要补填。")
    if not judge:
        judge.append("没有发现口径问题：该收的都有依据、到期日也填了。")
    lines += ["", *judge]
    return {"text": "\n".join(lines), "result": c,
            "plan": {"sort": "ship_receivable", "desc": True,
                     "fields": ["project_code", "ship_receivable", "balance",
                                "ship_status", "ledger_age_days"]}}


@skill("project_check", "项目体检", ["项目体检", "这个项目怎么样"], ["list"])
async def _project_check(db: AsyncSession, user: models.User, message: str) -> dict:
    """一个项目从台账到发货全看一遍，按固定四段输出。"""
    code = _pick_entity(message, ["项目体检", "这个项目怎么样"])
    if not code:
        return {"text": "要体检哪个项目？给个编号，比如「2026-063 项目体检」。"}
    p = await _te.get_project(db, user, code)
    if not p.get("found"):
        return {"text": f"没找到项目「{code}」。"}
    led = p.get("ledger") or {}
    lines = [f"**{p['project']}「{p['name']}」**", ""]
    lines.append(f"- 合同 {_rd.money(led.get('contract'))} · 客户 {led.get('customer') or '—'}"
                 f" · 订单状态 {led.get('order_state') or '—'}")
    unpaid = float(led.get("ship_receivable") or 0) + float(led.get("balance") or 0)
    lines.append(f"- 未收 {_rd.money(unpaid)}"
                 + (f"（发货款 {_rd.money(led.get('ship_receivable'))}）"
                    if led.get("ship_receivable") else ""))
    lines.append(f"- 部门任务逾期 {p['dept_overdue_count']} 个 · "
                 f"采购未到货 {p.get('purchase_overdue_count', 0)} 项")
    lines.append(f"- 发货状态 {p.get('shipment_status') or '还没建发货单'}"
                 + ("（收货人未填）" if not p.get("shipment_receiver") else ""))
    risk = []
    if p["dept_overdue_count"]:
        risk.append(f"{p['dept_overdue_count']} 个部门任务已逾期")
    if p.get("purchase_overdue_count"):
        risk.append(f"{p['purchase_overdue_count']} 项采购到期没到货")
    if not led.get("contract"):
        risk.append("合同额为 0，毛利会算成假亏损")
    lines += ["", ("⚠️ 风险：" + "；".join(risk)) if risk else "✅ 没有发现明显风险。"]
    return {"text": "\n".join(lines)}


# ══════════════════════════ 赵仁辉：仓库/采购 ══════════════════════════

@skill("stock_alert", "库存预警", ["库存预警", "哪些物料不够", "缺料"], ["warehouse"])
async def _stock_alert(db: AsyncSession, user: models.User, message: str) -> dict:
    """低于安全库存的物料。赵仁辉 36% 的操作在仓库，这是他最该被主动告知的。"""
    from sqlalchemy import select
    mats = list((await db.execute(select(models.WhMaterial).where(
        models.WhMaterial.safety_stock > 0))).scalars().all())
    if not mats:
        return {"text": "**没有物料设置了安全库存**，所以算不出缺料。"
                        "要用这个功能，先在物料主数据里把安全库存填上。"}
    rows = []
    for m in mats:
        d = await _te.get_material(db, user, m.code or m.name)
        if d.get("below_safety"):
            rows.append({"material": d["material"], "code": d["code"],
                         "stock": d["stock"], "safety": d["safety_stock"],
                         "shortfall": d["shortfall"], "location": d["location"]})
    if not rows:
        return {"text": f"**{len(mats)} 个设了安全库存的物料都在安全线以上** ✅"}
    rows.sort(key=lambda x: -x["shortfall"])
    return {"text": f"**{len(rows)} 个物料低于安全库存**（共 {len(mats)} 个设了安全线）。"
                    f"缺口最大的是 {rows[0]['material']}，差 {rows[0]['shortfall']}。",
            "result": {"count": len(rows), "shown": len(rows), "items": rows},
            "plan": {"sort": "shortfall", "desc": True,
                     "fields": ["material", "stock", "shortfall", "location"]}}


@skill("supplier_review", "供应商拖期复盘",
       ["供应商复盘", "哪家供应商拖期", "供应商拖期"], ["purchase_mgmt"])
async def _supplier_review(db: AsyncSession, user: models.User, message: str) -> dict:
    """按供应商聚合未到货，指出集中度——「谁最该被约谈」。"""
    from ..routers.agent_router import _run_tool
    d = await _run_tool("po_overdue_by_supplier", {"limit": 200}, db, user)
    sups = d.get("suppliers") or []
    if not sups:
        return {"text": "**当前没有到期未到货的采购** ✅"}
    total_items = d.get("item_total") or sum(s.get("count", 0) for s in sups)
    top = sorted(sups, key=lambda s: -(s.get("count") or 0))[:3]
    share = sum(s.get("count", 0) for s in top) / total_items if total_items else 0
    return {"text": f"**{len(sups)} 家供应商共 {total_items} 条未到货，"
                    f"前 3 家占 {share*100:.0f}%。** 最集中的是 {top[0].get('supplier')}"
                    f"（{top[0].get('count')} 条）——集中度这么高，约谈一家就能解决大半。",
            "result": d, "plan": {"sort": "count", "desc": True}}


# ══════════════════════════ 工具 ══════════════════════════

_STOP = re.compile(r"[的了吗呢啊？?，,。.\s]+")


def _pick_entity(message: str, triggers: list[str]) -> str:
    """从「迈克斯 回款画像」里抠出「迈克斯」。把触发词和虚词去掉即可。"""
    t = message or ""
    for kw in triggers:
        t = t.replace(kw, " ")
    t = _STOP.sub(" ", t).strip()
    return t


async def run(db: AsyncSession, user: models.User, sk: dict, message: str) -> str:
    """执行技能并把明细渲染上去。返回最终正文。"""
    out = await sk["run"](db, user, message)
    text = out.get("text", "")
    result = out.get("result")
    if isinstance(result, dict):
        detail = _rd.table(result, plan=out.get("plan") or _rd.default_plan(result))
        if detail:
            text = (text.rstrip() + "\n\n" + detail).strip()
    return text
