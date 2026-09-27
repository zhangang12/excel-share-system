"""🆕 2026-09-27 常用提示词 —— 网页版智能体「提示词」面板的数据源。

为什么要有这一层：网页版原来写死 5 个快捷问题（待我审批的 / 今日晨报 / 采购未到货 /
尾款到期 / 逾期任务），所有人看到的一模一样。装配工点「尾款到期」得到的是
「你无权查询该域数据」，财务点「采购未到货」也一样 —— 5 个里有 3 个对他是死按钮。
而真正能问的东西（项目体检、回款画像、给谁派待办、某物料还剩多少）一个都没摆出来，
用户只能靠猜。

条目怎么定的：看了生产上近 30 天真实的提问（agent_chat_logs）。排前面的是
「项目进度跟进」14 次、「今日晨报」10 次、「我发的待办怎么样了」9 次、「台账缺件」6 次；
还有大量**带参数**的问法 ——「068发货款」「056的尾款」「江苏翎戴智能装备的订单」
「安排一个代办给夏坤，让他明天把文件上传了」。后一类做成**填空模板**（kind=fill）：
点了不直接发，而是填进输入框、把 {占位} 选中，人只要打那一段。

口径纪律（和 portal.py 同一条）：
  · 每一条都挂在一个**真实存在的工具**上，按 `_allowed_tools(user)` 过滤。
    没权限的条目压根不下发 —— 摆一个点了只会得到「无权查询」的按钮，比不摆更伤信任。
  · 技能（skills.py）的门控是菜单，这里挂到同菜单门控的工具上，效果等价。
  · tool=None 的条目人人可见（「待我审批的」—— OA 审批链里任何人都可能是审批人）。
"""
from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models

SAVED_KEY = "agent_saved_prompts"     # UserSetting 里的 key（门户磁贴用的是 portal_tiles）
MAX_SAVED = 20                        # 收藏上限；再多就不是「常用」了
MAX_LEN = 120                         # 单条长度，与门户自定义卡同口径
RECENT_N = 8                          # 「最近问过」给几条

# kind: send = 点了直接发；fill = 填进输入框，{…} 是要人补的那一段
# ⚠️ q 里的 {占位} 必须用全角说明文字（{项目编号}），前端靠「{」「}」识别并选中；
#    发送前还会检查有没有漏填的占位，漏了就拦下来提示，不会把「{项目编号}」原样发出去。
GROUPS: list[dict] = [
    {"name": "今天要盯的", "items": [
        {"label": "今日晨报", "q": "今日晨报", "tool": "morning_report",
         "desc": "采购超期、尾款到期、部门逾期、人事到期，一条看完"},
        {"label": "待我审批的", "q": "待我审批的", "tool": None,
         "desc": "请款、OA、销售下单，能直接点通过/驳回"},
        {"label": "我手上的活", "q": "我手上的活", "tool": "my_tasks",
         "desc": "派给我、还没做完的任务，含装配/钣金/封板"},
        {"label": "我发的待办怎么样了", "q": "我发的待办怎么样了", "tool": "mgmt_todo_watch",
         "desc": "谁没回、谁超期、谁申请顺延"},
    ]},
    {"name": "项目交期", "items": [
        {"label": "项目进度跟进", "q": "项目进度跟进", "tool": "project_progress",
         "desc": "在建项目还剩几天、卡在哪一环"},
        {"label": "7 天内要交的", "q": "7 天内要交货的项目有哪些，各卡在哪", "tool": "project_progress"},
        {"label": "已经过期的", "q": "已经过了交货日期还没发的项目，各卡在哪一环", "tool": "project_progress"},
        {"label": "部门逾期任务", "q": "部门逾期任务", "tool": "overdue_orders"},
        {"label": "项目体检", "q": "{项目编号} 项目体检", "tool": "get_project", "kind": "fill",
         "desc": "一个项目从台账到发货全看一遍"},
        {"label": "某项目卡在哪", "q": "{项目编号} 卡在哪", "tool": "get_project", "kind": "fill"},
        {"label": "某项目还缺哪些料", "q": "{项目编号} 还有哪些采购没到货", "tool": "get_project",
         "kind": "fill"},
    ]},
    {"name": "采购到货", "items": [
        {"label": "采购未到货", "q": "采购未到货", "tool": "po_arrival_overdue"},
        {"label": "按供应商汇总", "q": "按供应商汇总未到货", "tool": "po_overdue_by_supplier",
         "desc": "哪家拖得最狠"},
        {"label": "未来 7 天到货", "q": "未来 7 天到货", "tool": "po_arriving"},
        {"label": "供应商拖期复盘", "q": "供应商拖期复盘", "tool": "po_overdue_by_supplier"},
        {"label": "某供应商靠不靠谱", "q": "{供应商名} 准时率怎么样", "tool": "get_supplier",
         "kind": "fill"},
    ]},
    {"name": "销售回款", "items": [
        {"label": "这月销售额多少", "q": "这月销售额多少", "tool": "sales_summary"},
        {"label": "台账缺件", "q": "台账缺件", "tool": "ledger_incomplete",
         "desc": "缺合同额/客户的台账行"},
        {"label": "尾款到期", "q": "尾款到期", "tool": "balance_due"},
        {"label": "盯不住的应收", "q": "盯不住的应收", "tool": "receivable_blind"},
        {"label": "待开票", "q": "待开票", "tool": "invoice_pending"},
        {"label": "待填收货人", "q": "待填收货人", "tool": "shipment_receiver"},
        {"label": "待审销售单", "q": "待审批销售订单", "tool": "order_pending"},
        {"label": "线索待跟进", "q": "线索待跟进", "tool": "leads_followup"},
        {"label": "客户回款画像", "q": "{客户名} 回款画像", "tool": "get_customer", "kind": "fill",
         "desc": "这家客户欠多少、该不该催"},
        {"label": "某项目的款", "q": "{项目编号} 的发货款和尾款收了没有", "tool": "get_customer",
         "kind": "fill"},
    ]},
    {"name": "仓库物料", "items": [
        {"label": "库存预警", "q": "库存预警", "tool": "get_material",
         "desc": "低于安全库存的物料"},
        {"label": "某物料还有多少", "q": "{物料名称或编码} 还有多少库存", "tool": "get_material",
         "kind": "fill"},
    ]},
    {"name": "派待办", "items": [
        {"label": "给人派个待办", "q": "给{某人}派个待办：{做什么}，{几号}前完成",
         "tool": "mgmt_todo_send", "kind": "fill",
         "desc": "先出草稿，你点「确认发出」才真发"},
        {"label": "我常派给谁", "q": "我最近常派待办给谁", "tool": "mgmt_todo_peers"},
    ]},
]


def visible_groups(allowed_tools: set[str]) -> list[dict]:
    """按权限过滤后的分组。空组整组不下发。返回给前端的条目里不带 tool 字段 ——
    前端用不着，也不该让它知道「背后是哪个工具」。"""
    out = []
    for g in GROUPS:
        items = [
            {"label": it["label"], "q": it["q"], "kind": it.get("kind", "send"),
             "desc": it.get("desc", "")}
            for it in g["items"]
            if it["tool"] is None or it["tool"] in allowed_tools
        ]
        if items:
            out.append({"name": g["name"], "items": items})
    return out


async def recent_questions(db: AsyncSession, user: models.User, n: int = RECENT_N) -> list[str]:
    """这个人最近问过的问题（去重，最近的在前）。

    数据源就是审计日志 —— 不另记一份，理由同 quota.py：两份数据早晚对不上。
    ⚠️ 排除「[直答]…」：那是门户卡片走直答时日志里记的标签，不是人打的话，
       放进「最近问过」点了会把「[直答]今日晨报」原样发出去。
    """
    L = models.AgentChatLog
    rows = (await db.execute(
        select(L.question, func.max(L.id).label("last"))
        .where(L.user_id == user.id,
               ~L.question.like("[直答]%"),
               func.length(L.question) >= 2)
        .group_by(L.question)
        .order_by(func.max(L.id).desc())
        .limit(n))).all()
    return [r[0][:MAX_LEN] for r in rows]


def _clean(items: list) -> list[str]:
    """收藏的净化：去空、限长、去重（保序）、封顶。"""
    out: list[str] = []
    for x in items or []:
        s = str(x or "").strip()[:MAX_LEN]
        if s and s not in out:
            out.append(s)
        if len(out) >= MAX_SAVED:
            break
    return out


async def get_saved(db: AsyncSession, user: models.User) -> list[str]:
    row = (await db.execute(select(models.UserSetting).where(
        models.UserSetting.user_id == user.id,
        models.UserSetting.key == SAVED_KEY))).scalar_one_or_none()
    if not row or not row.value:
        return []
    try:
        return _clean(json.loads(row.value))
    except (ValueError, TypeError):
        return []           # 脏数据当没存过，不让面板报错


async def set_saved(db: AsyncSession, user: models.User, items: list) -> list[str]:
    clean = _clean(items)
    row = (await db.execute(select(models.UserSetting).where(
        models.UserSetting.user_id == user.id,
        models.UserSetting.key == SAVED_KEY))).scalar_one_or_none()
    payload = json.dumps(clean, ensure_ascii=False)
    if row:
        row.value = payload
    else:
        db.add(models.UserSetting(user_id=user.id, key=SAVED_KEY, value=payload))
    await db.commit()
    return clean
