"""🆕 H5 智能体门户：可枚举的卡片目录 + 按用户的门户配置。

沿用原则三「能力可枚举」：门户上能摆什么，由服务端这张目录说了算。
用户能做的是「挑哪几张、什么顺序」，以及沉淀自己的提问，
**不能**凭配置造出一个新能力——自定义卡片的本质只是一句预置提问，
点下去仍然走 /agent/chat，不会多出任何数据访问路径。

每张内置卡都绑一个真实工具（tool），装配时按 `_allowed_tools` 过滤：
没权限用那个工具的人，目录里根本看不到这张卡，也就摆不上去。
"""
import json
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models

PORTAL_KEY = "portal_tiles"

MAX_TILES = 12          # 门户最多摆这么多张；再多手机上一屏翻不完，失去"点一下就看"的意义
MAX_LABEL = 10          # 卡片标题字数
MAX_QUESTION = 120      # 自定义提问长度

# ── 卡片目录：每一张都对应 agent_router 里真实存在的工具 ──
# 没有工具支撑的条目一律不要加：摆上去点进去只会得到「查不到」，比不摆更伤信任。
CATALOG: list[dict] = [
    {"key": "approvals", "label": "等你签字", "desc": "待你审批的请款单",
     "glyph": "￥", "tone": "blue", "q": "待我审批的请款单", "tool": None, "kind": "approve"},
    {"key": "morning_report", "label": "今日晨报", "desc": "一条消息看完全部要紧事",
     "glyph": "报", "tone": "blue", "q": "今日晨报", "tool": "morning_report"},
    {"key": "balance_due", "label": "尾款到期", "desc": "14 天内到期与已逾期的应收",
     "glyph": "收", "tone": "warn", "q": "尾款到期", "tool": "balance_due"},
    {"key": "overdue_orders", "label": "部门逾期", "desc": "各部门超期未完成的任务",
     "glyph": "逾", "tone": "danger", "q": "部门逾期任务", "tool": "overdue_orders"},
    {"key": "po_overdue_by_supplier", "label": "按供应商汇总", "desc": "哪家供应商拖得最狠",
     "glyph": "供", "tone": "blue", "q": "按供应商汇总未到货", "tool": "po_overdue_by_supplier"},
    {"key": "po_arrival_overdue", "label": "采购超期", "desc": "到期未到货的料和供应商",
     "glyph": "箱", "tone": "danger", "q": "采购未到货", "tool": "po_arrival_overdue"},
    {"key": "po_arriving", "label": "近期到货", "desc": "接下来一周能到的料",
     "glyph": "期", "tone": "good", "q": "未来 7 天到货", "tool": "po_arriving"},

    # ── 🆕 第二批：销售/台账域 ──
    # 加这批的依据是杨坛的真实操作轨迹（台账 243 次 / 请款 40 / 收货人 34 / 订单 29，
    # 采购 0 次）。desc 与 agent_router.TOOL_DESC 保持同一句话，别写两份说法。
    {"key": "receivable_blind", "label": "盯不住的应收",
     "desc": "催办查不到的钱：没填到期日的尾款 + 发货款应收",
     "glyph": "盯", "tone": "danger", "q": "盯不住的应收", "tool": "receivable_blind"},
    {"key": "shipment_receiver", "label": "待填收货人",
     "desc": "发货单收货人还空着，填了才能送货签收",
     "glyph": "收", "tone": "warn", "q": "待填收货人", "tool": "shipment_receiver"},
    {"key": "ledger_incomplete", "label": "台账缺件",
     "desc": "缺合同额或客户；合同额为 0 会让毛利算成假亏损",
     "glyph": "缺", "tone": "warn", "q": "台账缺件", "tool": "ledger_incomplete"},
    {"key": "order_pending", "label": "待审销售单",
     "desc": "销售下了单、等主管审批的订单",
     "glyph": "单", "tone": "blue", "q": "待审批销售订单", "tool": "order_pending"},
    {"key": "invoice_pending", "label": "待开票",
     "desc": "已申请开票、等财务出票的台账行",
     "glyph": "票", "tone": "blue", "q": "待开票", "tool": "invoice_pending"},
    {"key": "leads_followup", "label": "线索待跟进",
     "desc": "既没成交也没放弃、还挂着的销售线索",
     "glyph": "索", "tone": "good", "q": "线索待跟进", "tool": "leads_followup"},
]
_BY_KEY = {c["key"]: c for c in CATALOG}

# ── 角色默认门户 ──
# 依据是**真实操作轨迹**，不是助手调用日志（那只有 3 次会话，样本太薄）。
# 杨坛 2026-06-08 ~ 08-02 的 audit_logs + payment_requests：
#   销售台账 sales_ledger  243 次（改台账 164 / 标记已开票 31 / 传附件 18 /
#                                 换技术文件 12 / 申请开票 10 / 作废 4 / 审批驳回 3）
#   请款审批                40 笔
#   物流收货人              34 次
#   销售订单                29 次（建 19 / 审批 9 / 驳回 1）
#   项目头信息 12 · 派单改派 12 · 销售线索 13
#   采购相关                 0 次 ← 两个月一次没碰过
# 所以 po_* 三张采购卡**不进**他的默认门户；balance_due 查的正是他天天在改的
# sales_ledger，反而最贴。采购三件套留给 purchase 角色。
# ⚠️ key 必须是 roles 表里的真实 code。系统里采购角色叫 buyer（还有 buyer_lead /
#    buyer_standard / buyer_outsource），不叫 purchase——写错了这组默认永远不会命中。
_DEFAULTS: dict[str, list[str]] = {
    # 杨坛(manager)：按「他做过多少 × 此刻还有多少在等」排——
    #   请款审批 40 次/2 笔在等、盯不住的应收 36 次/63 笔在等、收货人 34 次/49 单在等、
    #   晨报他 3 次会话每次都调。采购三件套一张不进（两个月 0 次操作）。
    "manager": ["approvals", "receivable_blind", "shipment_receiver",
                "morning_report", "ledger_incomplete", "overdue_orders"],
    "finance_lead": ["approvals", "receivable_blind", "balance_due",
                     "invoice_pending", "morning_report"],
    "finance": ["approvals", "balance_due", "invoice_pending", "morning_report"],
    "buyer_lead": ["po_arrival_overdue", "po_overdue_by_supplier",
                   "po_arriving", "morning_report"],
    "buyer": ["po_arrival_overdue", "po_overdue_by_supplier",
              "po_arriving", "morning_report"],
    "sales_lead": ["order_pending", "receivable_blind", "shipment_receiver",
                   "leads_followup", "morning_report"],
    "sales": ["receivable_blind", "balance_due", "leads_followup", "morning_report"],
}
# 查找顺序：先管理层、再主管、再普通岗。杨坛同时是 manager/sales_lead/finance_lead，
# 命中第一个 manager，拿到含请款审批的那组。
_ROLE_ORDER = ("manager", "finance_lead", "buyer_lead", "sales_lead",
               "finance", "buyer", "sales")
_FALLBACK = ["morning_report", "overdue_orders", "po_arrival_overdue"]


def visible_catalog(allowed_tools: set[str]) -> list[dict]:
    """当前用户能摆上门户的卡。tool=None 的（审批）不受工具门控，由卡片层自己判权限。"""
    return [c for c in CATALOG if c["tool"] is None or c["tool"] in allowed_tools]


def default_tiles(user: models.User, allowed_tools: set[str]) -> list[dict]:
    """没配置过时给什么。按角色取一组，再按可见目录过滤。"""
    keys: list[str] = []
    for role in _ROLE_ORDER:
        if user.has_role(role):
            keys = _DEFAULTS[role]
            break
    if not keys:
        keys = _FALLBACK
    ok = {c["key"] for c in visible_catalog(allowed_tools)}
    return [{"key": k} for k in keys if k in ok]


def _clean_custom(t: dict) -> dict | None:
    """自定义卡：只留 label 与 q 两个字段，其余一概丢弃。

    它不是新能力——点下去等价于用户手打这句话发给 /agent/chat。
    但仍要限长并去掉控制字符：这段文本会进 LLM 上下文，也会显示在门户上。
    """
    label = re.sub(r"[\x00-\x1f\x7f]", "", str(t.get("label") or "")).strip()[:MAX_LABEL]
    q = re.sub(r"[\x00-\x1f\x7f]", "", str(t.get("q") or "")).strip()[:MAX_QUESTION]
    if not label or not q:
        return None
    key = str(t.get("key") or "")
    if not key.startswith("custom:"):
        key = f"custom:{uuid.uuid4().hex[:12]}"
    return {"key": key, "label": label, "q": q, "custom": True,
            "glyph": "问", "tone": "blue", "desc": q[:24]}


def sanitize(tiles: list, allowed_tools: set[str]) -> list[dict]:
    """把前端传来的配置洗成可信形状。

    内置卡只认目录里的 key（且用户有权限）；自定义卡只留 label/q。
    这一步是必须的：配置由用户提交，不能直接回喂给渲染层。
    """
    ok = {c["key"] for c in visible_catalog(allowed_tools)}
    out: list[dict] = []
    seen: set[str] = set()
    for t in (tiles or [])[: MAX_TILES * 2]:
        if not isinstance(t, dict):
            continue
        key = str(t.get("key") or "")
        if key.startswith("custom:") or t.get("custom"):
            c = _clean_custom(t)
            if c and c["key"] not in seen:
                seen.add(c["key"]); out.append(c)
        elif key in ok and key not in seen:
            seen.add(key); out.append({"key": key})
        if len(out) >= MAX_TILES:
            break
    return out


def expand(tiles: list[dict]) -> list[dict]:
    """把存的精简形状（内置卡只有 key）补成前端要渲染的完整卡。
    目录改了文案/图标，所有人的门户跟着变——所以存的时候只存 key，不存副本。"""
    out = []
    for t in tiles:
        if t.get("custom"):
            out.append(t)
        elif t["key"] in _BY_KEY:
            c = _BY_KEY[t["key"]]
            # 带上 tool：前端据此判断能不能走直答通道（不经 LLM，快两个数量级）
            out.append({k: c[k] for k in ("key", "label", "desc", "glyph", "tone", "q")}
                       | {"kind": c.get("kind"), "tool": c.get("tool")})
    return out


async def get_tiles(db: AsyncSession, user: models.User, allowed_tools: set[str]) -> list[dict]:
    row = (await db.execute(select(models.UserSetting).where(
        models.UserSetting.user_id == user.id,
        models.UserSetting.key == PORTAL_KEY))).scalar_one_or_none()
    if row and row.value:
        try:
            stored = json.loads(row.value)
        except (ValueError, TypeError):
            stored = []          # 脏数据当没配过，回落默认，不让门户变空白
        if stored:
            return sanitize(stored, allowed_tools)
    return default_tiles(user, allowed_tools)


async def set_tiles(db: AsyncSession, user: models.User, tiles: list[dict]) -> None:
    row = (await db.execute(select(models.UserSetting).where(
        models.UserSetting.user_id == user.id,
        models.UserSetting.key == PORTAL_KEY))).scalar_one_or_none()
    payload = json.dumps(tiles, ensure_ascii=False)
    if row:
        row.value = payload
    else:
        db.add(models.UserSetting(user_id=user.id, key=PORTAL_KEY, value=payload))
    await db.commit()
