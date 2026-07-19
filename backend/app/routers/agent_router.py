"""🆕 Agent 助手（只读问数 POC，仅 admin/manager 可用）。

- POST /api/agent/chat  {message, history?} → {reply, fallback, sources}
  - history 可选，最多保留最近 10 轮（20 条）
  - sources 列出本轮实际调用的数据工具（前端小字展示「数据来源」）
- 大脑：OpenAI 兼容接口 function calling（30s 超时）。LLM 配置生效优先级 =
  数据库 app_settings（admin 在页面配置，GET/PUT /api/agent/config）> settings(.env) 默认值；
  api_key 任何接口/日志都不输出明文
- 降级：生效配置无 api_key、或 LLM 调用任何异常 → 规则意图匹配 + 模板格式化，
  fallback=true；降级路径不依赖任何外部服务，永远可用
- 只读红线：本模块所有数据工具仅 SELECT，不做任何写库操作
- 查询口径照抄 overdue.py（采购到期未到货/部门逾期任务/尾款到期/人事到期），
  日期字段为 ISO 字符串可直接字典序比较；业务时区中国 UTC+8（复用 overdue._CN_TZ）
"""
import json
import logging
import re
from datetime import date, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models
from ..config import settings
from ..database import get_db
from ..deps import require_admin_or_manager, get_current_user
from ..dept_config import DEPTS
from ..overdue import _CN_TZ

log = logging.getLogger("agent")

router = APIRouter(prefix="/api/agent", tags=["Agent助手"])

_ORDER_STATUS_CN = {"pending_assign": "待分派", "assigned": "待接单",
                    "in_progress": "进行中", "done": "已完成", "voided": "已作废"}


def _today() -> date:
    return datetime.now(_CN_TZ).date()


def _parse_d(s) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def _uname(u) -> str:
    return (u.full_name or u.username) if u else "—"


# ==================== 数据工具层（全部只读 SELECT） ====================

async def tool_po_arrival_overdue(db: AsyncSession, min_overdue_days: int = 0) -> dict:
    """采购到期未到货明细：预计到货日期已到(含当天)且仍未收货（口径同 overdue.scan_po_arrival_overdue）。"""
    today = _today()
    r = await db.execute(
        select(models.PurchaseItem).where(
            models.PurchaseItem.expected_arrival.isnot(None),
            models.PurchaseItem.expected_arrival != "",
            models.PurchaseItem.expected_arrival <= today.isoformat(),
            or_(models.PurchaseItem.arrival_date.is_(None),
                models.PurchaseItem.arrival_date == ""),
        )
    )
    rows = []
    for it in r.scalars().all():
        exp = _parse_d(it.expected_arrival)
        if exp is None:
            continue
        over_days = (today - exp).days
        if over_days < min_overdue_days:
            continue
        rows.append({
            "item_name": it.item_name, "po_no": it.po_no,
            "supplier": it.supplier.name if it.supplier else "—",
            "project_code": it.project_code, "buyer": _uname(it.buyer),
            "expected_arrival": it.expected_arrival, "over_days": over_days,
        })
    rows.sort(key=lambda x: -x["over_days"])
    return {"count": len(rows), "items": rows[:20]}


async def tool_po_arriving(db: AsyncSession, days: int = 3) -> dict:
    """未来 N 天（含今天）预计到货、目前仍未收货的采购明细。"""
    today = _today()
    end = (today + timedelta(days=max(days, 0))).isoformat()
    r = await db.execute(
        select(models.PurchaseItem).where(
            models.PurchaseItem.expected_arrival.isnot(None),
            models.PurchaseItem.expected_arrival != "",
            models.PurchaseItem.expected_arrival >= today.isoformat(),
            models.PurchaseItem.expected_arrival <= end,
            or_(models.PurchaseItem.arrival_date.is_(None),
                models.PurchaseItem.arrival_date == ""),
        )
    )
    rows = [{
        "item_name": it.item_name, "po_no": it.po_no,
        "supplier": it.supplier.name if it.supplier else "—",
        "project_code": it.project_code,
        "expected_arrival": it.expected_arrival,
        "in_days": (_parse_d(it.expected_arrival) - today).days,
    } for it in r.scalars().all() if _parse_d(it.expected_arrival)]
    rows.sort(key=lambda x: x["expected_arrival"])
    return {"count": len(rows), "days": days, "items": rows[:20]}


async def tool_balance_due(db: AsyncSession) -> dict:
    """尾款到期/逾期清单：balance>0 且 balance_date 非空且 <= 今天+14 天（口径同 overdue.scan_balance_due）。"""
    today = _today()
    threshold = (today + timedelta(days=14)).isoformat()
    r = await db.execute(
        select(models.SalesLedger).where(
            models.SalesLedger.balance > 0,
            models.SalesLedger.balance_date.isnot(None),
            models.SalesLedger.balance_date != "",
            models.SalesLedger.balance_date <= threshold,
        )
    )
    rows = []
    for led in r.scalars().all():
        due = _parse_d(led.balance_date)
        if due is None:
            continue
        p = led.project
        rows.append({
            "project_code": p.code if p else f"#{led.project_id}",
            "project_name": p.name if p else "",
            "customer": led.customer, "balance": led.balance,
            "balance_date": led.balance_date,
            "days": (due - today).days,   # 负数=已逾期
            "sales": _uname(led.sales_user),
        })
    rows.sort(key=lambda x: x["balance_date"])
    return {"count": len(rows), "items": rows[:20]}


async def tool_overdue_orders(db: AsyncSession, dept: str | None = None) -> dict:
    """部门逾期任务：进行中且预计完成日已过（口径同 overdue.scan_overdue）。dept 可限定 design/electric/produce。"""
    today = _today()
    q = select(models.DeptOrder).where(
        models.DeptOrder.status == "in_progress",
        models.DeptOrder.due_date.isnot(None),
        models.DeptOrder.due_date < today.isoformat(),
    )
    if dept in DEPTS:
        q = q.where(models.DeptOrder.dept == dept)
    r = await db.execute(q)
    rows = []
    for o in r.scalars().all():
        due = _parse_d(o.due_date)
        if due is None:
            continue
        cfg = DEPTS.get(o.dept) or {}
        rows.append({
            "dept": o.dept, "dept_name": cfg.get("name", o.dept),
            "project_code": o.project.code if o.project else f"#{o.project_id}",
            "worker": _uname(o.worker),
            "due_date": o.due_date, "over_days": (today - due).days,
        })
    rows.sort(key=lambda x: -x["over_days"])
    return {"count": len(rows), "items": rows[:20]}


async def _hr_due_rows(db: AsyncSession) -> list[dict]:
    """人事到期：合同到期（30 天窗口，含已过期）/ 试用期转正（7 天窗口）（口径同 overdue.scan_hr_reminders）。"""
    today = _today()
    r = await db.execute(select(models.Employee).where(models.Employee.status != "离职"))
    rows = []
    for e in r.scalars().all():
        dept = e.department.name if e.department else "未分部门"
        for kind, dt_s, window in (("合同到期", e.contract_end, 30),
                                   ("试用期转正", e.regular_date if e.status == "试用" else None, 7)):
            due = _parse_d(dt_s)
            if due is None:
                continue
            days = (due - today).days
            if days <= window:
                rows.append({"kind": kind, "name": e.name, "dept": dept,
                             "date": dt_s, "days": days})
    rows.sort(key=lambda x: x["days"])
    return rows


async def tool_morning_report(db: AsyncSession) -> dict:
    """晨报聚合：采购到期未到货 / 部门逾期任务 / 尾款到期 / 人事到期，各取 Top5 + 总数。"""
    po, orders, balance, hr = (
        await tool_po_arrival_overdue(db),
        await tool_overdue_orders(db),
        await tool_balance_due(db),
        await _hr_due_rows(db),
    )
    return {
        "today": _today().isoformat(),
        "po_arrival_overdue": {"count": po["count"], "top": po["items"][:5]},
        "overdue_orders": {"count": orders["count"], "top": orders["items"][:5]},
        "balance_due": {"count": balance["count"], "top": balance["items"][:5]},
        "hr_due": {"count": len(hr), "top": hr[:5]},
    }


async def tool_project_status(db: AsyncSession, code: str) -> dict:
    """按项目编号查进度：基本信息 + 各部门任务 + 未到货采购项 + 尾款情况。"""
    code = (code or "").strip()
    r = await db.execute(select(models.Project).where(models.Project.code == code))
    p = r.scalar_one_or_none()
    if p is None:  # 兼容大小写差异再试一次
        r = await db.execute(select(models.Project).where(models.Project.code == code.upper()))
        p = r.scalar_one_or_none()
    if p is None:
        return {"found": False, "code": code}

    r = await db.execute(select(models.DeptOrder).where(models.DeptOrder.project_id == p.id))
    orders = [{
        "dept_name": (DEPTS.get(o.dept) or {}).get("name", o.dept),
        "status": _ORDER_STATUS_CN.get(o.status, o.status),
        "worker": _uname(o.worker),
        "start_date": o.start_date, "due_date": o.due_date, "done_date": o.done_date,
    } for o in r.scalars().all()]

    r = await db.execute(
        select(models.PurchaseItem).where(
            models.PurchaseItem.project_code == p.code,
            or_(models.PurchaseItem.arrival_date.is_(None),
                models.PurchaseItem.arrival_date == ""),
        )
    )
    today = _today()
    po_pending = []
    for it in r.scalars().all():
        exp = _parse_d(it.expected_arrival)
        po_pending.append({
            "item_name": it.item_name, "po_no": it.po_no,
            "supplier": it.supplier.name if it.supplier else "—",
            "expected_arrival": it.expected_arrival,
            "over_days": (today - exp).days if exp else None,
        })

    r = await db.execute(select(models.SalesLedger).where(models.SalesLedger.project_id == p.id))
    led = r.scalar_one_or_none()
    ledger = None
    if led is not None:
        due = _parse_d(led.balance_date)
        ledger = {
            "customer": led.customer, "amount": led.amount, "prepay": led.prepay,
            "before_ship": led.before_ship, "balance": led.balance,
            "balance_date": led.balance_date,
            "balance_days": (due - today).days if due else None,  # 负数=已逾期
            "sales": _uname(led.sales_user),
        }

    return {
        "found": True, "code": p.code, "name": p.name, "status": p.status,
        "is_deleted": bool(p.is_deleted), "manager": _uname(p.manager),
        "dept_orders": orders, "po_pending_count": len(po_pending),
        "po_pending": po_pending[:10], "ledger": ledger,
    }


# ==================== 工具注册表（LLM function calling + 降级模板共用） ====================

TOOL_LABELS = {
    "morning_report": "晨报聚合",
    "po_arrival_overdue": "采购到期未到货",
    "po_arriving": "预计到货",
    "balance_due": "尾款到期清单",
    "overdue_orders": "部门逾期任务",
    "project_status": "项目进度查询",
}

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "morning_report",
        "description": "晨报聚合：采购到期未到货/部门逾期任务/尾款到期/人事到期 各 Top5 + 计数",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "po_arrival_overdue",
        "description": "采购到期未到货明细（预计到货日期已过或当天但仍未收货），含超期天数/供应商/采购单号/项目编号",
        "parameters": {"type": "object", "properties": {
            "min_overdue_days": {"type": "integer", "description": "最小超期天数，默认 0（含当天到期）"}}}}},
    {"type": "function", "function": {
        "name": "po_arriving",
        "description": "未来 N 天预计到货的采购明细（默认 3 天）",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "未来天数，默认 3"}}}}},
    {"type": "function", "function": {
        "name": "balance_due",
        "description": "尾款到期/逾期清单（尾款>0 且约定日期在未来 14 天内或已逾期）",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "overdue_orders",
        "description": "各部门逾期未完成任务（设计/电工/生产）",
        "parameters": {"type": "object", "properties": {
            "dept": {"type": "string", "enum": ["design", "electric", "produce"],
                     "description": "部门，留空查全部"}}}}},
    {"type": "function", "function": {
        "name": "project_status",
        "description": "按项目编号查询项目进度：基本信息/各部门任务/未到货采购/尾款",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "项目编号，如 TH-2501"}}, "required": ["code"]}}},
]


async def _run_tool(name: str, args: dict, db: AsyncSession):
    if name == "morning_report":
        return await tool_morning_report(db)
    if name == "po_arrival_overdue":
        return await tool_po_arrival_overdue(db, int(args.get("min_overdue_days") or 0))
    if name == "po_arriving":
        return await tool_po_arriving(db, int(args.get("days") or 3))
    if name == "balance_due":
        return await tool_balance_due(db)
    if name == "overdue_orders":
        return await tool_overdue_orders(db, args.get("dept") or None)
    if name == "project_status":
        return await tool_project_status(db, str(args.get("code") or ""))
    return {"error": f"未知工具 {name}"}


# ==================== 大脑：OpenAI 兼容 function calling ====================

_SYSTEM_PROMPT = """你是制造业 ERP 项目管理系统内置的数据助手（只读）。严格遵守：
1. 只能根据工具返回的真实数据回答，严禁编造任何数字、日期、金额、项目编号、人名；
2. 工具没有返回的信息就如实说"系统里查不到"，不要推测、不要举例；
3. 回答用中文，简洁条目化（每项一行），金额保留原始数值，日期原样引用；
4. 需要数据时先调用工具，可连续调用多个；拿到工具结果后直接总结，不要重复调用同一工具；
5. 你只能查询，不能修改任何数据；用户要求改数据时明确拒绝。
今天日期：{today}（中国时区）。"""


async def _llm_request(messages: list[dict], model: str, cfg: dict) -> dict:
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOL_SCHEMAS,
        "tool_choice": "auto",
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as cli:
            r = await cli.post(url, json=payload, headers=headers)
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        # 安全红线：只透出状态码、掐断异常链——httpx 异常可能携带请求信息，绝不能把 key 泄进日志
        raise RuntimeError(f"LLM 接口返回 HTTP {e.response.status_code}") from None
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"LLM 调用失败（{type(e).__name__}）") from None


async def _chat_with_llm(message: str, history: list[dict], db: AsyncSession,
                         model: str, cfg: dict):
    """LLM 主路径：带 tools 请求 → 执行 tool_calls 回灌 → 再请模型总结。返回 (reply, sources)。"""
    sources: list[str] = []
    messages = ([{"role": "system", "content": _SYSTEM_PROMPT.format(today=_today().isoformat())}]
                + history + [{"role": "user", "content": message}])
    for _ in range(4):  # 工具轮次上限，防死循环
        data = await _llm_request(messages, model, cfg)
        msg = data["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            content = (msg.get("content") or "").strip()
            if not content:
                raise RuntimeError("LLM 返回空内容")
            return content, sources
        messages.append(msg)  # 含 tool_calls 的 assistant 消息原样回灌
        for tc in tool_calls[:4]:
            fn = (tc.get("function") or {})
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await _run_tool(name, args, db)
            if name in TOOL_LABELS and TOOL_LABELS[name] not in sources:
                sources.append(TOOL_LABELS[name])
            messages.append({
                "role": "tool", "tool_call_id": tc.get("id"),
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })
    raise RuntimeError("LLM 工具调用轮次超限")


# ==================== 降级：规则意图匹配 + 模板格式化（永远可用） ====================

_PROJECT_CODE_RE = re.compile(r"[A-Za-z]{2,}-?\d+")


def _fmt_money(x) -> str:
    return f"¥{(x or 0):,.0f}"


def _fmt_days(days: int) -> str:
    if days > 0:
        return f"还有 {days} 天"
    if days == 0:
        return "今天到期"
    return f"已逾期 {-days} 天"


def _po_overdue_text(d: dict) -> str:
    lines = [f"到期未到货采购共 {d['count']} 条" + ("（仅列前 20 条）：" if d["count"] > 20 else "：")]
    for it in d["items"]:
        po = f"采购单 {it['po_no']}，" if it.get("po_no") else ""
        proj = f"，项目 {it['project_code']}" if it.get("project_code") else ""
        when = "预计今天到货" if it["over_days"] == 0 else f"预计 {it['expected_arrival']} 到货，已超期 {it['over_days']} 天"
        lines.append(f"- {it['item_name']}（{po}供应商：{it['supplier']}{proj}）{when}")
    if d["count"] == 0:
        lines.append("- 暂无到期未到货的采购明细")
    return "\n".join(lines)


def _balance_text(d: dict) -> str:
    lines = [f"尾款到期（14 天内）或已逾期共 {d['count']} 条" + ("（仅列前 20 条）：" if d["count"] > 20 else "：")]
    for it in d["items"]:
        lines.append(f"- {it['project_code']} {it['project_name']}：尾款 {_fmt_money(it['balance'])}，"
                     f"约定 {it['balance_date']}（{_fmt_days(it['days'])}），销售：{it['sales']}")
    if d["count"] == 0:
        lines.append("- 暂无到期或逾期的尾款")
    return "\n".join(lines)


def _overdue_orders_text(d: dict) -> str:
    lines = [f"部门逾期任务共 {d['count']} 条" + ("（仅列前 20 条）：" if d["count"] > 20 else "：")]
    for it in d["items"]:
        lines.append(f"- {it['dept_name']} {it['project_code']}：预计 {it['due_date']} 完成，"
                     f"已逾期 {it['over_days']} 天（负责人：{it['worker']}）")
    if d["count"] == 0:
        lines.append("- 暂无逾期任务")
    return "\n".join(lines)


def _morning_text(d: dict) -> str:
    lines = [f"【今日晨报 {d['today']}】"]
    s = d["po_arrival_overdue"]
    lines.append(f"一、采购到期未到货：{s['count']} 条")
    for it in s["top"]:
        lines.append(f"  - {it['item_name']}（供应商：{it['supplier']}）预计 {it['expected_arrival']} 到货，"
                     + ("今天到期" if it["over_days"] == 0 else f"已超期 {it['over_days']} 天"))
    s = d["overdue_orders"]
    lines.append(f"二、部门逾期任务：{s['count']} 条")
    for it in s["top"]:
        lines.append(f"  - {it['dept_name']} {it['project_code']} 已逾期 {it['over_days']} 天（{it['worker']}）")
    s = d["balance_due"]
    lines.append(f"三、尾款到期/逾期：{s['count']} 条")
    for it in s["top"]:
        lines.append(f"  - {it['project_code']} 尾款 {_fmt_money(it['balance'])}，{_fmt_days(it['days'])}")
    s = d["hr_due"]
    lines.append(f"四、人事到期：{s['count']} 条")
    for it in s["top"]:
        lines.append(f"  - {it['name']}（{it['dept']}）{it['kind']} {it['date']}（{_fmt_days(it['days'])}）")
    return "\n".join(lines)


def _project_text(d: dict) -> str:
    if not d.get("found"):
        return f"系统里查不到项目编号「{d['code']}」，请核对编号后重试。"
    lines = [f"【项目 {d['code']} {d['name']}】" + ("（已删除）" if d.get("is_deleted") else ""),
             f"状态：{d['status']}；负责人：{d['manager']}"]
    lines.append(f"各部门任务（{len(d['dept_orders'])} 条）：")
    for o in d["dept_orders"]:
        segs = [f"  - {o['dept_name']}：{o['status']}（负责人：{o['worker']}"]
        if o.get("due_date"):
            segs.append(f"，预计 {o['due_date']} 完成")
        if o.get("done_date"):
            segs.append(f"，实际 {o['done_date']} 完成")
        lines.append("".join(segs) + "）")
    if not d["dept_orders"]:
        lines.append("  - 暂无部门任务单")
    lines.append(f"未到货采购项：{d['po_pending_count']} 项")
    for it in d["po_pending"]:
        over = ""
        if it.get("over_days") is not None and it["over_days"] > 0:
            over = f"，已超期 {it['over_days']} 天"
        exp = f"，预计 {it['expected_arrival']} 到货" if it.get("expected_arrival") else "，未填预计到货"
        lines.append(f"  - {it['item_name']}（供应商：{it['supplier']}{exp}{over}）")
    led = d.get("ledger")
    if led:
        bal = led["balance"] or 0
        if bal > 0:
            when = _fmt_days(led["balance_days"]) if led.get("balance_days") is not None else "未约定日期"
            lines.append(f"尾款：{_fmt_money(bal)} 未收（约定 {led.get('balance_date') or '—'}，{when}）；"
                         f"合同额 {_fmt_money(led['amount'])}，客户：{led.get('customer') or '—'}")
        else:
            lines.append(f"尾款：已结清；合同额 {_fmt_money(led['amount'])}，客户：{led.get('customer') or '—'}")
    else:
        lines.append("尾款：无销售台账记录")
    return "\n".join(lines)


_CAPABILITY_TEXT = """我是 ERP 数据助手（只读），所有数字都来自系统实时查询。目前可以回答：
- 「今日晨报」：采购未到货 / 逾期任务 / 尾款 / 人事到期一览
- 「采购未到货」：到期仍未收货的采购明细
- 「未来几天到货」：预计到货的采购明细
- 「尾款到期」：14 天内到期或已逾期的尾款
- 「逾期任务」：各部门逾期未完成任务
- 单项目进度：消息里带上项目编号，如「TH-2501 进度」"""


async def _rule_chat(message: str, db: AsyncSession):
    """规则降级：关键词意图匹配 → 调对应数据工具 → 模板格式化。返回 (reply, sources)。"""
    m = message.strip()
    if any(k in m for k in ("晨报", "早报", "早会")):
        return _morning_text(await tool_morning_report(db)), [TOOL_LABELS["morning_report"]]
    if any(k in m for k in ("未到货", "采购", "到货")):
        if any(k in m for k in ("未来", "预计", "即将", "将要")) and "未到货" not in m and "超期" not in m:
            d = await tool_po_arriving(db)
            lines = [f"未来 {d['days']} 天预计到货共 {d['count']} 条："]
            lines += [f"- {it['item_name']}（供应商：{it['supplier']}）预计 {it['expected_arrival']} 到货"
                      for it in d["items"]] or ["- 暂无"]
            return "\n".join(lines), [TOOL_LABELS["po_arriving"]]
        return _po_overdue_text(await tool_po_arrival_overdue(db)), [TOOL_LABELS["po_arrival_overdue"]]
    if any(k in m for k in ("尾款", "回款")):
        return _balance_text(await tool_balance_due(db)), [TOOL_LABELS["balance_due"]]
    if "逾期" in m:
        return _overdue_orders_text(await tool_overdue_orders(db)), [TOOL_LABELS["overdue_orders"]]
    hit = _PROJECT_CODE_RE.search(m)
    if hit:
        return _project_text(await tool_project_status(db, hit.group(0))), [TOOL_LABELS["project_status"]]
    return _CAPABILITY_TEXT, []


# ==================== 接口 ====================

_AGENT_CFG_FIELDS = ("base_url", "api_key", "model", "models")
_CLEAR_MARK = "-"   # PUT /config 字段传 "-" = 清除库中覆盖值，回退 .env 默认


def _model_whitelist(cfg: dict | None = None) -> list[str]:
    """可选模型白名单（逗号分隔解析，去空白去重保序）。
    默认模型不在名单里时自动并入，保证默认值总能选到。
    cfg 缺省用 settings(.env) 默认值；接口内传「生效配置」。"""
    if cfg is None:
        cfg = {"model": settings.agent_llm_model, "models": settings.agent_llm_models}
    ws = [w.strip() for w in (cfg.get("models") or "").split(",") if w.strip()]
    out = list(dict.fromkeys(ws))
    if cfg.get("model") and cfg["model"] not in out:
        out.append(cfg["model"])
    return out


async def _effective_llm_config(db: AsyncSession) -> dict:
    """生效 LLM 配置 = 数据库 app_settings 已配置值 > settings(.env) 默认值。
    每次请求实时读库（量极小），admin 页面保存后全局立即生效。"""
    r = await db.execute(select(models.AppSetting).where(
        models.AppSetting.key.in_([f"agent_llm.{f}" for f in _AGENT_CFG_FIELDS])))
    stored = {row.key.rsplit(".", 1)[1]: (row.value or "").strip() for row in r.scalars().all()}
    return {
        "base_url": stored.get("base_url") or settings.agent_llm_base_url,
        "api_key":   stored.get("api_key")   or settings.agent_llm_api_key,
        "model":     stored.get("model")     or settings.agent_llm_model,
        "models":    stored.get("models")    or settings.agent_llm_models,
    }


def _mask_key(key: str) -> str:
    """api_key 打码：只露最后 4 位；不足 4 位全打码。任何接口/日志都不得输出明文。"""
    if not key:
        return ""
    return "****" + key[-4:] if len(key) > 4 else "****"


def _config_out(cfg: dict) -> dict:
    return {"base_url": cfg["base_url"], "model": cfg["model"], "models": cfg["models"],
            "api_key_masked": _mask_key(cfg["api_key"]), "has_key": bool(cfg["api_key"])}


async def _require_admin_only(current: models.User = Depends(get_current_user)) -> models.User:
    """Agent LLM 配置仅 admin（deps.require_admin 实际含 manager，此处要更严：manager 也 403）。"""
    if not current.has_role("admin"):
        raise HTTPException(403, "仅管理员可配置")
    return current


@router.get("/models")
async def list_models(
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """可选模型列表 + 默认模型 + 是否已配置 LLM Key（走生效配置；不泄露 api_key 本身）。"""
    cfg = await _effective_llm_config(db)
    return {
        "models": _model_whitelist(cfg),
        "default": cfg["model"],
        "llm_enabled": bool(cfg["api_key"]),
    }


@router.get("/config")
async def get_agent_config(
    current: models.User = Depends(_require_admin_only),
    db: AsyncSession = Depends(get_db),
):
    """当前生效 LLM 配置（仅 admin）。api_key 永远只回打码值，不回明文。"""
    return _config_out(await _effective_llm_config(db))


class AgentConfigIn(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    models: str | None = None


@router.put("/config")
async def update_agent_config(
    body: AgentConfigIn,
    current: models.User = Depends(_require_admin_only),
    db: AsyncSession = Depends(get_db),
):
    """保存 LLM 配置（仅 admin，全局生效）。字段均可选：
    空字符串 = 保持不变（防页面回显打码值被误存）；"-" = 清除库中覆盖值回退 .env 默认；其余 = 覆盖。"""
    cfg = await _effective_llm_config(db)   # 校验基准：改动后的生效配置
    writes: dict[str, str | None] = {}      # 待落库；值=None 表示删除该覆盖项
    for f in _AGENT_CFG_FIELDS:
        raw = getattr(body, f)
        if raw is None:
            continue
        v = raw.strip()
        if not v:
            continue                        # 空串=保持不变
        if v == _CLEAR_MARK:
            writes[f] = None
            cfg[f] = getattr(settings, f"agent_llm_{f}")
        else:
            writes[f] = v
            cfg[f] = v
    # 存库前校验（只校验本次改动的维度）
    if "base_url" in writes and not cfg["base_url"].lower().startswith("http"):
        raise HTTPException(400, "Base URL 必须以 http(s) 开头")
    if "model" in writes or "models" in writes:
        wl = [w.strip() for w in cfg["models"].split(",") if w.strip()]
        if not wl:
            raise HTTPException(400, "可选模型列表不能为空")
        if not cfg["model"]:
            cfg["model"] = wl[0]
            writes["model"] = wl[0]
        if cfg["model"] not in wl:          # 默认模型自动并入白名单（与白名单逻辑一致）
            wl.append(cfg["model"])
            cfg["models"] = ",".join(wl)
            writes["models"] = cfg["models"]
    # 落库（upsert / 删除覆盖项）
    for f, v in writes.items():
        key = f"agent_llm.{f}"
        row = await db.get(models.AppSetting, key)
        if v is None:
            if row is not None:
                await db.delete(row)
        elif row is None:
            db.add(models.AppSetting(key=key, value=v))
        else:
            row.value = v
    await db.commit()
    # 安全红线：日志只记改了哪些字段，绝不记值（api_key 明文不落日志）
    log.info("[agent] LLM 配置已由 %s 更新（字段：%s）", current.username, ",".join(writes) or "无")
    return _config_out(cfg)


class ChatMsg(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    message: str
    history: list[ChatMsg] = []
    model: str | None = None   # 🆕 可选：指定 LLM 模型（须在白名单内）；规则降级路径忽略


@router.post("/chat")
async def chat(
    body: ChatIn,
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(400, "请输入问题")
    cfg = await _effective_llm_config(db)
    # 模型白名单校验（无论走 LLM 还是降级，非法模型都直接 400，与现有参数校验风格一致）
    model = (body.model or "").strip() or None
    if model is not None and model not in _model_whitelist(cfg):
        raise HTTPException(400, f"无效模型「{model}」，可选：{'、'.join(_model_whitelist(cfg))}")
    # 最多保留最近 10 轮（20 条），仅 user/assistant 两种角色
    history = [{"role": h.role, "content": h.content[:2000]}
               for h in body.history[-20:] if h.role in ("user", "assistant")]
    if cfg["api_key"]:
        try:
            reply, sources = await _chat_with_llm(text, history, db, model or cfg["model"], cfg)
            return {"reply": reply, "fallback": False, "sources": sources}
        except Exception as e:  # noqa: BLE001 —— LLM 任何异常都降级，保证可用
            log.warning("[agent] LLM 调用失败，转规则降级: %s", e)
    reply, sources = await _rule_chat(text, db)
    return {"reply": reply, "fallback": True, "sources": sources}
