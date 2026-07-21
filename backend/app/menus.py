"""🆕 v3 菜单可见性 —— 全系统唯一权威（前端经 GET /api/auth/menus 渲染）。

运行时口径（2026-07-21 起）：一级菜单按账号配置（User.menus），管理层 admin/manager
全量可见；角色菜单矩阵（原 ROLE_MENUS）已废除，仅存 ROLE_DEFAULT_MENUS 作建号/迁移默认值。

历史口径来源：《权限矩阵.md》§二 + 2026-06-12 收紧口径：
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
    {"key": "leads",      "label": "销售线索"},   # 🆕 线索池/分配/跟进/成交率报表
    {"key": "design",     "label": "设计部"},
    {"key": "electric",   "label": "电工部"},
    {"key": "produce",    "label": "生产部"},
    # 🆕 2026-06-19 「钣金组」菜单并入「生产部」(以 tab 呈现)，不再单列菜单 key=sheet
    # 🆕 采购部 tab 已合并入采购管理视图，不再单列 key=purchase
    {"key": "purchase_mgmt", "label": "采购管理"},   # 含采购部/明细/账目/请款/报表
    {"key": "warehouse",  "label": "仓库"},
    {"key": "logistics",  "label": "物流发货部"},
    {"key": "finance",    "label": "财务部"},
    {"key": "aftersales", "label": "售后部"},
    {"key": "hr",         "label": "人事部"},   # 🆕 一期:员工花名册+部门月度工资总额(hr+管理层)
    {"key": "report",     "label": "月度工作报表"},
    {"key": "oa",         "label": "OA审批"},     # 🆕 全员可见（业务/报销/采购申请+审批）
    {"key": "messages",   "label": "消息中心"},
]

# 管理组菜单（admin + manager 专属）
ADMIN_MENU_DEFS: list[dict] = [
    {"key": "admin-users",    "label": "用户"},
    {"key": "admin-perms",    "label": "权限管理"},
    {"key": "admin-audit",    "label": "操作审计"},
    {"key": "dict-admin",     "label": "字典设置"},   # 🆕 物料类别/单位/材质/供应商分类/订单编号 字典（admin+manager）
    {"key": "approve",        "label": "导出审批"},
    {"key": "wxbind",         "label": "企微绑定"},
    {"key": "user-feedback",  "label": "用户反馈"},  # 🆕 收集所有用户提交的问题/建议
    {"key": "agent",          "label": "Agent 助手"},  # 🆕 只读问数 POC（admin/manager 专属，归入「管理」分组）
    {"key": "desktop",        "label": "桌面端"},      # 🆕 桌面客户端在线版本分布（admin/manager 专属，只读统计）
]

_ALL_KEYS = [m["key"] for m in MENU_DEFS]
_ADMIN_KEYS = [m["key"] for m in ADMIN_MENU_DEFS]

# 未配置 User.menus（NULL）时的兜底默认（正常不会命中：建号预填 + 存量迁移都会落地）
DEFAULT_ACCOUNT_MENUS: list[str] = ["catalog", "list", "messages", "oa"]

# role_code -> 默认一级菜单 key 集合（admin/manager 不在表内=全可见）
# ⚠️ 仅作「建号预填 / 存量迁移 backfill」的默认值模板：运行时菜单可见性一律读
#    User.menus（见 user_menu_keys），不读本表。本表内容 = 已废除的原 ROLE_MENUS
#    角色菜单矩阵，原样保留仅供上述两处取默认值。
ROLE_DEFAULT_MENUS: dict[str, list[str]] = {
    # ---- 老角色：保持既有可见性（catalog+list），叠加对应部门工作台 ----
    "designer":         ["catalog", "list", "design"],
    "production_clerk": ["catalog", "list", "produce"],
    "warehouse":        ["catalog", "list", "warehouse"],
    "buyer_standard":   ["catalog", "list", "purchase_mgmt"],
    "buyer_outsource":  ["catalog", "list", "purchase_mgmt"],
    # 🆕 反馈#208：人事只需人事部,不看项目目录/项目详单(纯 hr 用户收窄;多角色仍取并集)
    "hr":               ["hr"],
    # ---- 🆕 v3 角色 ----
    "sales":            ["catalog", "sales", "leads"],        # 无详单，编号不可点；🆕 销售线索
    "sales_lead":       ["catalog", "sales", "leads"],
    "design_lead":      ["catalog", "list", "design"],
    "electrician":      ["catalog", "electric"],              # 无详单
    "electric_lead":    ["catalog", "electric"],              # 无详单（与 design_lead 不对称是有意口径）
    "assembler":        ["catalog", "produce"],               # 无详单；装配组 tab 在生产部菜单内
    "pm_lead":          ["catalog", "list", "produce"],
    "sheetmetal":       ["catalog", "produce"],                 # 🆕 钣金组并入生产部菜单(tab)；无详单（同 assembler 口径）
    "sealing":          ["catalog", "produce"],                 # 🆕 反馈#209 封板组并入生产部菜单(tab)；无详单（同装配/钣金口径）
    "buyer":            ["catalog", "list", "purchase_mgmt"],
    "buyer_lead":       ["catalog", "list", "purchase_mgmt"],  # 🆕 采购主管
    "warehouse_lead":   ["catalog", "list", "warehouse"],
    "logistics":        ["catalog", "list", "logistics"],
    "finance":          ["catalog", "list", "finance", "purchase_mgmt"],
    "finance_lead":     ["catalog", "list", "finance", "purchase_mgmt"],  # 🆕 财务主管(⊇财务)
    "as_worker":        ["aftersales"],                       # 仅售后部（无项目目录）
    "as_lead":          ["aftersales"],
}


# 🆕 #7 可按账号授权的「二级菜单(tab)」注册表：menu_key -> [(tab_name, 中文名)]。
#   全局唯一 key = f"{menu_key}:{tab_name}"，存进 User.hidden_tabs 表示对该账号隐藏。
#   tab_name 必须与前端 <el-tab-pane name="..."> 一致。
TAB_REGISTRY: list[dict] = [
    {"menu_key": "purchase_mgmt", "menu_label": "采购管理", "tabs": [
        ("purchase", "采购部"), ("items", "采购明细"), ("statements", "供应商账目"),
        ("payreq", "请款记录"), ("preq", "采购申请"), ("reports", "汇总报表")]},
    {"menu_key": "finance", "menu_label": "财务部", "tabs": [
        ("pending", "待开票"), ("invoiced", "已开票"), ("aftersales", "安装/售后费用"),
        ("pay_requests", "请款审批"), ("pay_payment", "付款"), ("expense", "支出总览"),
        ("pnl", "项目毛利"), ("audit", "成本审计"), ("fund", "资金面板"),
        ("payables", "采购应付"), ("inventory", "库存/成本")]},
    {"menu_key": "hr", "menu_label": "人事部", "tabs": [
        ("roster", "员工花名册"), ("payroll", "工资总额")]},
    {"menu_key": "warehouse", "menu_label": "仓库", "tabs": [
        # 🆕 「出入库登记」(io) 已并入「出入库/物料需求」(demand)；旧 io 的 hidden_tabs 记录失效无害
        ("ov", "库存总览"), ("sum", "收发存汇总"), ("txn", "出入库流水"),
        ("mat", "物料主数据"), ("loc", "库位管理"), ("demand", "出入库/物料需求"), ("recv", "采购收货"),
        ("ship", "发货清单"), ("preq", "采购申请")]},
]


def tab_registry() -> list[dict]:
    """给管理端权限页用：可授权的二级菜单清单(带全局唯一 key)。"""
    return [{"menu_key": g["menu_key"], "menu_label": g["menu_label"],
             "tabs": [{"key": f'{g["menu_key"]}:{name}', "label": label} for name, label in g["tabs"]]}
            for g in TAB_REGISTRY]


def canonical_menu_order(keys) -> list[str]:
    """把一批菜单 key 按规范顺序排列：业务 key 按 MENU_DEFS 顺序，管理组 key 按
    ADMIN_MENU_DEFS 顺序排尾；无效 key 丢弃。"""
    s = set(keys or [])
    return [k for k in _ALL_KEYS if k in s] + [k for k in _ADMIN_KEYS if k in s]


def default_menus_for_roles(codes) -> list[str]:
    """建号/存量迁移的一次性默认值：角色集 → ROLE_DEFAULT_MENUS 并集 ∪ {messages, oa}
    （按规范顺序）。仅建号/迁移用，运行时不调。"""
    allowed: set[str] = set()
    for code in (codes or {""}):
        allowed |= set(ROLE_DEFAULT_MENUS.get(code, ["catalog", "list"]))  # 未知角色按老默认（目录+详单）
    allowed |= {"messages", "oa"}  # messages/oa 全员可见，统一在此并入，不在模板重复
    return canonical_menu_order(allowed)


def user_menu_keys(user: models.User) -> list[str]:
    """当前用户可见的业务菜单 key（按 MENU_DEFS 顺序）+ 管理组 key（按 ADMIN_MENU_DEFS 顺序排尾）。

    口径（2026-07-21 起）：一级菜单按账号配置（User.menus），角色菜单矩阵已废除：
    - admin/manager：全量可见（不读 User.menus）
    - 其余账号：User.menus 即完整清单；NULL=未配置 → DEFAULT_ACCOUNT_MENUS 兜底
    """
    codes = user.role_codes
    if codes & {"admin", "manager"}:
        return _ALL_KEYS + _ADMIN_KEYS
    configured = user.menus if user.menus is not None else DEFAULT_ACCOUNT_MENUS
    return canonical_menu_order(configured)


def user_can_view_detail(user: models.User) -> bool:
    """是否可进入项目详单/项目详情（2026-06-12 收紧口径：销售/电工/装配/售后不可）。
    与菜单配置同源：有 'list' 菜单即可进详单。"""
    return "list" in user_menu_keys(user)
