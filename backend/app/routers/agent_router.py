"""🆕 AI 助手（只读问数，全员可用；数据查询权限按用户菜单门控）。

- POST /api/agent/chat  {message, history?, model?} → {reply, fallback, sources, suggestions}
  - history 可选，最多保留最近 10 轮（20 条）
  - sources 列出本轮实际调用的数据工具（前端小字展示「数据来源」）
  - suggestions 为追问建议（按实际调用的工具映射，且过滤掉无权工具，前端渲染为可点击 chips）
  - 🆕 权限：登录即可聊；数据域按 user_menu_keys 门控（见 _run_tool，LLM 与规则降级共用）：
    采购域→purchase_mgmt；尾款→finance/sales；部门逾期→design/electric/produce（按菜单交集过滤）；
    项目进度→list + 行级可见性（deps.user_can_view_project），ledger 仅 finance/sales 返回；
    晨报按可用域聚合；无权域返回 {"error": ...} 不返回数据
- 大脑：OpenAI 兼容接口 function calling（30s 超时）。LLM 配置生效优先级 =
  数据库 app_settings（admin 在页面配置，GET/PUT /api/agent/config）> settings(.env) 默认值；
  api_key 任何接口/日志都不输出明文
- 降级：生效配置无 api_key、或 LLM 调用任何异常 → 规则意图匹配 + 模板格式化，
  fallback=true；降级路径不依赖任何外部服务，永远可用
- 🆕 审计：两条路径的问答都写 agent_chat_logs（用户/问题/回答/工具/模型/耗时）；
  写日志失败只记 log 不影响聊天；GET /api/agent/chat-logs（admin/manager）分页查询
- 只读红线：本模块所有数据工具仅 SELECT，不做任何写库操作
- 查询口径照抄 overdue.py（采购到期未到货/部门逾期任务/尾款到期/人事到期），
  日期字段为 ISO 字符串可直接字典序比较；业务时区中国 UTC+8（复用 overdue._CN_TZ）
"""
import asyncio
import json
import logging
import re
import time
from datetime import date, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from .. import menus, models
from ..config import settings
from ..database import get_db, SessionLocal
from ..deps import require_admin_or_manager, get_current_user, user_can_view_project
from ..dept_config import DEPTS
from ..overdue import _CN_TZ
from ..utils import write_audit
# 🆕 行级可见性：**复用页面本体的谓词，不在这里重写一遍角色判断**。
#   重写是上一版越权的根因——页面改了规则，工具这边不会跟。这几个函数是唯一真源：
#   _buyer_restricted 有反直觉语义（兼任 finance/logistics 不解除采购隔离），自己写必然踩坑。
from .purchase_mgmt_router import _buyer_restricted
from .sales_router import _all_view as _sales_all_view
from .orders_router import _is_mgr as _orders_is_mgr, _is_lead as _orders_is_lead

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

def _scope_po(q, current: models.User):
    """采购行级隔离：与 /api/purchase-mgmt/items 列表同口径（purchase_mgmt_router.py:349）。"""
    if _buyer_restricted(current):
        q = q.where(models.PurchaseItem.buyer_id == current.id)
    return q


async def _po_arrival_overdue_rows(db: AsyncSession, current: models.User,
                                   min_overdue_days: int = 0) -> list[dict]:
    """到期未到货的**全量**行（未截断）——聚合类工具必须基于它，不能基于 Top-N 的结果再聚合。"""
    today = _today()
    q = _scope_po(
        select(models.PurchaseItem).where(
            models.PurchaseItem.expected_arrival.isnot(None),
            models.PurchaseItem.expected_arrival != "",
            models.PurchaseItem.expected_arrival <= today.isoformat(),
            or_(models.PurchaseItem.arrival_date.is_(None),
                models.PurchaseItem.arrival_date == ""),
        ), current)
    r = await db.execute(q)
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
    return rows


async def tool_po_arrival_overdue(db: AsyncSession, current: models.User,
                                  min_overdue_days: int = 0) -> dict:
    """采购到期未到货明细：预计到货日期已到(含当天)且仍未收货（口径同 overdue.scan_po_arrival_overdue）。"""
    rows = await _po_arrival_overdue_rows(db, current, min_overdue_days)
    return {"count": len(rows), "items": rows}


async def tool_po_arriving(db: AsyncSession, current: models.User, days: int = 3) -> dict:
    """未来 N 天（含今天）预计到货、目前仍未收货的采购明细。"""
    today = _today()
    end = (today + timedelta(days=max(days, 0))).isoformat()
    r = await db.execute(_scope_po(
        select(models.PurchaseItem).where(
            models.PurchaseItem.expected_arrival.isnot(None),
            models.PurchaseItem.expected_arrival != "",
            models.PurchaseItem.expected_arrival >= today.isoformat(),
            models.PurchaseItem.expected_arrival <= end,
            or_(models.PurchaseItem.arrival_date.is_(None),
                models.PurchaseItem.arrival_date == ""),
        ), current)
    )
    rows = [{
        "item_name": it.item_name, "po_no": it.po_no,
        "supplier": it.supplier.name if it.supplier else "—",
        "project_code": it.project_code,
        "expected_arrival": it.expected_arrival,
        "in_days": (_parse_d(it.expected_arrival) - today).days,
    } for it in r.scalars().all() if _parse_d(it.expected_arrival)]
    rows.sort(key=lambda x: x["expected_arrival"])
    return {"count": len(rows), "days": days, "items": rows}


async def tool_po_overdue_by_supplier(db: AsyncSession, current: models.User) -> dict:
    """到期未到货按供应商聚合：每个供应商的未收货条数、最大超期天数、涉及项目（口径同 tool_po_arrival_overdue）。
    🆕 修口径 bug：此前在**已截断的 Top-20 明细**上聚合，count 却用全量总数，两个数字对不上；
    现基于全量行聚合，只在最后截断供应商条数。"""
    all_rows = await _po_arrival_overdue_rows(db, current, 0)
    agg: dict[str, dict] = {}
    for it in all_rows:
        a = agg.setdefault(it["supplier"], {"supplier": it["supplier"], "count": 0,
                                            "max_over_days": 0, "projects": set()})
        a["count"] += 1
        a["max_over_days"] = max(a["max_over_days"], it["over_days"])
        if it.get("project_code"):
            a["projects"].add(it["project_code"])
    rows = sorted(agg.values(), key=lambda x: (-x["max_over_days"], -x["count"]))
    for a in rows:
        a["projects"] = sorted(a["projects"])
    return {"count": len(rows), "item_total": len(all_rows), "suppliers": rows}


async def tool_balance_due(db: AsyncSession, current: models.User) -> dict:
    """尾款到期/逾期清单：balance>0 且 balance_date 非空且 <= 今天+14 天（口径同 overdue.scan_balance_due）。"""
    today = _today()
    threshold = (today + timedelta(days=14)).isoformat()
    q = select(models.SalesLedger).where(
        models.SalesLedger.balance > 0,
        models.SalesLedger.balance_date.isnot(None),
        models.SalesLedger.balance_date != "",
        models.SalesLedger.balance_date <= threshold,
    )
    # 销售行级隔离：与 /api/sales/ledger 同口径（sales_router.py:239）——非管理层/非销售主管只看本人
    if not _sales_all_view(current):
        q = q.where(models.SalesLedger.sales_uid == current.id)
    r = await db.execute(q)
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
    return {"count": len(rows), "items": rows}


async def tool_overdue_orders(db: AsyncSession, current: models.User, dept: str | None = None,
                              allowed_depts: list[str] | None = None) -> dict:
    """部门逾期任务：进行中且预计完成日已过（口径同 overdue.scan_overdue）。
    dept 可限定 design/electric/produce；未指定 dept 时可传 allowed_depts 按调用者菜单收窄。"""
    today = _today()
    q = select(models.DeptOrder).where(
        models.DeptOrder.status == "in_progress",
        models.DeptOrder.due_date.isnot(None),
        models.DeptOrder.due_date < today.isoformat(),
    )
    if dept in DEPTS:
        q = q.where(models.DeptOrder.dept == dept)
    elif allowed_depts is not None:
        q = q.where(models.DeptOrder.dept.in_(allowed_depts))
    # 任务行级隔离：与 orders_router 同口径——管理层看全部；部门主管看本部门全部；
    # 其余（工人）只看派给自己的单。主管可能只主管其中一个部门，故按部门逐个放行再并上本人。
    if not _orders_is_mgr(current):
        lead_depts = [d for d in DEPTS if _orders_is_lead(current, d)]
        cond = models.DeptOrder.worker_id == current.id
        if lead_depts:
            cond = or_(cond, models.DeptOrder.dept.in_(lead_depts))
        q = q.where(cond)
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
    return {"count": len(rows), "items": rows}


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


async def tool_morning_report(db: AsyncSession, current: models.User,
                              domains: set[str] | None = None,
                              order_depts: list[str] | None = None) -> dict:
    """晨报聚合：采购到期未到货 / 部门逾期任务 / 尾款到期 / 人事到期，各取 Top5 + 总数。
    🆕 domains ⊆ {"po","orders","balance","hr"} 指定只统计调用者有菜单的小节（缺省=全部）；
    order_depts 把「部门逾期」小节收窄到调用者有菜单的部门。"""
    dom = domains if domains is not None else {"po", "orders", "balance", "hr"}
    out: dict = {"today": _today().isoformat()}
    if "po" in dom:
        po = await tool_po_arrival_overdue(db, current)
        out["po_arrival_overdue"] = {"count": po["count"], "top": po["items"][:5]}
    if "orders" in dom:
        orders = await tool_overdue_orders(db, current, allowed_depts=order_depts)
        out["overdue_orders"] = {"count": orders["count"], "top": orders["items"][:5]}
    if "balance" in dom:
        balance = await tool_balance_due(db, current)
        out["balance_due"] = {"count": balance["count"], "top": balance["items"][:5]}
    if "hr" in dom:
        hr = await _hr_due_rows(db)
        out["hr_due"] = {"count": len(hr), "top": hr[:5]}
    return out


async def tool_project_status(db: AsyncSession, code: str, current: models.User | None = None,
                              menu_keys: set[str] | None = None) -> dict:
    """按项目编号查进度：基本信息 + 各部门任务 + 未到货采购项 + 尾款情况。
    🆕 传 current 时做行级可见性门禁（deps.user_can_view_project，与项目详情同一判定）；
    ledger（尾款金额）仅当 menu_keys 含 finance/sales 时返回，否则剔除该字段。"""
    code = (code or "").strip()
    r = await db.execute(select(models.Project).where(models.Project.code == code))
    p = r.scalar_one_or_none()
    if p is None:  # 兼容大小写差异再试一次
        r = await db.execute(select(models.Project).where(models.Project.code == code.upper()))
        p = r.scalar_one_or_none()
    if p is None:
        return {"found": False, "code": code}
    # 🆕 行级可见性：受限岗位/非项目成员查不到的项目一律「无权查看」（不泄露项目是否存在）
    if current is not None and not await user_can_view_project(db, current, p):
        return {"error": "无权查看该项目", "code": p.code}

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

    out = {
        "found": True, "code": p.code, "name": p.name, "status": p.status,
        "is_deleted": bool(p.is_deleted), "manager": _uname(p.manager),
        "dept_orders": orders, "po_pending_count": len(po_pending),
        "po_pending": po_pending[:10],
    }
    # 🆕 尾款金额仅财务/销售菜单可见，其余剔除该字段
    if menu_keys is None or (menu_keys & {"finance", "sales"}):
        out["ledger"] = ledger
    return out


# ==================== 工具注册表（LLM function calling + 降级模板共用） ====================

TOOL_LABELS = {
    "morning_report": "晨报聚合",
    "po_arrival_overdue": "采购到期未到货",
    "po_arriving": "预计到货",
    "po_overdue_by_supplier": "未到货·按供应商汇总",
    "balance_due": "尾款到期清单",
    "overdue_orders": "部门逾期任务",
    "project_status": "项目进度查询",
    # 🆕 第二批：销售/台账域。依据是杨坛真实操作轨迹（台账 243 次、请款 40 笔、
    #    收货人 34 次、销售订单 29 次；采购 0 次），不是拍脑袋。
    "receivable_blind": "盯不住的应收",
    "shipment_receiver": "待填收货人",
    "ledger_incomplete": "台账缺件",
    "leads_followup": "线索待跟进",
    "order_pending": "待审批销售订单",
    "invoice_pending": "待开票",
    # 🆕 v2 阶段一：find → get 递进。依据是两位管理层近 30 天真实操作
    #    （杨坛 188 次里销售台账+订单占 32%；赵仁辉 635 次里仓库占 36%，
    #     而 v1 对赵仁辉的工具覆盖率只有 5%）。
    "find_entity": "找实体",
    "get_customer": "客户全景",
    "get_project": "项目全景",
    "get_supplier": "供应商画像",
    "get_material": "物料全景",
}

# 🆕 每个工具一句人话说明：给用户看的（门户小字、能力清单），也给模型当选型依据。
TOOL_DESC = {
    "find_entity": "说个大概的名字就能找到项目/客户/供应商/物料，不用记编号",
    "get_customer": "这家客户一共几单、收了多少、还欠多少、卡在哪一步",
    "get_project": "一个项目从台账到发货全看完：收款、各部门任务、采购在途、发货状态",
    "get_supplier": "这家供应商准时率多少、平均拖几天、现在还欠几批货",
    "get_material": "这个物料还有多少库存、低不低于安全线、最近进出了多少",
    "morning_report": "把今天要盯的事聚成一条：采购超期、尾款到期、部门逾期、人事到期",
    "po_arrival_overdue": "预计到货日已过、货还没到的采购明细",
    "po_arriving": "未来几天预计能到的料，用来提前安排生产",
    "po_overdue_by_supplier": "把未到货按供应商归堆，看哪家拖得最狠",
    "balance_due": "14 天内到期或已逾期的尾款（只含填了到期日的）",
    "overdue_orders": "各部门预计完成日已过、仍未完成的任务",
    "project_status": "按项目编号查这个项目的进度、采购、尾款",
    "receivable_blind": "催办和尾款清单都盯不到的应收：没填到期日的尾款 + 发货款应收",
    "shipment_receiver": "已建发货单但收货人还空着的，填了才能送货签收",
    "ledger_incomplete": "台账缺合同额或客户的行；合同额为 0 会让毛利算成假亏损",
    "leads_followup": "既没成交也没丢单、还挂着的销售线索",
    "order_pending": "销售下了单、等主管审批的订单",
    "invoice_pending": "已申请开票、等财务出票的台账行",
}

# 追问建议：按实际调用的工具映射固定建议（去重保序，取前 3 条）
_TOOL_SUGGESTIONS = {
    "morning_report": ["采购未到货明细", "尾款到期清单", "部门逾期任务"],
    "po_arrival_overdue": ["按供应商汇总未到货", "未来 7 天到货", "今日晨报"],
    "po_arriving": ["采购未到货", "今日晨报"],
    "po_overdue_by_supplier": ["采购未到货明细", "未来 7 天到货"],
    "balance_due": ["今日晨报", "部门逾期任务"],
    "overdue_orders": ["今日晨报", "采购未到货"],
    "project_status": ["该项目未到货采购", "尾款到期", "今日晨报"],
}
_DEFAULT_SUGGESTIONS = ["今日晨报", "采购未到货", "尾款到期"]

# 🆕 建议文案 → 对应数据工具（按调用者可用工具过滤建议用）
_SUGGESTION_TOOL = {
    "今日晨报": "morning_report",
    "采购未到货明细": "po_arrival_overdue",
    "采购未到货": "po_arrival_overdue",
    "按供应商汇总未到货": "po_overdue_by_supplier",
    "未来 7 天到货": "po_arriving",
    "尾款到期清单": "balance_due",
    "尾款到期": "balance_due",
    "部门逾期任务": "overdue_orders",
    "该项目未到货采购": "project_status",
}


def _suggestions_for(tool_names, allowed: set[str] | None = None) -> list[str]:
    out: list[str] = []

    def _ok(s: str) -> bool:
        return allowed is None or _SUGGESTION_TOOL.get(s) in allowed

    for n in tool_names:
        for s in _TOOL_SUGGESTIONS.get(n, []):
            if s not in out and _ok(s):
                out.append(s)
    if not out:
        out = [s for s in _DEFAULT_SUGGESTIONS if _ok(s)]
    return out[:3]

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "morning_report",
        "description": "晨报聚合：采购到期未到货/部门逾期任务/尾款到期/人事到期 各 Top5 + 计数",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "最多返回几条明细，默认 20；用户说「全部/都列出来/完整清单」时传 200"}}}}},
    {"type": "function", "function": {
        "name": "po_arrival_overdue",
        "description": "采购到期未到货明细（预计到货日期已过或当天但仍未收货），含超期天数/供应商/采购单号/项目编号",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "最多返回几条明细，默认 20；用户说「全部/都列出来/完整清单」时传 200"},
            
            "min_overdue_days": {"type": "integer", "description": "最小超期天数，默认 0（含当天到期）"}}}}},
    {"type": "function", "function": {
        "name": "po_arriving",
        "description": "未来 N 天预计到货的采购明细（默认 3 天）",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "最多返回几条明细，默认 20；用户说「全部/都列出来/完整清单」时传 200"},
            
            "days": {"type": "integer", "description": "未来天数，默认 3"}}}}},
    {"type": "function", "function": {
        "name": "po_overdue_by_supplier",
        "description": "到期未到货按供应商聚合：每家供应商的未收货条数、最大超期天数、涉及项目",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "最多返回几条明细，默认 20；用户说「全部/都列出来/完整清单」时传 200"}}}}},
    {"type": "function", "function": {
        "name": "balance_due",
        "description": "尾款到期/逾期清单（尾款>0 且约定日期在未来 14 天内或已逾期）",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "最多返回几条明细，默认 20；用户说「全部/都列出来/完整清单」时传 200"}}}}},
    {"type": "function", "function": {
        "name": "overdue_orders",
        "description": "各部门逾期未完成任务（设计/电工/生产）",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "最多返回几条明细，默认 20；用户说「全部/都列出来/完整清单」时传 200"},
            
            "dept": {"type": "string", "enum": ["design", "electric", "produce"],
                     "description": "部门，留空查全部"}}}}},
    {"type": "function", "function": {
        "name": "project_status",
        "description": "按项目编号查询项目进度：基本信息/各部门任务/未到货采购/尾款",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "最多返回几条明细，默认 20；用户说「全部/都列出来/完整清单」时传 200"},
            
            "code": {"type": "string", "description": "项目编号，如 TH-2501"}}, "required": ["code"]}}},
]

# 🆕 第二批工具的 schema 由 TOOL_DESC 自动生成——它们都无参数，
#   手工再抄一遍 description 就是给漂移留口子（手册 6.3 说的七处同步问题）。
TOOL_SCHEMAS += [{
    "type": "function",
    "function": {"name": _n, "description": TOOL_DESC[_n],
                 "parameters": {"type": "object", "properties": {}}},
} for _n in ("receivable_blind", "shipment_receiver", "ledger_incomplete",
             "leads_followup", "order_pending", "invoice_pending")]


# ══════════ v2 阶段一：find → get 递进（docs/agent-architecture-v2.md）══════════
# v1 的 13 个工具里 12 个是「列一类」，多轮循环无事可做——第一轮列完就没下一步。
# 下面这 5 个才让 ReAct 有链可走：find_entity 拿到实体 → get_* 纵深 → 再决定下一步。
_LIM_PROP = {"limit": {"type": "integer",
                       "description": "最多返回几条明细，默认 20；用户要「全部」时传 200"}}

TOOL_SCHEMAS += [
    {"type": "function", "function": {
        "name": "find_entity",
        "description": "模糊词找实体（项目/客户/供应商/物料）。用户说「南京那个项目」「迈克斯」"
                       "「诺朋」这类不完整的名字时**先调它**拿到准确名称，再调 get_*",
        "parameters": {"type": "object", "properties": {
            "q": {"type": "string", "description": "用户说的那个词，原样传"},
            "kind": {"type": "string", "enum": ["project", "customer", "supplier", "material"],
                     "description": "限定只找某一类；不确定就别传"},
        }, "required": ["q"]}}},
    {"type": "function", "function": {
        "name": "get_customer",
        "description": "客户全景：这家客户所有台账、合同额、已收/未收、回款到哪一步、"
                       "几笔尾款没填到期日、几笔发货款其实还没发货",
        "parameters": {"type": "object", "properties": dict(
            _LIM_PROP, name={"type": "string", "description": "客户名，可以不全"}),
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "get_project",
        "description": "项目全景：台账收款分解 + 各部门任务与逾期 + 采购未到货 + 发货状态，一次给全",
        "parameters": {"type": "object", "properties": dict(
            _LIM_PROP, code={"type": "string", "description": "项目编号或名称"}),
            "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "get_supplier",
        "description": "供应商画像：准时率、平均超期天数、最大超期、当前未到货明细。"
                       "回答「哪家供应商靠不住」「要不要换供应商」用它",
        "parameters": {"type": "object", "properties": dict(
            _LIM_PROP, name={"type": "string", "description": "供应商名，可以不全"}),
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "get_material",
        "description": "物料全景：当前库存、安全库存、是否低于安全线及缺口、库位、近期出入库流水",
        "parameters": {"type": "object", "properties": dict(
            _LIM_PROP, q={"type": "string", "description": "物料编码或名称或规格"}),
            "required": ["q"]}}},
]



# ==================== 🆕 数据域-菜单门控（LLM 与规则降级共用） ====================

_MENU_LABELS = {m["key"]: m["label"] for m in menus.MENU_DEFS}
_DEPT_MENU_KEYS = ("design", "electric", "produce")


def _deny(*need: str) -> dict:
    """无权查询的统一错误结果（作为工具返回值，不进异常链）。"""
    return {"error": f"你无权查询该域数据（需要 {' 或 '.join(_MENU_LABELS.get(k, k) for k in need)} 菜单权限）"}


def _allowed_tools(user: models.User) -> set[str]:
    """用户菜单可用的数据工具集合（morning_report 只要晨报任一数据域可用即可，工具内再按域聚合；
    project_status 不算晨报域）。"""
    keys = set(menus.user_menu_keys(user))
    out: set[str] = set()
    if "purchase_mgmt" in keys:
        out |= {"po_arrival_overdue", "po_arriving", "po_overdue_by_supplier"}
    if keys & {"finance", "sales"}:
        # 🆕 销售/台账域第二批：与 balance_due 同一道菜单门控。
        #   admin/manager 走 menus.user_menu_keys 的全量分支，自动拿到全部。
        out |= {"balance_due", "receivable_blind", "ledger_incomplete",
                "order_pending", "invoice_pending", "leads_followup"}
    if keys & {"finance", "sales", "logistics"}:
        out.add("shipment_receiver")
    if keys & set(_DEPT_MENU_KEYS):
        out.add("overdue_orders")
    if "list" in keys:
        out.add("project_status")
        out.add("get_project")
    # 🆕 v2：找实体是所有纵深查询的入口，任何能查数的人都该有
    if out:
        out.add("find_entity")
    if keys & {"finance", "sales"}:
        out.add("get_customer")
    if "purchase_mgmt" in keys:
        out.add("get_supplier")
    if "warehouse" in keys:
        # 赵仁辉 36% 的操作在仓库，v1 一个仓库工具都没有
        out.add("get_material")
    if "purchase_mgmt" in keys or "hr" in keys or keys & {"finance", "sales", *_DEPT_MENU_KEYS}:
        out.add("morning_report")
    return out


_LIMIT_MAX = 200      # 再多模型也读不完，而且会把上下文顶爆


def _cap(result, args: dict):
    """按调用方要的 limit 截断，并**把截断说清楚**。

    ⚠️ 以前每个工具写死 `rows[:20]`，用户问「全部列举」时结构上就给不出来，
       而且回答里完全不提「我只给了 20 条」——用户以为那就是全部。
       现在：limit 可调（默认 20，上限 200），并回 `shown/truncated`，
       系统提示词里要求截断必须明说。
    """
    if not isinstance(result, dict):
        return result
    try:
        lim = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        lim = 20
    lim = max(1, min(lim, _LIMIT_MAX))
    for key in ("items", "suppliers", "rows"):
        rows = result.get(key)
        if isinstance(rows, list):
            total = result.get("count")
            total = total if isinstance(total, int) else len(rows)
            result[key] = rows[:lim]
            result["shown"] = len(result[key])
            result["truncated"] = max(0, total - len(result[key]))
    return result


async def _run_tool(name: str, args: dict, db: AsyncSession, current: models.User):
    """统一出口：所有工具结果都过 _cap，保证 limit 生效、截断被记录下来。
    别在各个 tool_xxx 里各写各的截断——那样 limit 参数形同虚设（踩过）。"""
    return _cap(await _run_tool_inner(name, args, db, current), args)


async def _run_tool_inner(name: str, args: dict, db: AsyncSession, current: models.User):
    # 🆕 第二批工具：独立模块，签名统一 (db, current)，无额外参数
    from ..agent import tools_sales as _ts
    _SECOND = {
        "receivable_blind": _ts.tool_receivable_blind,
        "shipment_receiver": _ts.tool_shipment_receiver,
        "ledger_incomplete": _ts.tool_ledger_incomplete,
        "leads_followup": _ts.tool_leads_followup,
        "order_pending": _ts.tool_order_pending,
        "invoice_pending": _ts.tool_invoice_pending,
    }
    if name in _SECOND:
        if name not in _allowed_tools(current):
            return _deny("finance", "sales")
        return await _SECOND[name](db, current)

    # 🆕 v2 阶段一：find → get。门控沿用 _allowed_tools（各自的业务域），
    #    工具内部的行级隔离继续走 sales_router._all_view，不另写谓词。
    from ..agent import tools_entity as _te
    _V2 = {
        "find_entity":  lambda: _te.find_entity(db, current, args.get("q", ""), args.get("kind")),
        "get_customer": lambda: _te.get_customer(db, current, args.get("name", "")),
        "get_project":  lambda: _te.get_project(db, current, args.get("code", "")),
        "get_supplier": lambda: _te.get_supplier(db, current, args.get("name", "")),
        "get_material": lambda: _te.get_material(db, current, args.get("q", "")),
    }
    if name in _V2:
        if name not in _allowed_tools(current):
            return _deny("list", "finance", "sales", "purchase_mgmt", "warehouse")
        return await _V2[name]()
    """执行数据工具（只读）。按调用者菜单门控数据域，无权域返回 {"error": ...} 而非数据。"""
    keys = set(menus.user_menu_keys(current))
    if name == "morning_report":
        # 不硬拒：按可用域聚合；全无任何可用域才提示
        domains: set[str] = set()
        if "purchase_mgmt" in keys:
            domains.add("po")
        if keys & {"finance", "sales"}:
            domains.add("balance")
        dept_keys = sorted(keys & set(_DEPT_MENU_KEYS))
        if dept_keys:
            domains.add("orders")
        if "hr" in keys:
            domains.add("hr")
        if not domains:
            return {"error": "你没有可查询的数据域，请联系管理员开通菜单"}
        return await tool_morning_report(db, current, domains, dept_keys or None)
    if name in ("po_arrival_overdue", "po_arriving", "po_overdue_by_supplier"):
        if "purchase_mgmt" not in keys:
            return _deny("purchase_mgmt")
        if name == "po_arrival_overdue":
            return await tool_po_arrival_overdue(db, current, int(args.get("min_overdue_days") or 0))
        if name == "po_arriving":
            return await tool_po_arriving(db, current, int(args.get("days") or 3))
        return await tool_po_overdue_by_supplier(db, current)
    if name == "balance_due":
        if not keys & {"finance", "sales"}:
            return _deny("finance", "sales")
        return await tool_balance_due(db, current)
    if name == "overdue_orders":
        dept_keys = sorted(keys & set(_DEPT_MENU_KEYS))
        if not dept_keys:
            return _deny(*_DEPT_MENU_KEYS)
        dept = args.get("dept") or None
        if dept:
            if dept not in _DEPT_MENU_KEYS:
                return {"error": f"未知部门 {dept}"}
            if dept not in dept_keys:
                return _deny(dept)
        return await tool_overdue_orders(db, current, dept,
                                         allowed_depts=None if dept else dept_keys)
    if name == "project_status":
        if "list" not in keys:
            return _deny("list")
        return await tool_project_status(db, str(args.get("code") or ""), current, keys)
    return {"error": f"未知工具 {name}"}


# ==================== 大脑：OpenAI 兼容 function calling ====================

# 用户明确要全量时的措辞。命中就放开 max_tokens ——
# 700 tokens 连 20 条中文明细都写不完，用户问「全部列举」只会得到半截清单。
_FULL_LIST_HINT = ("全部", "都列", "列全", "完整", "所有", "一个不漏", "逐条", "详细列举")
_MAX_TOKENS_DEFAULT = 700
_MAX_TOKENS_FULL = 3000


def _max_tokens_for(message: str) -> int:
    return _MAX_TOKENS_FULL if any(k in (message or "") for k in _FULL_LIST_HINT) \
        else _MAX_TOKENS_DEFAULT

_SYSTEM_PROMPT = """你是制造业 ERP 系统内置的数据分析助手（只读），当前服务对象：「{user_name}」（角色：{roles}）。今天：{today}（中国时区）。

# 铁律
1. 只用工具返回的真实数据。严禁编造任何数字、日期、金额、编号、人名。
2. 工具没返回的就说"系统里查不到"，不推测、不举例。
3. **凡是截断都必须说出来。** 工具结果里的 `count` 是总数、`shown` 是本次给了几条、
   `truncated` 是没给的条数。只要 truncated>0，结尾必须写「已列 N 条，另有 M 条未列」。
   **绝不允许**给了 5 条却让人以为那就是全部。
4. 用户说"全部/都列出来/完整清单/所有"时，**重新调用工具并传 limit=200**，然后**全部列完**，
   这种情况不受下面的条数与字数限制。
5. 只读。用户要改数据时明确拒绝，并说清该去哪个页面改。

# ⚡ 明细不要你打字（这条优先级最高）
调完工具、需要列明细时，**不要自己一行一行写数据**。改成在正文末尾附一个编排块：

```render
{{"sort":"over_days","desc":true,
 "fields":["supplier","item_name","over_days","expected_arrival","project_code"],
 "highlight":["TH20260724-006"]}}
```

- 明细行、合计、截断声明**全部由代码按这个编排渲染**，你一个数字都不用打。
- 你只负责：**结论那一句** + 上面这个编排块。
- 实测：46 条明细你自己打要 ~1700 字 / 35.9 秒；用编排块只要 ~70 字 / 3~5 秒，
  而且数字不可能出错（代码直接取工具原始值）。
- 只在「确实要列明细」时给这个块；一句话能答完的问题不要给。
- `fields` 从工具返回的字段里挑，按 `主体 · 关键量 · 时间/状态 · (补充)` 的顺序，最多 5 个。

# 输出格式（手机上看）
- **第一行永远是结论**，一句话，带上最关键的那个数，加粗。
- 明细用无序列表，**一条一行**，字段之间用「·」分隔，顺序固定：
  `主体 · 关键量 · 时间/状态 · (补充)`
  例：`- **无锡诺朋商贸** · 钢丝软管 · 超 10 天 · (预计 7/25，2026-063)`
- 金额写原始数值（¥220,000 不写 22 万）；日期原样引用；最严重那条整行加粗。
- 默认最多 5 条，按严重度降序，其余写「另有 N 条」。用户要全部时不受此限。
- 不写"建议""下一步""如需…可以…"这类收尾套话。

# 必须做的分析（这是你和一张报表的区别）
拿到数据后**先想「这堆数里最该被指出来的是什么」**，再写。至少做到其中一条：
- **排序与集中度**：谁最严重、前几名占了多少。例：「11 家里 3 家占了 30 条」。
- **异常点**：明显偏离其余的那条要单独点名。
- **口径提醒**：数据本身有坑时必须提醒。已知的几个：
  · 发货单大量停在「待发货」（shipped_at 几乎没人填），所以"发货款应收"不等于客户欠钱，
    要先确认货到底发没发，别直接说"去催款"。
  · 尾款没填到期日的，催办按到期日扫，永远扫不到。
  · 合同额为 0 的台账会让项目毛利算成假亏损。
- **算得出来的就算**：合计、占比、平均超期天数——别让用户自己拿计算器。
禁止把分析写成空话（"需要关注""建议跟进"），要落到具体是哪条、差多少。

# 数字口径
- 比例/合计一律用 `count` 这个总数算，**不要拿截断后的 shown 去算**。
- 同一事实在多个工具里出现时以更专门的那个为准，并说明来源差异。"""


async def _llm_request(messages: list[dict], model: str, cfg: dict, tools: list[dict],
                       max_tokens: int = _MAX_TOKENS_DEFAULT) -> dict:
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        # 🆕 生产实测：耗时几乎线性于**输出字数**（每字 12-15ms），答案平均 1300 字 → 18s。
        #   手机上没人读 1300 字。封顶 700 tokens（约 450 中文字），配合 system prompt
        #   里的"先给结论"要求，把 p50 从 17s 压下来。截断优于让人等。
        "max_tokens": max_tokens,
    }
    if tools:  # 🆕 只下放调用者有权的数据工具；无可用工具则纯对话（不下发 tools 字段，防空数组被拒）
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
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
                         model: str, cfg: dict, user: models.User):
    """LLM 主路径：带 tools 请求 → 执行 tool_calls 回灌 → 再请模型总结。返回 (reply, 调用过的工具名列表)。"""
    tool_names: list[str] = []
    roles = "、".join(sorted(user.role_codes)) if getattr(user, "role_codes", None) else "—"
    sys_prompt = _SYSTEM_PROMPT.format(today=_today().isoformat(),
                                       user_name=_uname(user), roles=roles)
    max_tokens = _max_tokens_for(message)
    # 🆕 只下放该用户菜单可用的数据工具（_run_tool 内仍二次门控，双保险）
    allowed = _allowed_tools(user)
    schemas = [s for s in TOOL_SCHEMAS if s["function"]["name"] in allowed]
    messages = ([{"role": "system", "content": sys_prompt}]
                + history + [{"role": "user", "content": message}])
    last_result: dict | None = None   # v2：明细交给代码渲染，需要留住原始工具结果
    for _ in range(4):  # 工具轮次上限，防死循环
        data = await _llm_request(messages, model, cfg, schemas, max_tokens)
        msg = data["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            content = (msg.get("content") or "").strip()
            if not content:
                raise RuntimeError("LLM 返回空内容")
            return apply_render(content, last_result), tool_names
        messages.append(msg)  # 含 tool_calls 的 assistant 消息原样回灌
        # 🆕 同一轮里的多个工具并发跑：它们之间没有依赖，串行等于把等待时间乘以工具数。
        #   注意共用同一个 db session —— SQLAlchemy 的 AsyncSession 不是并发安全的，
        #   所以每个工具用独立 session，跑完即关。
        calls = []
        for tc in tool_calls[:4]:
            fn = (tc.get("function") or {})
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append((tc, name, args))

        async def _one(name: str, args: dict):
            async with SessionLocal() as s2:
                return await _run_tool(name, args, s2, user)

        results = await asyncio.gather(*[_one(n, a) for _, n, a in calls],
                                       return_exceptions=True)
        for (tc, name, _), result in zip(calls, results):
            if isinstance(result, Exception):
                # 单个工具炸掉不能拖垮整轮：把错误回灌给模型，让它据此作答
                log.warning("[agent] 工具 %s 执行失败: %s", name, result)
                result = {"error": f"{TOOL_LABELS.get(name, name)}查询失败"}
            elif name in TOOL_LABELS and name not in tool_names:
                tool_names.append(name)
            # 留住带明细的那份结果，收尾时按模型给的编排块渲染（v2 阶段二）
            if isinstance(result, dict) and any(
                    isinstance(result.get(k), list) for k in ("items", "suppliers", "rows")):
                last_result = result
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
    if d["count"] == 0:
        return "**到期未到货采购：0 条** ✅\n\n目前没有到期仍未收货的采购明细。"
    head = f"**到期未到货采购共 {d['count']} 条**"
    if d["count"] > 20:
        head += "（仅列超期最严重的前 20 条，完整清单见「采购管理」）"
    lines = [head, "", "| 物料 | 供应商 | 采购单号 | 预计到货 | 超期 | 项目 | 采购员 |",
             "|---|---|---|---|---|---|---|"]
    for it in d["items"]:
        over = "今天到期" if it["over_days"] == 0 else f"**⚠ {it['over_days']} 天**"
        lines.append(f"| {it['item_name']} | {it['supplier']} | {it.get('po_no') or '—'} "
                     f"| {it['expected_arrival']} | {over} | {it.get('project_code') or '—'} | {it['buyer']} |")
    return "\n".join(lines)


def _po_arriving_text(d: dict) -> str:
    if d["count"] == 0:
        return f"**未来 {d['days']} 天预计到货：0 条** ✅"
    lines = [f"**未来 {d['days']} 天预计到货共 {d['count']} 条**"
             + ("（仅列前 20 条）" if d["count"] > 20 else ""), "",
             "| 物料 | 供应商 | 采购单号 | 预计到货 | 项目 |", "|---|---|---|---|---|"]
    for it in d["items"]:
        when = "今天" if it["in_days"] == 0 else f"{it['in_days']} 天后"
        lines.append(f"| {it['item_name']} | {it['supplier']} | {it.get('po_no') or '—'} "
                     f"| {it['expected_arrival']}（{when}） | {it.get('project_code') or '—'} |")
    return "\n".join(lines)


def _po_by_supplier_text(d: dict) -> str:
    if d["count"] == 0:
        return "**到期未到货按供应商汇总：0 家** ✅"
    lines = [f"**{d['count']} 家供应商存在到期未到货，共 {d['item_total']} 条**"
             + ("（仅列前 20 家）" if d["count"] > 20 else ""), "",
             "| 供应商 | 未到货条数 | 最大超期 | 涉及项目 |", "|---|---|---|---|"]
    for s in d["suppliers"]:
        over = "未超期（今天到期）" if s["max_over_days"] == 0 else f"**⚠ {s['max_over_days']} 天**"
        lines.append(f"| {s['supplier']} | {s['count']} | {over} | {'、'.join(s['projects']) or '—'} |")
    return "\n".join(lines)


def _balance_text(d: dict) -> str:
    if d["count"] == 0:
        return "**尾款到期/逾期：0 条** ✅\n\n未来 14 天内没有到期尾款，也没有已逾期未收的。"
    lines = [f"**尾款到期（14 天内）或已逾期共 {d['count']} 条**"
             + ("（仅列前 20 条）" if d["count"] > 20 else ""), "",
             "| 项目 | 客户 | 尾款 | 约定日期 | 状态 | 销售 |", "|---|---|---|---|---|---|"]
    for it in d["items"]:
        status = _fmt_days(it["days"])
        if it["days"] < 0:
            status = f"**⚠ {status}**"
        lines.append(f"| {it['project_code']} {it['project_name']} | {it.get('customer') or '—'} "
                     f"| {_fmt_money(it['balance'])} | {it['balance_date']} | {status} | {it['sales']} |")
    return "\n".join(lines)


def _overdue_orders_text(d: dict) -> str:
    if d["count"] == 0:
        return "**部门逾期任务：0 条** ✅"
    lines = [f"**部门逾期任务共 {d['count']} 条**"
             + ("（仅列逾期最严重的前 20 条）" if d["count"] > 20 else ""), "",
             "| 部门 | 项目 | 预计完成 | 已逾期 | 负责人 |", "|---|---|---|---|---|"]
    for it in d["items"]:
        lines.append(f"| {it['dept_name']} | {it['project_code']} | {it['due_date']} "
                     f"| **⚠ {it['over_days']} 天** | {it['worker']} |")
    return "\n".join(lines)


def _morning_text(d: dict) -> str:
    def _sec(title: str, count: int) -> str:
        return f"**{title}：{count} 条**" + (" ✅" if count == 0 else "")

    lines = [f"## 📋 今日晨报（{d['today']}）", ""]
    s = d.get("po_arrival_overdue")
    if s is not None:
        lines.append(_sec("一、采购到期未到货", s["count"]))
        for it in s["top"]:
            over = "今天到期" if it["over_days"] == 0 else f"**⚠ 已超期 {it['over_days']} 天**"
            lines.append(f"- {it['item_name']}（{it['supplier']}）预计 {it['expected_arrival']}，{over}")
    s = d.get("overdue_orders")
    if s is not None:
        lines += ["", _sec("二、部门逾期任务", s["count"])]
        for it in s["top"]:
            lines.append(f"- {it['dept_name']} {it['project_code']}，**⚠ 逾期 {it['over_days']} 天**（{it['worker']}）")
    s = d.get("balance_due")
    if s is not None:
        lines += ["", _sec("三、尾款到期/逾期", s["count"])]
        for it in s["top"]:
            lines.append(f"- {it['project_code']} 尾款 {_fmt_money(it['balance'])}，{_fmt_days(it['days'])}")
    s = d.get("hr_due")
    if s is not None:
        lines += ["", _sec("四、人事到期", s["count"])]
        for it in s["top"]:
            lines.append(f"- {it['name']}（{it['dept']}）{it['kind']} {it['date']}（{_fmt_days(it['days'])}）")
    return "\n".join(lines)


def _project_text(d: dict) -> str:
    if not d.get("found"):
        return f"系统里查不到项目编号「{d['code']}」，请核对编号后重试。"
    lines = [f"### 项目 {d['code']} {d['name']}" + ("（已删除）" if d.get("is_deleted") else ""), "",
             f"- 状态：**{d['status']}**；负责人：{d['manager']}"]
    lines.append(f"\n**各部门任务（{len(d['dept_orders'])} 条）**")
    if d["dept_orders"]:
        lines += ["", "| 部门 | 状态 | 预计完成 | 实际完成 | 负责人 |", "|---|---|---|---|---|"]
        for o in d["dept_orders"]:
            lines.append(f"| {o['dept_name']} | {o['status']} | {o.get('due_date') or '—'} "
                         f"| {o.get('done_date') or '—'} | {o['worker']} |")
    else:
        lines.append("\n- 暂无部门任务单")
    lines.append(f"\n**未到货采购项：{d['po_pending_count']} 项**")
    for it in d["po_pending"]:
        over = ""
        if it.get("over_days") is not None and it["over_days"] > 0:
            over = f"，**⚠ 已超期 {it['over_days']} 天**"
        exp = f"预计 {it['expected_arrival']}" if it.get("expected_arrival") else "未填预计到货"
        lines.append(f"- {it['item_name']}（{it['supplier']}，{exp}{over}）")
    # 🆕 ledger 字段仅 finance/sales 菜单用户才下发；无该字段时整个小节不渲染
    if "ledger" in d:
        led = d.get("ledger")
        lines.append("\n**回款/尾款**")
        if led:
            bal = led["balance"] or 0
            if bal > 0:
                when = _fmt_days(led["balance_days"]) if led.get("balance_days") is not None else "未约定日期"
                lines.append(f"- 尾款 **{_fmt_money(bal)}** 未收（约定 {led.get('balance_date') or '—'}，{when}）；"
                             f"合同额 {_fmt_money(led['amount'])}，客户：{led.get('customer') or '—'}")
            else:
                lines.append(f"- 尾款已结清；合同额 {_fmt_money(led['amount'])}，客户：{led.get('customer') or '—'}")
        else:
            lines.append("- 无销售台账记录")
    return "\n".join(lines)


_CAPABILITY_TEXT = """我是 ERP 数据助手（只读），所有数字都来自系统实时查询。目前可以回答：
- **「今日晨报」**：采购未到货 / 逾期任务 / 尾款 / 人事到期一览（也可以说「今天要盯什么」）
- **「采购未到货」**：到期仍未收货的采购明细；「哪个供应商拖期」→ 按供应商汇总
- **「未来一周到货」**：即将到货的采购明细
- **「尾款到期」**：14 天内到期或已逾期的尾款（也可以说「回款」「欠款」）
- **「逾期任务」**：各部门逾期未完成任务
- **单项目进度**：消息里带上项目编号，如「TH-2501 进度」"""


async def _rule_chat(message: str, db: AsyncSession, current: models.User):
    """规则降级：关键词意图匹配 → 调数据工具（经 _run_tool，与 LLM 共用菜单门控）→ Markdown 模板格式化。
    返回 (reply, 工具名列表)（工具名供 endpoints 映射 sources 标签 + 追问建议）。"""
    m = message.strip()

    async def _call(name: str, args: dict | None = None):
        return await _run_tool(name, args or {}, db, current)

    if any(k in m for k in ("晨报", "早报", "早会", "要盯", "风险", "汇报")):
        d = await _call("morning_report")
        return (d["error"], []) if "error" in d else (_morning_text(d), ["morning_report"])
    if "供应商" in m:
        d = await _call("po_overdue_by_supplier")
        return (d["error"], []) if "error" in d else (_po_by_supplier_text(d), ["po_overdue_by_supplier"])
    if any(k in m for k in ("未到货", "采购", "到货")):
        if any(k in m for k in ("未来", "预计", "即将", "将要", "下周", "一周")) \
                and "未到货" not in m and "超期" not in m:
            days = 7 if any(k in m for k in ("下周", "一周", "7 天", "7天")) else 3
            d = await _call("po_arriving", {"days": days})
            return (d["error"], []) if "error" in d else (_po_arriving_text(d), ["po_arriving"])
        d = await _call("po_arrival_overdue")
        return (d["error"], []) if "error" in d else (_po_overdue_text(d), ["po_arrival_overdue"])
    if any(k in m for k in ("尾款", "回款", "欠款")):
        d = await _call("balance_due")
        return (d["error"], []) if "error" in d else (_balance_text(d), ["balance_due"])
    if "逾期" in m:
        d = await _call("overdue_orders")
        return (d["error"], []) if "error" in d else (_overdue_orders_text(d), ["overdue_orders"])
    hit = _PROJECT_CODE_RE.search(m)
    if hit:
        d = await _call("project_status", {"code": hit.group(0)})
        return (d["error"], []) if "error" in d else (_project_text(d), ["project_status"])
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


# ==================== 🆕 审计日志（问答全量落 agent_chat_logs，失败不影响聊天） ====================

_LOG_TEXT_MAX = 5000   # question/answer 入库前截断上限，防超大文本撑爆行


_RENDER_RE = re.compile(r"```render\s*(\{.*?\})\s*```", re.S)


def apply_render(reply: str, last_result: dict | None) -> str:
    """把模型给的 ```render 编排块换成代码渲染的明细。

    ⚠️ 模型给不出合法 JSON、或者没有可渲染的工具结果时，**原样去掉这个块**，
       绝不把 JSON 漏给用户看。宁可少一段明细，也不能露出内部结构。
    """
    m = _RENDER_RE.search(reply or "")
    if not m:
        return reply
    body = reply[:m.start()].rstrip() + reply[m.end():].rstrip()
    if not isinstance(last_result, dict):
        return body
    try:
        plan = json.loads(m.group(1))
    except (ValueError, TypeError):
        return body
    from ..agent import render as _rd
    detail = _rd.table(last_result, plan=plan)
    return (body + "\n\n" + detail).strip() if detail else body


def _fallback_reason(e: Exception) -> str:
    """LLM 失败原因简述（记进 model 字段，如 "rule-fallback:timeout"），不含敏感信息。"""
    msg = str(e)
    if "timeout" in msg.lower() or "超时" in msg:
        return "timeout"
    m = re.search(r"HTTP (\d+)", msg)          # LLM 接口返回 HTTP 500
    if m:
        return f"http-{m.group(1)}"
    m = re.search(r"（(.+?)）", msg)            # LLM 调用失败（ConnectError）
    if m:
        return m.group(1)
    return type(e).__name__


async def _log_chat(db: AsyncSession, user: models.User, question: str, answer: str,
                    tool_names: list[str], via: str, model: str, duration_ms: int | None):
    """写一条对话审计日志。独立小函数 + try/except 全包裹：日志失败只记 log，绝不影响聊天主流程。"""
    try:
        db.add(models.AgentChatLog(
            user_id=user.id, username=user.username,
            question=(question or "")[:_LOG_TEXT_MAX],
            answer=(answer or "")[:_LOG_TEXT_MAX],
            tools_used=list(tool_names or []),
            # ⚠️ via 列是 String(8)。传 "rule-stream-fallback"（19 字符）会
            #    StringDataRightTruncationError → 日志写不进去。真正的降级原因
            #    本来就记在 model 字段（rule-fallback:<原因>），via 只需要粗分类。
            via=(via or "")[:8], model=(model or "")[:64], duration_ms=duration_ms,
        ))
        await db.commit()
    except Exception as e:  # noqa: BLE001 —— 审计是旁路，任何失败都不能炸掉聊天
        log.warning("[agent] 审计日志写入失败: %s", e)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass


@router.post("/chat")
async def chat(
    body: ChatIn,
    current: models.User = Depends(get_current_user),   # 🆕 全员可聊；数据域在 _run_tool 按菜单门控
    db: AsyncSession = Depends(get_db),
):
    t0 = time.perf_counter()
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
    allowed = _allowed_tools(current)
    if cfg["api_key"]:
        llm_model = model or cfg["model"]
        try:
            reply, tool_names = await _chat_with_llm(text, history, db, llm_model, cfg, current)
            await _log_chat(db, current, text, reply, tool_names, via="llm",
                            model=llm_model, duration_ms=int((time.perf_counter() - t0) * 1000))
            return {"reply": reply, "fallback": False,
                    "sources": [TOOL_LABELS[n] for n in tool_names],
                    "suggestions": _suggestions_for(tool_names, allowed)}
        except Exception as e:  # noqa: BLE001 —— LLM 任何异常都降级，保证可用
            log.warning("[agent] LLM 调用失败，转规则降级: %s", e)
            fb_model = f"rule-fallback:{_fallback_reason(e)}"
    else:
        fb_model = "rule-fallback"
    reply, tool_names = await _rule_chat(text, db, current)
    await _log_chat(db, current, text, reply, tool_names, via="rule",
                    model=fb_model, duration_ms=int((time.perf_counter() - t0) * 1000))
    return {"reply": reply, "fallback": True,
            "sources": [TOOL_LABELS[n] for n in tool_names],
            "suggestions": _suggestions_for(tool_names, allowed)}


@router.get("/chat-logs")
async def list_chat_logs(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    username: str | None = Query(None, description="按用户名模糊过滤"),
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """🆕 对话审计日志分页查询（admin/manager）：按时间倒序，可按 username 模糊过滤。"""
    L = models.AgentChatLog
    conds = []
    if username and username.strip():
        conds.append(L.username.like(f"%{username.strip()}%"))
    total = (await db.execute(select(func.count(L.id)).where(*conds))).scalar() or 0
    rows = list((await db.execute(
        select(L).where(*conds)
        .order_by(L.created_at.desc(), L.id.desc())
        .limit(size).offset((page - 1) * size))).scalars().all())
    return {
        "total": total,
        "items": [{
            "id": r.id, "username": r.username, "question": r.question, "answer": r.answer,
            "tools_used": r.tools_used or [], "via": r.via, "model": r.model,
            "duration_ms": r.duration_ms,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
    }


# ==================== 🆕 L3 卡片式人审门 ====================
# 设计依据：docs/ai-agent-erp-handbook 第 3.4 / 3.5 节。
# 本层只「提案 + 呈现」，一行写操作没有：用户点按钮后，前端拿他自己的 token
# 打现有业务端点（URL 只存在于前端 cardRegistry.ts），后端区分不出也不需要区分。

@router.get("/cards/pending")
async def list_pending_cards(
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """当前用户的待办审批卡。第一期白名单只有请款一类（registry.CARD_TYPES）。

    行级隔离交给装配层复用业务侧谓词；这里不写任何 where。
    """
    from ..agent import cards as _cards
    items = await _cards.assemble_pay_req_cards(db, current)
    total = sum(float(str(f["v"]).replace("¥", "").replace(",", ""))
                for c in items for f in c["facts"] if f["k"] == "请款金额")
    return {
        "cards": items,
        "count": len(items),
        "amount_total": round(total, 2),
        # 有几张卡因职责分离等原因批不了，前端可用来做「其中 N 件需他人处理」的提示
        "blocked": sum(1 for c in items
                       if any(f["level"] == "block" for f in c["flags"])),
    }


@router.get("/cards/{card_type}")
async def list_cards_by_type(
    card_type: str,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """按类型取卡。类型必须在白名单里（原则三），否则 400 并留痕。

    ledger_settle 额外给 amount_total —— 门户上「盯不住的应收 ¥253万」那个数
    要与卡片列表同源，两处各算一遍必然对不上。
    """
    from ..agent import cards as _cards
    if card_type not in _cards.ASSEMBLERS:
        await write_audit(db, user=current, action="card_action_denied",
                          target_type=card_type, detail="未知卡片类型")
        raise HTTPException(400, "未知的卡片类型")
    items = await _cards.ASSEMBLERS[card_type](db, current)
    total = 0.0
    for c in items:
        for f in c["facts"]:
            if f.get("emphasis") and str(f["v"]).startswith("¥"):
                total += float(str(f["v"])[1:].replace(",", ""))
    out = {
        "cards": items,
        "count": len(items),
        "amount_total": round(total, 2),
        "blocked": sum(1 for c in items
                       if any(f["level"] == "block" for f in c["flags"])),
    }
    # 🆕 汇总：一次弹 20 张卡没法看，先给一张总账再逐条展开。
    #   合计基于全量而非截断后的前 20 条，否则总数与列表对不上。
    if card_type == "ledger_settle":
        out["summary"] = await _cards.summarize_settle(db, current)
    return out


class CardActionIn(BaseModel):
    type: str
    ref: int
    token: str
    action: str


@router.post("/cards/verify-action")
async def verify_card_action(
    body: CardActionIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """点按钮前的一道校验：确认这张卡确实是发给这个人、这条记录、这个动作的。

    它 **不** 替代业务端点的鉴权——真正的权限判断仍在业务端点里（用户自己的 token）。
    它挡的是另一类问题：卡片被串改、令牌过期后复用、把 A 卡按钮接到 B 条记录上。
    校验通过后前端才发起真正的审批请求。
    """
    from ..agent.cards import registry as _reg, token as _tok
    if not _reg.is_known(body.type):
        await write_audit(db, user=current, action="card_action_denied",
                          target_type=body.type, target_id=body.ref,
                          detail="卡片类型不在白名单内")
        raise HTTPException(400, "卡片类型不在白名单内")
    if not _reg.allows(body.type, body.action):
        await write_audit(db, user=current, action="card_action_denied",
                          target_type=body.type, target_id=body.ref,
                          detail=f"动作 {body.action} 不属于该卡")
        raise HTTPException(400, "该卡片不支持这个动作")
    ok, why = _tok.verify(body.token, current.id, body.type, body.ref)
    if not ok:
        await write_audit(db, user=current, action="card_action_denied",
                          target_type=body.type, target_id=body.ref, detail=why)
        raise HTTPException(400, why)
    # 重新装配一次，拿最新的 flags：卡是 15 分钟前发的，这期间单子可能已被别人处理。
    # 按 type 找装配器——写死某一类的话，新加的卡类型永远校验不过。
    from ..agent import cards as _cards
    assembler = _cards.ASSEMBLERS.get(body.type)
    if assembler is None:
        raise HTTPException(400, "该卡片类型没有装配器")
    fresh = await assembler(db, current, refs=[body.ref])
    if not fresh:
        raise HTTPException(400, "该单已不在你的待办里，可能已被处理")
    blocking = [f for f in fresh[0]["flags"] if f["level"] == "block"]
    if blocking:
        raise HTTPException(400, blocking[0]["msg"])
    await write_audit(db, user=current, action="card_action_ok",
                      target_type=body.type, target_id=body.ref, detail=body.action)
    return {"ok": True, "card": fresh[0]}


# ==================== 🆕 H5 门户配置（按用户） ====================

@router.get("/portal")
async def get_portal(
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """当前用户的门户配置 + 他能摆的卡片目录。

    没配置过返回角色默认值（不是空门户）；目录按 _allowed_tools 过滤——
    没权限用某个工具的人，压根看不到那张卡，也就摆不上去（原则三）。
    """
    from ..agent import portal
    allowed = _allowed_tools(current)
    tiles = await portal.get_tiles(db, current, allowed)
    return {
        "tiles": portal.expand(tiles),
        "catalog": portal.visible_catalog(allowed),
        "limits": {"max_tiles": portal.MAX_TILES,
                   "max_label": portal.MAX_LABEL,
                   "max_question": portal.MAX_QUESTION},
    }


class PortalIn(BaseModel):
    tiles: list[dict] = []


@router.put("/portal")
async def save_portal(
    body: PortalIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """保存门户配置。提交内容一律先 sanitize：内置卡只认目录里的 key 且要有权限，
    自定义卡只留 label/q 两个字段并限长，其余字段全丢。"""
    from ..agent import portal
    allowed = _allowed_tools(current)
    clean = portal.sanitize(body.tiles, allowed)
    await portal.set_tiles(db, current, clean)
    return {"tiles": portal.expand(clean)}


@router.delete("/portal")
async def reset_portal(
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """恢复角色默认门户。"""
    from ..agent import portal
    allowed = _allowed_tools(current)
    await portal.set_tiles(db, current, [])
    return {"tiles": portal.expand(portal.default_tiles(current, allowed))}


# ==================== 🆕 直答：门户卡片不经 LLM ====================
# 生产实测：一次 LLM 往返 ~8-10s，答案每字 12-15ms，p50 17s / p95 34s。
# 而门户卡片是**确定性**的——「今日晨报」就是调 morning_report，没有歧义，
# 让模型再想一遍纯属浪费。直接调工具 + 复用规则降级的格式化模板，几十毫秒出结果。
#
# 复用 _run_tool（菜单门控）与 _xxx_text（Markdown 模板），不新增任何数据访问路径。

_DIRECT_FORMATTERS = {
    "morning_report": _morning_text,
    "po_arrival_overdue": _po_overdue_text,
    "po_arriving": _po_arriving_text,
    "po_overdue_by_supplier": _po_by_supplier_text,
    "balance_due": _balance_text,
    "overdue_orders": _overdue_orders_text,
}


class DirectToolIn(BaseModel):
    tool: str
    args: dict = {}


@router.post("/tool")
async def run_tool_direct(
    body: DirectToolIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """直接执行一个数据工具并返回排好版的答案。不经 LLM。

    工具白名单 = _DIRECT_FORMATTERS ∩ 该用户有权的工具；越界 400。
    权限仍由 _run_tool 内部按菜单门控（与 LLM 路径同一套，不另开口子）。
    """
    t0 = time.perf_counter()
    name = body.tool
    if name not in _DIRECT_FORMATTERS:
        raise HTTPException(400, "不支持直接执行该工具")
    if name not in _allowed_tools(current):
        raise HTTPException(403, "无权使用该工具")

    d = await _run_tool(name, body.args or {}, db, current)
    if isinstance(d, dict) and "error" in d:
        reply, tools = d["error"], []
    else:
        reply, tools = _DIRECT_FORMATTERS[name](d), [name]

    ms = int((time.perf_counter() - t0) * 1000)
    await _log_chat(db, current, f"[直答]{TOOL_LABELS.get(name, name)}", reply, tools,
                    via="direct", model="direct", duration_ms=ms)
    return {"reply": reply, "fallback": False, "direct": True, "duration_ms": ms,
            "sources": [TOOL_LABELS[n] for n in tools],
            "suggestions": _suggestions_for(tools, _allowed_tools(current))}


# ==================== 🆕 流式输出（SSE） ====================
# 为什么要流式：生产实测 p50 17s。总时长压不到 1s，但**感知延迟**可以——
# 模型出第一个字通常在 1-2s 内，之后逐字推给前端，人不再对着白屏干等。
#
# 工具轮次不流给用户（那是内部过程），只推一条「正在查 xxx」的状态；
# 最后一轮的正文才逐块推。

async def _llm_stream(messages: list[dict], model: str, cfg: dict, tools: list[dict],
                      max_tokens: int = _MAX_TOKENS_DEFAULT):
    """向 LLM 发流式请求，逐块 yield 原始 delta。

    ⚠️ max_tokens 必须**当参数传进来**。我一度直接引用调用方的同名局部变量，
       运行时 NameError → 被外层 except 吞掉 → 整条流式请求掉进规则降级，
       返回那段「我是 ERP 数据助手」的功能菜单。现象是「秒回但答非所问」，
       很容易被误读成「变快了」。
    """
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload: dict = {"model": model, "messages": messages,
                     "temperature": 0.2, "max_tokens": max_tokens, "stream": True}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    async with httpx.AsyncClient(timeout=60.0) as cli:
        async with cli.stream("POST", url, json=payload, headers=headers) as r:
            if r.status_code != 200:
                await r.aread()
                # 安全红线：只透状态码，绝不把可能含 key 的异常链带出去
                raise RuntimeError(f"LLM 接口返回 HTTP {r.status_code}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    yield json.loads(chunk)
                except json.JSONDecodeError:
                    continue


def _merge_tool_call_deltas(acc: dict, deltas: list) -> None:
    """流式下 tool_calls 是按 index 分片来的，要按索引拼回完整调用。"""
    for d in deltas or []:
        i = d.get("index", 0)
        cur = acc.setdefault(i, {"id": "", "function": {"name": "", "arguments": ""}})
        if d.get("id"):
            cur["id"] = d["id"]
        fn = d.get("function") or {}
        if fn.get("name"):
            cur["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            cur["function"]["arguments"] += fn["arguments"]


async def _chat_stream(message: str, history: list[dict], model: str,
                       cfg: dict, user: models.User):
    """流式主循环。yield (事件类型, 数据)。DB session 自己开——
    StreamingResponse 的生成器在请求处理函数返回之后才跑，Depends 给的 session 那时已关。"""
    tool_names: list[str] = []
    roles = "、".join(sorted(user.role_codes)) if getattr(user, "role_codes", None) else "—"
    sys_prompt = _SYSTEM_PROMPT.format(today=_today().isoformat(),
                                       user_name=_uname(user), roles=roles)
    max_tokens = _max_tokens_for(message)
    allowed = _allowed_tools(user)
    schemas = [s for s in TOOL_SCHEMAS if s["function"]["name"] in allowed]
    messages = ([{"role": "system", "content": sys_prompt}]
                + history + [{"role": "user", "content": message}])

    last_result: dict | None = None   # v2 阶段二：留住工具原始结果，收尾时代码渲染明细
    for _ in range(4):
        content_parts: list[str] = []
        tc_acc: dict = {}
        streamed = 0          # 已推给前端的字符数
        suppress = False      # 进入 ``` 之后不再推——render 块的裸 JSON 不能让用户看见
        async for data in _llm_stream(messages, model, cfg, schemas, max_tokens):
            choices = data.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            if delta.get("tool_calls"):
                _merge_tool_call_deltas(tc_acc, delta["tool_calls"])
            piece = delta.get("content")
            if not piece:
                continue
            content_parts.append(piece)
            if suppress:
                continue
            acc = "".join(content_parts)
            # ``` 可能被切在两个 chunk 之间，所以按累计文本判断，不看单块
            fence = acc.find("```")
            if fence >= 0:
                suppress = True
                if fence > streamed:
                    yield "delta", acc[streamed:fence]
                    streamed = fence
            else:
                yield "delta", piece
                streamed = len(acc)

        if not tc_acc:
            text = "".join(content_parts).strip()
            # ⚠️ 判空必须在**渲染之后**。模型完全可以只回一句结论 + 一个编排块，
            #    而编排块整段被 suppress 吞掉了 —— 拿吞之前的文本判空会误判成
            #    「LLM 返回空内容」→ 抛异常 → 整条请求降级成功能菜单。踩过。
            final = apply_render(text, last_result)
            if not final.strip():
                raise RuntimeError("LLM 返回空内容")
            if len(final) > streamed:
                yield "delta", final[streamed:]
            yield "done", {"text": final, "tools": tool_names}
            return

        # 这一轮是工具调用：不推正文，只报状态，然后并发执行
        calls = []
        for _, tc in sorted(tc_acc.items()):
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append((tc, name, args))
            if name in TOOL_LABELS:
                yield "tool", TOOL_LABELS[name]

        messages.append({"role": "assistant", "content": None,
                         "tool_calls": [{"id": tc["id"], "type": "function",
                                         "function": tc["function"]} for tc, _, _ in calls]})

        async def _one(name: str, args: dict):
            async with SessionLocal() as s2:
                return await _run_tool(name, args, s2, user)

        results = await asyncio.gather(*[_one(n, a) for _, n, a in calls],
                                       return_exceptions=True)
        for (tc, name, _), result in zip(calls, results):
            if isinstance(result, Exception):
                log.warning("[agent] 工具 %s 失败: %s", name, result)
                result = {"error": f"{TOOL_LABELS.get(name, name)}查询失败"}
            elif name in TOOL_LABELS and name not in tool_names:
                tool_names.append(name)
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": json.dumps(result, ensure_ascii=False, default=str)})

    raise RuntimeError("LLM 工具调用轮次超限")


@router.post("/chat/stream")
async def chat_stream(
    body: ChatIn,
    current: models.User = Depends(get_current_user),
):
    """SSE 流式问答。事件：tool（正在查什么）/ delta（正文片段）/ done / error。

    降级路径不流式——规则模板是一次成型的文本，没有逐字生成的过程，
    直接当作一个 delta 推出去即可。
    """
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(400, "请输入问题")
    history = [{"role": h.role, "content": h.content[:2000]}
               for h in body.history[-20:] if h.role in ("user", "assistant")]

    async def gen():
        t0 = time.perf_counter()
        def sse(ev: str, data) -> str:
            return f"event: {ev}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        async with SessionLocal() as db:
            cfg = await _effective_llm_config(db)
            model = (body.model or "").strip() or cfg["model"]
            if model not in _model_whitelist(cfg):
                yield sse("error", {"message": f"无效模型「{model}」"})
                return
            allowed = _allowed_tools(current)
            reply, tools, via = "", [], "llm"
            try:
                if not cfg["api_key"]:
                    raise RuntimeError("未配置 api_key")
                async for kind, payload in _chat_stream(text, history, model, cfg, current):
                    if kind == "delta":
                        yield sse("delta", {"text": payload})
                    elif kind == "tool":
                        yield sse("tool", {"label": payload})
                    else:
                        reply, tools = payload["text"], payload["tools"]
            except Exception as e:  # noqa: BLE001 —— 任何异常都降级，保证可用
                log.warning("[agent] 流式失败，转规则降级: %s", e)
                via = "rule-stream-fallback"
                reply, tools = await _rule_chat(text, db, current)
                yield sse("delta", {"text": reply})

            ms = int((time.perf_counter() - t0) * 1000)
            await _log_chat(db, current, text, reply, tools, via=via,
                            model=model if via == "llm" else via, duration_ms=ms)
            yield sse("done", {
                "duration_ms": ms, "fallback": via != "llm",
                "sources": [TOOL_LABELS[n] for n in tools],
                "suggestions": _suggestions_for(tools, allowed),
            })

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",   # ← 关键：不加这行 nginx 会缓冲整个响应，流式失效
    })


# ==================== 🆕 第二批工具的文本模板 ====================

def _money0(x) -> str:
    return f"¥{float(x or 0):,.0f}"


def _receivable_blind_text(d: dict) -> str:
    if not d.get("count"):
        return "**没有盯不住的应收** ✅ 所有未收款都填了到期日，催办能覆盖到。"
    lines = [f"**盯不住的应收：{d['count']} 笔，合计 {_money0(d['total'])}**",
             "现有催办与「尾款到期」都查不到这些——没填到期日的尾款、以及发货款应收。", ""]
    for r in d["items"][:5]:
        age = f" · 挂了 {r['age_days']} 天" if r.get("age_days") else ""
        lines.append(f"- {r['customer']} · {r['kind_cn']} **{_money0(r['amount'])}**"
                     f" · {r['project_code']}{age}")
    if d["count"] > 5:
        lines.append(f"- 另有 {d['count'] - 5} 笔")
    return "\n".join(lines)


def _shipment_receiver_text(d: dict) -> str:
    if not d.get("count"):
        return "**发货单收货人都填齐了** ✅"
    lines = [f"**{d['count']} 张发货单还没填收货人**，填了才能安排送货签收。", ""]
    shown = d.get("items", [])
    for r in shown[:5]:
        sug = r.get("suggest")
        tip = f" · 上次收货人 {sug['name']}" if sug and sug.get("name") else ""
        # 客户名摆最前——「#95」这种单据号对人没有任何意义，认不出是哪一单。
        # 客户名一直都在（沿 project_id 取得到），以前没取才显得「系统里查不到」。
        cust = r.get("customer") or "（台账没填客户）"
        lines.append(f"- **{cust}** · {r.get('project_code') or f'#{r[chr(105)+chr(100)]}'}"
                     f" · 单号 #{r['id']}{tip}")
    rest = d["count"] - len(shown[:5])
    if rest > 0:
        lines.append(f"- 另有 {rest} 张（共 {d['count']} 张）")
    return "\n".join(lines)


def _ledger_incomplete_text(d: dict) -> str:
    if not d.get("count"):
        return "**台账关键字段都填齐了** ✅"
    lines = [f"**{d['count']} 行台账缺关键字段**。缺合同额会让项目毛利算成假亏损。", ""]
    for r in d["items"][:5]:
        lines.append(f"- {r['project_code']} {r['customer']} · 缺 {'、'.join(r['missing'])}")
    if d["count"] > 5:
        lines.append(f"- 另有 {d['count'] - 5} 行")
    return "\n".join(lines)


def _leads_followup_text(d: dict) -> str:
    if not d.get("count"):
        return "**没有挂着的线索** ✅"
    lines = [f"**{d['count']} 条线索还没闭环**（既没成交也没丢单）。", ""]
    for r in d["items"][:5]:
        age = f" · {r['age_days']} 天前" if r.get("age_days") else ""
        lines.append(f"- {r['customer']} · {r['status']}{age}")
    if d["count"] > 5:
        lines.append(f"- 另有 {d['count'] - 5} 条")
    return "\n".join(lines)


def _order_pending_text(d: dict) -> str:
    if not d.get("count"):
        return "**没有待审批的销售订单** ✅"
    lines = [f"**{d['count']} 笔销售订单等你审批**。", ""]
    for r in d["items"][:5]:
        lines.append(f"- {r['project_code']} {r['customer']} · {_money0(r['amount'])}")
    return "\n".join(lines)


def _invoice_pending_text(d: dict) -> str:
    if not d.get("count"):
        return "**没有待开票的台账行** ✅"
    lines = [f"**{d['count']} 行已申请开票，等财务出票**。", ""]
    for r in d["items"][:5]:
        lines.append(f"- {r['project_code']} {r['customer']} · {_money0(r['amount'])}")
    return "\n".join(lines)


_DIRECT_FORMATTERS.update({
    "receivable_blind": _receivable_blind_text,
    "shipment_receiver": _shipment_receiver_text,
    "ledger_incomplete": _ledger_incomplete_text,
    "leads_followup": _leads_followup_text,
    "order_pending": _order_pending_text,
    "invoice_pending": _invoice_pending_text,
})


@router.get("/capabilities")
async def list_capabilities(
    current: models.User = Depends(get_current_user),
):
    """当前用户能用的工具清单 + 每个是干什么的。

    做这个接口的理由：用户问「你会什么」时，模型只能靠 system prompt 里那几句
    自己描述，容易吹得比实际大。这里给的是**代码里真实注册过的**那一份，
    而且按 _allowed_tools 过滤——他看到的每一条都是他真能用的。
    """
    allowed = _allowed_tools(current)
    return {
        "count": len(allowed),
        "items": [{"key": k, "label": TOOL_LABELS[k], "desc": TOOL_DESC[k]}
                  for k in TOOL_LABELS if k in allowed],
        # 明确说明不做什么，比含糊其辞强（手册 11.7 优雅拒答）
        "not_supported": [
            "改数据——助手只读，任何修改都要回到业务页面或卡片按钮上做",
            "销售额/毛利统计——成本归集率不足，给数会误导",
            "人事花名册详情、库存单价等未开放域",
        ],
    }

# ==================== 每日简报（主动推）====================
# 智能体原来只在被问时答。推送通道（站内 + 企微）早就有，助手一次没调过。
# 这里把两头接上，并把「推给谁」做成可配，避免上线即全员轰炸。


class BriefingUsersIn(BaseModel):
    usernames: list[str]


@router.get("/briefing/config")
async def briefing_config(
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """当前收件人 + 下次推送时间。"""
    from ..agent import daily
    users = await daily._get_setting(db, daily._SETTING_USERS, [])
    return {
        "usernames": users,
        "push_hour_cn": daily._PUSH_HOUR_CN,
        "next_run_in_seconds": int(daily._seconds_to_next_run()),
        "cooldown_days": _briefing_cooldown(),
    }


def _briefing_cooldown() -> int:
    from ..agent import briefing as _b
    return _b._REPEAT_COOLDOWN_DAYS


@router.put("/briefing/config")
async def set_briefing_users(
    body: BriefingUsersIn,
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """设置收件人。空数组 = 关掉推送。"""
    from ..agent import daily
    names = [u.strip() for u in body.usernames if u.strip()]
    if names:
        found = {u.username for u in (await db.execute(select(models.User).where(
            models.User.username.in_(names)))).scalars().all()}
        missing = [n for n in names if n not in found]
        if missing:
            raise HTTPException(400, f"这些用户不存在：{'、'.join(missing)}")
    await daily._set_setting(db, daily._SETTING_USERS, names)
    await write_audit(db, user=current, action="agent_briefing_config",
                      target_type="app_setting", target_id=0,
                      detail=",".join(names) or "(已关闭)")
    return {"message": f"已设置 {len(names)} 位收件人" if names else "已关闭每日简报"}


@router.post("/briefing/preview")
async def briefing_preview(
    dry: bool = Query(True, description="true=只看不发（默认）；false=真推出去"),
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """手动跑一轮。默认 dry=true 只返回将要发的内容，方便调完排序马上看效果，
    不用等到第二天早上 8 点。"""
    from ..agent import daily
    return await daily.run_once(db, dry=dry)


@router.get("/briefing/me")
async def my_briefing(
    top: int = Query(3, ge=1, le=10),
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """当前用户自己的简报（H5 首页顶部用：进来就看见今天该管什么，不用先打字）。"""
    from ..agent import briefing
    brief = await briefing.build(current, top=top)
    return {**brief, "text": briefing.render(brief, current.full_name or current.username)}
