"""🆕 2026-09-23 智能体的权限口径（推广到全公司之前补的这一层）。

为什么单独一个文件：智能体的取数散在 agent_router / tools_entity / tools_sales / briefing / cards 里，
每处各写各的判断，结果就是**智能体比网页版看得多**。推广前审出两处真泄露：
  · 待审请款卡对所有登录用户可见（金额、供应商、账号后 4 位、采购内容）
  · 项目类工具把合同额、回款、客户给了所有有「项目详单」菜单的人（仓库、设计、电工都能问出来）
这里把口径收到一处，以后加工具**先来这里拿判据，别再在各自文件里重写**。

口径对齐网页版：
  · 钱（合同额/四段款/请款金额）—— 管理层、销售主管、财务看全部；销售只看自己负责的台账；其余不给。
  · 请款审批 —— 只有财务和管理层（网页 approve 端点就是 require_roles("finance")）。
  · 销售订单审批 —— 只有销售主管和管理层（网页 order-approve 是 require_roles("sales_lead")）。
"""
from __future__ import annotations

from .. import models

_FIN = ("finance", "finance_lead")
_MGMT = ("admin", "manager")


def is_mgmt(u: models.User | None) -> bool:
    return bool(u) and u.has_role(*_MGMT)


def is_finance(u: models.User | None) -> bool:
    """财务口径的人（含管理层）。网页上请款审批、付款、开票都是这批人。"""
    return bool(u) and u.has_role(*_FIN, *_MGMT)


def can_approve_pay_req(u: models.User | None) -> bool:
    """能不能审批请款。与 purchase_mgmt_router.approve_payment_request 的 require_roles("finance") 同口径
    （require_roles 自带 admin/manager 兜底）。"""
    return is_finance(u)


def can_approve_sales_order(u: models.User | None) -> bool:
    """能不能审批销售下单。与 sales_router.order_approve 的 require_roles("sales_lead") 同口径。"""
    return bool(u) and u.has_role("sales_lead", *_MGMT)


def money_scope(u: models.User | None) -> str:
    """能看到多少「钱」：all=全部 / own=只有自己负责的台账 / none=不给。

    ⚠️ 财务算 all：网页上财务的待开票、应收、支出总览本来就是全量，
       而智能体这边一直套用销售的 _all_view（只认管理层+销售主管），
       导致财务问「待开票」回「没有 ✅」——数据是有的，只是被过滤掉了。
    """
    if not u:
        return "none"
    if is_mgmt(u) or u.has_role("sales_lead", *_FIN):
        return "all"
    if u.has_role("sales"):
        return "own"
    return "none"


def can_see_ledger(u: models.User | None, sales_uid: int | None) -> bool:
    """这个人能不能看到某张销售台账上的钱和客户。"""
    scope = money_scope(u)
    if scope == "all":
        return True
    if scope == "own":
        return bool(u and sales_uid and sales_uid == u.id)
    return False


def redact_ledger(led: dict | None, u: models.User | None) -> dict | None:
    """项目快照里的台账段：没权限就整段去掉（连客户一起），别只去金额——
    客户名加上「这个项目归谁」本身也是销售信息。
    入参 led 里必须带 sales_uid（内部字段），返回时一律剔除。"""
    if not led:
        return None
    if not can_see_ledger(u, led.get("sales_uid")):
        return None
    return {k: v for k, v in led.items() if k != "sales_uid"}


def sales_read_all(u: models.User | None) -> bool:
    """只读的销售/台账类查询能不能看全部。= 管理层 / 销售主管 / 财务。
    ⚠️ 只给**读**用；销账、审批这些写动作仍走各自端点的角色判断。"""
    return money_scope(u) == "all"


# ── 项目可见性 ──────────────────────────────────────────────
async def visible_pids(db, u: models.User | None) -> set[int] | None:
    """这个人在项目目录里能看到哪些项目。返回 None 表示「全部可见」。

    直接复用 deps.restricted_dir_pids（网页项目目录列表用的就是它），
    不在这里重写判据。受限岗位 = 设计/电工/装配/钣金/封板/销售，
    只看被派单(worker_id) / 自己下单(sales_uid) 的项目。

    ⚠️ 智能体这边原来完全没有这层：一个装配工在网页项目目录里只看得到自己那几台，
       却能问智能体「在建项目都有哪些」拿到全部 46 个。2026-09-23 推广前补上。
    """
    if not u:
        return set()
    from ..deps import restricted_dir_pids
    restricted, pids = await restricted_dir_pids(db, u)
    return None if not restricted else pids


def project_visible(p, allowed: set[int] | None, u: models.User | None) -> bool:
    """allowed=None 一律可见；否则「被派单/自己下单」或在项目的补授名单里。
    补授名单 __viz_uids__ 是存量数据按姓名匹配补的，网页列表也认它，这里必须一起认，
    否则同一个人网页看得到、手机上问不出来。"""
    if allowed is None:
        return True
    return p.id in allowed or bool(u and u.id in ((p.extra or {}).get("__viz_uids__") or []))
