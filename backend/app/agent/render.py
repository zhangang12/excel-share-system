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
    # (字段名, 行内前缀, 是否金额, 表头列名)
    #
    # ⚠️ **这个顺序就是表格的列优先级**（只取前 3 列），排错了信息就被挤掉。
    #    排序原则：谁 → 多少 → 卡在哪 → 谁负责 → 具体哪天。
    #    绝对日期排最后：已经有「超 17 天 / 已过 55 天」这种相对量时，
    #    再占一列写「2026-06-10」是重复，而「卡在哪」「负责人」会被挤没。
    #
    # ── ① 认得出是哪一单（编号在名字前，人是按编号对单的）──
    ("project", "", False, "项目"), ("project_code", "", False, "项目"),
    ("code", "", False, "编号"),
    ("supplier", "", False, "供应商"), ("customer", "", False, "客户"),
    ("name", "", False, "名称"), ("item_name", "", False, "物料"),
    ("material", "", False, "物料"), ("spec", "", False, "规格"),
    # ── ② 多少钱 / 多少个 ──
    ("amount", "", True, "金额"), ("contract", "合同 ", True, "合同额"),
    ("ship_receivable", "发货款 ", True, "发货款"), ("balance", "尾款 ", True, "尾款"),
    ("unpaid_total", "未收 ", True, "未收"),
    ("qty", "数量 ", False, "数量"), ("stock", "库存 ", False, "库存"),
    ("shortfall", "缺 ", False, "缺口"),
    # ── ③ 急不急（相对量，比绝对日期直观）──
    ("days_left", "", False, "剩余"), ("over_days", "超 ", False, "超期"),
    ("age_days", "挂 ", False, "挂账"), ("ledger_age_days", "建档 ", False, "建档"),
    # ── ④ 卡在哪 / 什么状态 ──
    ("blocked_at", "卡在 ", False, "卡在哪"),
    ("status", "", False, "状态"), ("ship_status", "", False, "发货"),
    ("order_state", "", False, "订单"), ("purchase_pending", "待到货 ", False, "待到货"),
    # ── ⑤ 谁负责（要催人时比日期有用）──
    ("worker", "", False, "负责人"), ("buyer", "", False, "采购员"),
    ("dept_name", "", False, "部门"),
    # ── ⑥ 具体哪天（最后）──
    ("deliver_date", "交货 ", False, "交货日"),
    ("expected_arrival", "预计 ", False, "预计到货"),
    ("due_date", "到期 ", False, "截止"),
    ("balance_date", "尾款到期 ", False, "尾款到期"),
    ("po_no", "", False, "采购单"), ("location", "库位 ", False, "库位"),
]

# 表格最多几列。手机屏窄，**3 列是上限** —— 再多就要横向滚动，
# 而横向滚动的表格等于没有表格（人不会去滑）。
_MAX_COLS = 3
# 第一列合并「编号 + 名称」：编号用来对单，名称用来认是什么东西，
# 分成两列会挤掉「卡在哪」这种真正要看的信息。
_ID_KEYS = ("project", "project_code", "code")
_NAME_KEYS = ("name", "item_name", "material", "supplier", "customer")

_DAY_FIELDS = {"over_days", "age_days", "ledger_age_days"}
# 分组后每组最多铺几行。手机上一屏放不下十几行，剩下的写成「另有 N 条」。
_GROUP_MAX = 6
_COUNT_FIELDS = {"purchase_pending": "项"}
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
    # 🆕 交期倒计时：**不能直接把 -55 甩给人看**。负数是已过交货日，
    #    写成「剩 -55 天」既费解又容易被读反。
    if key == "days_left":
        try:
            n = int(val)
        except (TypeError, ValueError):
            return None
        return "今天交货" if n == 0 else (f"已过 {-n} 天" if n < 0 else f"剩 {n} 天")
    if key in _COUNT_FIELDS:
        try:
            n = int(val)
        except (TypeError, ValueError):
            return None
        return None if n <= 0 else f"{prefix}{n} {_COUNT_FIELDS[key]}"
    return f"{prefix}{val}"


def _auto_parts(item: dict) -> list[str]:
    parts: list[str] = []
    for key, prefix, is_money, _label in _FIELD_ORDER:
        if key in item:
            c = _cell(key, item[key], prefix, is_money)
            if c:
                parts.append(c)
    return parts


def row(item: dict, fields: list[str] | None = None, emphasis: bool = False) -> str | None:
    """一条明细渲染成一行：`主体 · 关键量 · 时间/状态 · (补充)`。

    fields 给定就按它的顺序，否则按 _FIELD_ORDER 挑存在的前几个。
    渲染不出任何字段时返回 None —— 调用方负责跳过这一行。
    """
    parts: list[str] = []
    if fields:
        for f in fields:
            spec = next((x for x in _FIELD_ORDER if x[0] == f), (f, "", False, f))
            c = _cell(f, item.get(f), spec[1], spec[2])
            if c:
                parts.append(c)
        # ⚠️ 模型给的 fields 可能**跟这批数据根本对不上**：它调了两个工具，
        #   编排块写的是前一个工具的字段名，而 last_result 是后一个的。
        #   生产实测就这么翻车过 —— 一个字段都没命中，于是走到
        #   `str(item)` 兜底，把**裸 Python dict 直接甩给了用户**：
        #     - {'dept': 'electric', 'project_code': '2026-057', ...}
        #   这时正确做法是**忽略 fields、按默认顺序自己挑**，而不是硬按它的错清单来。
        if not parts:
            parts = _auto_parts(item)
    else:
        parts = _auto_parts(item)
    if not parts:
        # ⚠️ **绝不能 `str(item)`**。这条兜底以前就在这，生产上真的把裸 dict
        #    漏给了用户。宁可少一行明细，也不能把内部结构甩出去 ——
        #    与 apply_render 里「模型给坏 JSON 就只删块不渲染」是同一条纪律。
        return None
    line = " · ".join(parts[:5])      # 手机上一行放不下更多
    return f"- **{line}**" if emphasis else f"- {line}"


# ════════════════════════════ 表格 ════════════════════════════
# ⚠️ 明细**一律用表格**，不用 `- A · B · C` 的列表。
#    用户原话：「这么一堆文字，用户肯定不愿看，要以用户体验为中心。」
#    主场景是手机：一行 `2026-045B · 300L平台式中转罐 · 交货 2026-06-10 ·
#    已过 55 天 · 卡在 电工未完成` 在窄屏上折成三行，十几条就是一屏文字墙，
#    人只会划过去。表格有对齐的列，能横向扫、能比较，同样的信息量省一半眼力。


# 语义着色：后端只给**档位**，颜色由前端翻（后端不许吐 HTML，见 markdown.ts 的红线）。
# 标记形如 `[[danger:已过 55 天]]`，前端老包遇到会原样显示 —— 刻意的取舍。
# ⚠️ 分隔符是**冒号不是竖线**：这些标记要放进表格单元格，竖线是列分隔符。
_TONE_MARK = "[[{tone}:{text}]]"


def _tone_of(key: str, val: Any) -> str | None:
    """这一格该是什么颜色。**只给真正需要一眼看见的**，全都上色等于都没上色。"""
    try:
        n = int(val)
    except (TypeError, ValueError):
        return None
    if key == "days_left":
        return "danger" if n < 0 else ("warn" if n <= 7 else None)
    if key in ("over_days", "age_days"):
        return "danger" if n > 0 else ("warn" if n == 0 else None)
    if key == "shortfall":
        return "danger" if n > 0 else None
    if key in ("purchase_pending", "dept_overdue", "produce_open"):
        return "warn" if n > 0 else None
    return None


def _esc(v: str) -> str:
    """竖线会把表格列冲散，换行会把一行拆成两行 —— 都得先处理掉。"""
    return str(v).replace("|", "／").replace("\n", " ").strip()


def _pick_columns(items: list[dict], prefer: list[str] | None = None) -> list[tuple]:
    """挑表格的列：第一列合并「编号+名称」，其余按 _FIELD_ORDER 优先级取。

    ⚠️ `prefer` 是**工具自己声明**的列（结果里的 `columns`）。为什么需要它：
       `_FIELD_ORDER` 是一套全局优先级，而「哪几列重要」是**跟场景走的**。
       交期看板的行里同时有 `contract`（合同额）和 `days_left`（剩余），
       全局顺序里金额排在前面 —— 于是表格出来是「项目｜客户｜合同额」，
       把真正要看的「剩余／卡在哪」全挤掉了（实测）。
       口径归工具：谁产出的数据，谁最清楚该先看哪几列。
    """
    present = {k for it in items if isinstance(it, dict) for k in it
               if _cell_of(it, k) is not None}
    cols: list[tuple] = []
    if prefer:
        id_key = next((k for k in _ID_KEYS if k in prefer and k in present), None)
        name_key = next((k for k in _NAME_KEYS if k in prefer and k in present), None)
        if id_key or name_key:
            lab = next((x[3] for x in _FIELD_ORDER if x[0] == (id_key or name_key)), "名称")
            cols.append(("__id__", (id_key, name_key), lab))
        for key in prefer:
            if len(cols) >= _MAX_COLS:
                break
            if key in (id_key, name_key) or key not in present:
                continue
            spec = next((x for x in _FIELD_ORDER if x[0] == key), None)
            if spec:
                cols.append((key, (spec[1], spec[2]), spec[3]))
        if cols:
            return cols
    id_key = next((k for k in _ID_KEYS if k in present), None)
    name_key = next((k for k in _NAME_KEYS if k in present), None)
    if id_key or name_key:
        lab = next((x[3] for x in _FIELD_ORDER if x[0] == (id_key or name_key)), "名称")
        cols.append(("__id__", (id_key, name_key), lab))
    used = {id_key, name_key}
    for key, prefix, is_money, label in _FIELD_ORDER:
        if len(cols) >= _MAX_COLS:
            break
        if key in present and key not in used:
            cols.append((key, (prefix, is_money), label))
            used.add(key)
    return cols


def _cell_of(item: dict, key: str) -> str | None:
    spec = next((x for x in _FIELD_ORDER if x[0] == key), None)
    if spec is None or key not in item:
        return None
    return _cell(key, item[key], "", spec[2])      # 表格里不要行内前缀，表头就是前缀


def _table_rows(items: list[dict], cols: list[tuple], is_hi) -> list[str]:
    out = [
        "| " + " | ".join(c[2] for c in cols) + " |",
        "|" + "|".join(["---"] * len(cols)) + "|",
    ]
    for it in items:
        # ⚠️ 高亮判的是**整行任一字段**命中，不只是编号列 —— 模型点名的
        #    往往是采购单号、供应商这类不在第一列的值（实测漏过）。
        #    命中就把第一格加粗：手机上整行加粗反而糊成一片。
        hit = bool(is_hi and is_hi(it))
        cells = []
        for key, meta, _label in cols:
            if key == "__id__":
                id_key, name_key = meta
                bits = [_esc(it.get(k)) for k in (id_key, name_key)
                        if k and it.get(k) not in (None, "")]
                v = " ".join(bits)
            else:
                v = _esc(_cell_of(it, key) or "")
                tone = _tone_of(key, it.get(key)) if v else None
                if tone:
                    v = _TONE_MARK.format(tone=tone, text=v)
            cells.append(v or _TONE_MARK.format(tone="muted", text="—"))
        if hit and cells:
            cells[0] = f"**{cells[0]}**"
        out.append("| " + " | ".join(cells) + " |")
    return out


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
    rev = bool(plan.get("desc", True))

    def _sorted(rows: list) -> list:
        if not sort_key or not all(isinstance(i, dict) for i in rows):
            return rows
        def _k(x):
            v = x.get(sort_key)
            return (v is None, v if isinstance(v, (int, float)) else str(v or ""))
        try:
            return sorted(rows, key=_k, reverse=rev)
        except TypeError:
            return rows

    hi = set(str(h) for h in (plan.get("highlight") or []))
    fields = plan.get("fields") or None
    # 工具声明的列优先于全局顺序（见 _pick_columns）
    prefer = result.get("columns") if isinstance(result.get("columns"), list) else None

    def _is_hi(it: dict) -> bool:
        return bool(hi) and any(str(v) in hi for v in it.values())

    # 🆕 分组：plan 里给一个字段名，就按它分段并加小标题。
    # 这个能力在本文件开头的注释里写了很久，但一直没实现 —— 46 个项目拉平成
    # 一串等长的行，人得一行行数才知道哪些是真急的。分了组才扫得动。
    # ⚠️ **保持工具给的原始顺序**：工具已经排好了（在做的按剩余天数 → 没交货日
    #    → 已发货待收尾），这里按出现次序建组，不重排。
    group_key = plan.get("group")
    # ⚠️ 分组字段必须**真的在数据上**。模型给的 group 可能是另一个工具的字段名
    #   （它这一轮调了两个工具），那时每一行 `it.get(group_key)` 都是 None，
    #   全部落进「其他」——一个信息量为零的大标题，比不分组还糟。实测栽过。
    can_group = (isinstance(group_key, str) and group_key
                 and all(isinstance(i, dict) for i in items)
                 and any(i.get(group_key) for i in items))
    if can_group:
        # ⚠️ **分组时 sort 只在组内生效**。跨组重排会把工具排好的优先级推翻 ——
        #   实测模型给 `sort:days_left,desc:false`，结果「已发货·只差收尾」
        #   （过期 181 天）被顶到第一组，而那恰恰是最不该占首位的一类。
        #   组的先后由工具决定（它知道哪一档更急），组内怎么排才轮到模型说了算。
        buckets: dict[str, list[dict]] = {}
        for it in items:
            buckets.setdefault(str(it.get(group_key) or "其他"), []).append(it)
        cols = _pick_columns([i for v in buckets.values() for i in v], prefer)
        lines = []
        for name, rows in ((k, _sorted(v)) for k, v in buckets.items()):
            lines.append(f"**{name}**（{len(rows)}）")
            lines.append("")
            # 每组只铺前几条。手机上 26 行一屏翻不完，人只会划过去 ——
            # 「最急的看得见」比「全都列出来」有用得多；剩下的说清有多少。
            head = rows[:_GROUP_MAX]
            if cols:
                lines += _table_rows(head, cols, _is_hi)
            else:
                lines += [r for r in (row(it, fields, emphasis=_is_hi(it))
                                      for it in head) if r]
            if len(rows) > _GROUP_MAX:
                lines.append("")
                lines.append(f"…本组另有 {len(rows) - _GROUP_MAX} 条")
            lines.append("")
        while lines and not lines[-1]:
            lines.pop()
    else:
        items = _sorted(items)
        cols = _pick_columns(items, prefer)
        if cols:
            lines = _table_rows(items, cols, _is_hi)
        else:
            lines = [r for r in (row(it, fields, emphasis=_is_hi(it)) for it in items) if r]

    # ⚠️ 截断声明由代码写，不交给模型 —— 代码知道真实总数，模型只知道它收到了几条。
    total = result.get("count")
    shown = result.get("shown", len(items))
    if isinstance(total, int) and total > shown:
        # ⚠️ 空一行再写，且**不要 `- ` 前缀**：紧贴在表格后面的列表项会被
        #    markdown 当成表格的一部分处理，排出来歪歪扭扭。
        lines.append("")
        lines.append(f"已列 {shown} 条，另有 {total - shown} 条未列（共 {total} 条）")
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


def default_plan(result: dict) -> dict:
    """模型没给编排时的默认排法：挑一个「越大越紧迫」的数值字段降序。

    宁可用一个讲得通的默认，也不要因为模型忘了给块就一条明细都不出。
    """
    items = None
    for k in ("items", "suppliers", "rows"):
        if isinstance(result.get(k), list) and result[k]:
            items = result[k]
            break
    if not items or not isinstance(items[0], dict):
        return {}
    for key in ("over_days", "age_days", "ledger_age_days",
                "amount", "ship_receivable", "balance", "qty"):
        if any(isinstance(i.get(key), (int, float)) for i in items):
            return {"sort": key, "desc": True}
    return {}


def compose(conclusion: str, result: dict, plan: dict | None = None) -> str:
    """结论（模型给） + 明细（代码渲染）。这就是最终发给用户的正文。"""
    body = table(result, plan=plan)
    parts = [conclusion.strip()] if conclusion and conclusion.strip() else []
    if body:
        parts.append(body)
    return "\n\n".join(parts)
