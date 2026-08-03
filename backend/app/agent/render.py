"""明细渲染 —— 智能体 v2 阶段二（见 docs/agent-architecture-v2.md）。

为什么要有这一层
----------------
实测「采购未到货，全部列举，按超期排序」：**35.9 秒 / 1743 个流式片段**。
慢的不是查库（工具 2~14ms），是**让大模型一个字一个字把 46 行数据打出来**。

而且每一个数字都是模型手打的 —— 幻觉风险、截断误报，全从这来。

改法：**模型只出判断，明细由代码渲染。**

    模型输出（~150 字的 JSON）        代码负责
    ├ 结论一句话                      ├ 按 sort/group 排版 46 行
    ├ 按什么排序                      ├ 数字格式化
    ├ 怎么分组                        ├ 合计/占比（**代码算，不是模型算**）
    └ 点名哪几条                      └ 截断声明（代码知道总数）

    |            | 现在      | 改后    |
    |------------|-----------|---------|
    | 模型输出   | ~1700 字  | ~150 字 |
    | 总时长     | 35.9s     | 3~5s    |
    | 数字幻觉   | 有        | **0**   |
    | 截断误报   | 有        | **0**   |

⚠️ 代价：明细排版变成固定格式，模型不再自由发挥。这是**刻意的取舍** ——
   固定格式可测、可比、不会今天一个样明天一个样。
"""
from typing import Any

# 每类工具结果怎么摆一行。顺序即优先级，取前几个存在的字段。
# 与提示词里的「主体 · 关键量 · 时间/状态 · (补充)」保持一致。
_FIELD_ORDER = [
    # (字段名, 显示前缀, 是否金额)
    ("supplier", "", False), ("customer", "", False), ("name", "", False),
    ("item_name", "", False), ("material", "", False),
    ("project_code", "", False), ("code", "", False),
    ("spec", "", False),
    ("amount", "", True), ("contract", "合同 ", True),
    ("ship_receivable", "发货款 ", True), ("balance", "尾款 ", True),
    ("unpaid_total", "未收 ", True), ("qty", "数量 ", False), ("stock", "库存 ", False),
    ("over_days", "超 ", False), ("age_days", "挂 ", False), ("ledger_age_days", "建档 ", False),
    ("expected_arrival", "预计 ", False), ("due_date", "到期 ", False),
    ("balance_date", "尾款到期 ", False),
    ("status", "", False), ("ship_status", "", False), ("order_state", "", False),
    ("po_no", "", False), ("buyer", "", False), ("worker", "", False),
    ("dept_name", "", False), ("location", "库位 ", False),
]

_DAY_FIELDS = {"over_days", "age_days", "ledger_age_days"}
_MONEY_MIN = 10000


def money(v) -> str:
    """金额。**写原始数值**，不写「22 万」—— 对账时「22 万」没法核。"""
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return str(v)
    return f"¥{f:,.0f}" if abs(f) >= 1 else f"¥{f:,.2f}"


def _cell(key: str, val: Any, prefix: str, is_money: bool) -> str | None:
    if val is None or val == "" or val is False:
        return None
    if is_money:
        try:
            if abs(float(val)) < 0.005:
                return None
        except (TypeError, ValueError):
            pass
        return f"{prefix}{money(val)}"
    if key in _DAY_FIELDS:
        try:
            n = int(val)
        except (TypeError, ValueError):
            return None
        if n <= 0 and key == "over_days":
            return "今日到期"
        return f"{prefix}{n} 天"
    return f"{prefix}{val}"


def row(item: dict, fields: list[str] | None = None, emphasis: bool = False) -> str:
    """一条明细渲染成一行：`主体 · 关键量 · 时间/状态 · (补充)`。

    fields 给定就按它的顺序，否则按 _FIELD_ORDER 挑存在的前几个。
    """
    parts: list[str] = []
    if fields:
        for f in fields:
            spec = next((x for x in _FIELD_ORDER if x[0] == f), (f, "", False))
            c = _cell(f, item.get(f), spec[1], spec[2])
            if c:
                parts.append(c)
    else:
        for key, prefix, is_money in _FIELD_ORDER:
            if key in item:
                c = _cell(key, item[key], prefix, is_money)
                if c:
                    parts.append(c)
            if len(parts) >= 5:      # 手机上一行放不下更多
                break
    line = " · ".join(parts) if parts else str(item)
    return f"- **{line}**" if emphasis else f"- {line}"


def table(result: dict, *, plan: dict | None = None) -> str:
    """把工具结果渲染成明细段。

    plan 是模型给的编排（可选）：
      { "sort": "over_days", "desc": true, "group": {"字段": "值"}|null,
        "highlight": ["po_no值", ...], "fields": ["supplier","item_name",...] }
    模型只需要给这些，**一行数据都不用它打**。
    """
    plan = plan or {}
    items = None
    for k in ("items", "suppliers", "rows"):
        if isinstance(result.get(k), list):
            items = result[k]
            break
    if not items:
        return ""

    sort_key = plan.get("sort")
    if sort_key and all(isinstance(i, dict) for i in items):
        rev = bool(plan.get("desc", True))
        def _k(x):
            v = x.get(sort_key)
            return (v is None, v if isinstance(v, (int, float)) else str(v or ""))
        try:
            items = sorted(items, key=_k, reverse=rev)
        except TypeError:
            pass

    hi = set(str(h) for h in (plan.get("highlight") or []))
    fields = plan.get("fields") or None

    def _is_hi(it: dict) -> bool:
        return bool(hi) and any(str(v) in hi for v in it.values())

    lines = [row(it, fields, emphasis=_is_hi(it)) for it in items]

    # ⚠️ 截断声明由代码写，不交给模型 —— 代码知道真实总数，模型只知道它收到了几条。
    total = result.get("count")
    shown = result.get("shown", len(items))
    if isinstance(total, int) and total > shown:
        lines.append(f"- 已列 {shown} 条，另有 {total - shown} 条未列（共 {total} 条）")
    return "\n".join(lines)


def summary_line(result: dict, label: str = "") -> str:
    """合计行。**代码算**，模型不碰 —— 模型算合计就是幻觉的入口。"""
    total = result.get("count")
    if not isinstance(total, int):
        return ""
    bits = [f"共 {total} 条"]
    items = result.get("items") or []
    for f, name in (("amount", "金额"), ("ship_receivable", "发货款"),
                    ("balance", "尾款"), ("contract", "合同额")):
        vals = [i.get(f) for i in items if isinstance(i, dict) and i.get(f)]
        if vals:
            try:
                bits.append(f"{name}合计 {money(sum(float(v) for v in vals))}")
            except (TypeError, ValueError):
                pass
    return f"**{label}{' · '.join(bits)}**" if label or bits else ""


def compose(conclusion: str, result: dict, plan: dict | None = None) -> str:
    """结论（模型给） + 明细（代码渲染）。这就是最终发给用户的正文。"""
    body = table(result, plan=plan)
    parts = [conclusion.strip()] if conclusion and conclusion.strip() else []
    if body:
        parts.append(body)
    return "\n\n".join(parts)
