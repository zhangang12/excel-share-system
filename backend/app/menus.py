"""🆕 v3 菜单可见性矩阵 —— 全系统唯一权威（前端经 GET /api/auth/menus 渲染）。

口径来源：《权限矩阵.md》§二 + 2026-06-12 收紧口径：
- 销售员/销售主管、电工/电工部负责人、装配组：只保留本部门菜单 + 项目目录，
  且项目目录中编号不可点进项目详单（can_view_detail=False）
- 售后部员工/主管：仅售后部菜单（无项目目录）
- 月度报表 / 管理组菜单：仅 admin + manager（部门主管也无权）
- 老 8 角色（designer/production_clerk/warehouse/buyer_*/hr）保持既有可见性不回收（红线）

新增模块菜单 key 与原型一致：sales/design/electric/produce/sheet/purchase/
warehouse/logistics/finance/aftersales/report/messages + 管理组 approve/wxbind。
"""
from . import models

# 全部业务菜单 key（顺序即侧边栏顺序；label 供前端展示）
MENU_DEFS: list[dict] = [
    {"key": "catalog",    "label": "项目目录"},
    {"key": "list",       "label": "项目详单"},
    {"key": "sales",      "label": "销售部"},
    {"key": "design",     "label": "设计部"},
    {"key": "electric",   "label": "电工部"},
    {"key": "produce",    "label": "生产部"},
    # 🆕 2026-06-19 「钣金组」菜单并入「生产部」(以 tab 呈现)，不再单列菜单 key=sheet
    {"key": "purchase",   "label": "采购部"},
    {"key": "warehouse",  "label": "仓库"},
    {"key": "logistics",  "label": "物流发货部"},
    {"key": "finance",    "label": "财务部"},
    {"key": "aftersales", "label": "售后部"},
    {"key": "report",     "label": "月度工作报表"},
    {"key": "messages",   "label": "消息中心"},
]

# 管理组菜单（admin + manager 专属）
ADMIN_MENU_DEFS: list[dict] = [
    {"key": "admin-users",    "label": "用户"},
    {"key": "admin-perms",    "label": "权限管理"},
    {"key": "admin-audit",    "label": "操作审计"},
    {"key": "approve",        "label": "导出审批"},
    {"key": "wxbind",         "label": "企微绑定"},
    {"key": "user-feedback",  "label": "用户反馈"},  # 🆕 收集所有用户提交的问题/建议
]

_ALL_KEYS = [m["key"] for m in MENU_DEFS]
_ADMIN_KEYS = [m["key"] for m in ADMIN_MENU_DEFS]

# role_code -> 可见菜单 key 集合（admin/manager 不在表内=全可见）
# 注意：messages 全员可见，统一在 user_menu_keys 里追加，不在矩阵重复
ROLE_MENUS: dict[str, list[str]] = {
    # ---- 老角色：保持既有可见性（catalog+list），叠加对应部门工作台 ----
    "designer":         ["catalog", "list", "design"],
    "production_clerk": ["catalog", "list", "produce"],
    "warehouse":        ["catalog", "list", "warehouse"],
    "buyer_standard":   ["catalog", "list", "purchase"],
    "buyer_outsource":  ["catalog", "list", "purchase"],
    "hr":               ["catalog", "list"],
    # ---- 🆕 v3 角色 ----
    "sales":            ["catalog", "sales"],                 # 无详单，编号不可点
    "sales_lead":       ["catalog", "sales"],
    "design_lead":      ["catalog", "list", "design"],
    "electrician":      ["catalog", "electric"],              # 无详单
    "electric_lead":    ["catalog", "electric"],              # 无详单（与 design_lead 不对称是有意口径）
    "assembler":        ["catalog", "produce"],               # 无详单；装配组 tab 在生产部菜单内
    "pm_lead":          ["catalog", "list", "produce"],
    "sheetmetal":       ["catalog", "list", "produce"],        # 🆕 钣金组并入生产部菜单(tab)
    "buyer":            ["catalog", "list", "purchase"],
    "warehouse_lead":   ["catalog", "list", "warehouse"],
    "logistics":        ["catalog", "list", "logistics"],
    "finance":          ["catalog", "list", "finance"],
    "as_worker":        ["aftersales"],                       # 仅售后部（无项目目录）
    "as_lead":          ["aftersales"],
}


def user_menu_keys(user: models.User) -> list[str]:
    """当前用户可见的业务菜单 key（按 MENU_DEFS 顺序）+ 管理组 key。

    多角色取并集：可见菜单 = 各角色可见菜单的合集。
    """
    codes = user.role_codes
    if codes & {"admin", "manager"}:
        return _ALL_KEYS + _ADMIN_KEYS
    allowed: set[str] = set()
    for code in (codes or {""}):
        allowed |= set(ROLE_MENUS.get(code, ["catalog", "list"]))  # 未知角色按老默认（目录+详单）
    allowed.add("messages")
    return [k for k in _ALL_KEYS if k in allowed]


def user_can_view_detail(user: models.User) -> bool:
    """是否可进入项目详单/项目详情（2026-06-12 收紧口径：销售/电工/装配/售后不可）。
    与菜单矩阵同源：有 'list' 菜单即可进详单。"""
    return "list" in user_menu_keys(user)
