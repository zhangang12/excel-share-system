"""Pydantic 模型：API 请求 / 响应"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict


# ---------- 通用 ----------
class Msg(BaseModel):
    message: str


# ---------- 角色 ----------
class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    description: Optional[str] = None


# ---------- 用户 ----------
class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    full_name: Optional[str] = None
    email: Optional[str] = None
    # 多角色：优先用 role_ids（可多选）；role_id 保留兼容旧调用（单角色）
    role_id: Optional[int] = None
    role_ids: Optional[list[int]] = None
    is_active: bool = True



class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    role_id: Optional[int] = None
    role_ids: Optional[list[int]] = None  # 传则整体替换该用户的角色集
    is_active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=6, max_length=128)
    hidden_tabs: Optional[list[str]] = None  # 🆕 #7 传则整体替换该账号隐藏的二级菜单tab


# 🆕 反馈#268 按账号开通管理组菜单（目前仅 dict-admin 字典设置）
#   2026-07-21 起为兼容包装：对管理组 key 做「入参含有的加入 menus、不含的移除」
class GrantMenusIn(BaseModel):
    grant_menus: list[str] = []  # 目标开通的管理组菜单 key（仅影响 menus 中的管理组部分）


# 🆕 一级菜单按账号配置（整体替换；key ⊆ MENU_DEFS ∪ ADMIN_MENU_DEFS）
class SetMenusIn(BaseModel):
    menus: list[str] = []


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    # 锚点角色（兼容旧前端字段）
    role_id: int
    role_code: Optional[str] = None
    role_name: Optional[str] = None
    # 🆕 全部角色（平等多角色）
    role_ids: list[int] = []
    role_codes: list[str] = []
    role_names: list[str] = []
    is_active: bool
    password_must_change: bool = False
    wxid: Optional[str] = None  # 🆕 v3 企微绑定
    hidden_tabs: list[str] = []  # 🆕 #7 该账号隐藏的二级菜单tab
    menus: list[str] = []        # 🆕 该账号配置的一级菜单 key（业务+管理组混合，规范顺序）
    grant_menus: list[str] = []  # 派生值 = menus ∩ 管理组有效 key（兼容旧客户端，不再独立存储）
    created_at: datetime
    last_login: Optional[datetime] = None


# ---------- 🆕 菜单 ----------
class MenuItem(BaseModel):
    key: str
    label: str


class MenusOut(BaseModel):
    menus: list[MenuItem]
    can_view_detail: bool


# ---------- 🆕 附件 ----------
class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    biz_type: str
    biz_id: Optional[int] = None
    kind: Optional[str] = None
    project_id: Optional[int] = None
    name: str
    ext: Optional[str] = None
    size: int
    uploaded_by: Optional[int] = None
    created_at: datetime


# ---------- 🆕 部门任务单 ----------
class OrderCreate(BaseModel):
    project_id: int
    dept: str                      # design / electric / produce
    req_text: Optional[str] = None
    worker_id: Optional[int] = None  # 管理层目录下单可直接指派


class OrderAssignIn(BaseModel):
    worker_id: int


class OrderStartIn(BaseModel):
    start_date: str  # YYYY-MM-DD
    due_date: str


class OrderCompleteIn(BaseModel):
    notify_user_id: int


class OrderReassignIn(BaseModel):
    worker_id: int


class OrderEditDueIn(BaseModel):
    due_date: str  # YYYY-MM-DD（管理层修改预计完成时间）


class ProduceGroupBrief(BaseModel):
    """🆕 生产单两组(钣金/装配)概要，供任务跟踪父视图展示各组预计完成/完成。"""
    group: str                       # sheetmetal / assembly
    name: str                        # 钣金 / 装配
    due_date: Optional[str] = None   # 本组预计完成
    done_date: Optional[str] = None  # 本组完成日期


class OrderOut(BaseModel):
    id: int
    project_id: int
    project_code: str
    project_name: str
    dept: str
    status: str
    worker_id: Optional[int] = None
    worker_name: Optional[str] = None
    req_text: Optional[str] = None
    start_date: Optional[str] = None
    due_date: Optional[str] = None
    done_date: Optional[str] = None
    notify_user_id: Optional[int] = None
    notify_user_name: Optional[str] = None
    eff_pct: Optional[int] = None      # 完成效率%（C2/C3 口径）
    on_time: Optional[bool] = None
    overdue: bool = False              # 进行中且超预计 / 完成且逾期
    created_at: datetime
    design_done_flag:   bool = False  # 🆕 设计完成第一步标记
    electric_done_flag: bool = False  # 🆕 接线完成第一步标记
    ship_prep_done:     bool = False  # 🆕 #5 设计部发货准备完成标记
    packlist_status: Optional[str] = None  # 🆕 发货清单：none/requested/ready（仅设计部任务所属项目有意义）
    input_files: list[AttachmentOut] = []
    start_files: list[AttachmentOut] = []
    output_files: list[AttachmentOut] = []
    produce_groups: Optional[list[ProduceGroupBrief]] = None  # 🆕 仅生产单：钣金/装配两组预计完成
    standard_datasheet_id: Optional[int] = None  # 🆕 #6 所属项目「标准件清单」数据表 id（电工部只读引用）
    material_locations: list[str] = []  # 🆕 #204 本项目材料所在库位（仓库收货入库时填,同步到设计/电工工作台）


class OrderOptionUser(BaseModel):
    id: int
    name: str


# ---------- 🆕 销售台账 / 销售下单 ----------
class SalesLedgerRow(BaseModel):
    id: int
    project_id: int
    code: str
    name: str
    status: str
    qty: Optional[int] = None             # 🆕 数量（解析一览「数量」单元格）
    unit: Optional[str] = None            # 🆕 单位 台/套
    sales_uid: Optional[int] = None
    sales_name: Optional[str] = None
    customer: Optional[str] = None
    cust_type: Optional[str] = None
    sign_date: Optional[str] = None      # 下单日期=合同签订日期（读项目 __o__签订日期）
    deliver_date: Optional[str] = None
    contract: str = "无"
    contract_file_id: Optional[int] = None
    contract_file_name: Optional[str] = None
    amount: float = 0
    tax_rate: Optional[str] = None
    invoice_state: Optional[str] = None
    invoice_batch_id: Optional[int] = None   # 🆕 合并开票批次号(同客户多项目合并)；None=单项目
    void_state: Optional[str] = None         # 🆕 订单作废流：None 正常 / applying 待审批 / voided 已作废
    void_reason: Optional[str] = None
    order_state: Optional[str] = None        # 🆕 下单审批流：None 已生效 / pending 待主管审批 / draft 被退回
    order_reject_reason: Optional[str] = None  # draft 时的退回原因
    pending_order: Optional[dict] = None     # pending/draft 时的派单信息(depts/req_text/receiver)，供前端预填
    invoice_apply_file_id: Optional[int] = None
    invoice_apply_file_name: Optional[str] = None
    invoice_file_id: Optional[int] = None
    invoice_file_name: Optional[str] = None
    prepay: float = 0
    prepay_note: Optional[str] = None        # 🆕 预付收款批注(支持插入时间戳)
    before_ship: float = 0
    before_ship_note: Optional[str] = None   # 🆕 发货前付收款批注
    ship_receivable: float = 0
    balance: float = 0
    balance_date: Optional[str] = None
    balance_note: Optional[str] = None       # 🆕 反馈#233 尾款到账批注
    ship_date: Optional[str] = None
    order_type: Optional[str] = None       # 🆕 调货订单 / 工厂制作订单
    revision_open: bool = False            # 🆕 #1 是否有未处理的技术资料修订意见
    revision_reason: Optional[str] = None  # 🆕 #1 最新一条未处理修订意见内容（供 tooltip）


class RevisionRequestIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)  # 🆕 #1 修订意见内容（必填）


class SalesLedgerTotals(BaseModel):
    count: int = 0
    amount: float = 0
    uninvoiced: float = 0      # 未开票金额合计（invoice_state != invoiced 的 amount）
    prepay: float = 0
    before_ship: float = 0
    ship_receivable: float = 0
    balance: float = 0


class SalesLedgerListOut(BaseModel):
    rows: list[SalesLedgerRow]
    totals: Optional[SalesLedgerTotals] = None  # 仅主管/管理层视角返回
    total: Optional[int] = None                 # 🆕 分页：当前筛选下的总条数（rows 只是一页）


class VoidApplyIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)  # 🆕 订单作废原因（必填）


class OrderRejectIn(BaseModel):
    reason: str = ""  # 🆕 下单退回原因（可选）


class SalesReceiverIn(BaseModel):
    name: str = ""
    company: str = ""
    phone: str = ""
    addr: str = ""


class SalesOrderCreate(BaseModel):
    code: str = ""                        # 🆕 项目编号：人工输入（取消自动生成）
    code_suffix: str = ""                 # (废弃)编号后缀字母——保留字段兼容，已不用
    name: str = Field(min_length=1, max_length=255)   # 设备名称
    qty: int = 1                          # 🆕 数量
    unit: str = "台"                       # 🆕 单位：台/套
    customer: str = ""
    cust_type: str = "经销商"
    contract: str = "有"
    amount: float = 0
    tax_rate: str = "13%"
    prepay: float = 0
    prepay_note: str = ""                 # 🆕 预付收款批注(选填)
    before_ship: float = 0
    before_ship_note: str = ""            # 🆕 发货前付收款批注(选填)
    ship_receivable: float = 0
    balance: float = 0
    balance_date: str = ""
    depts: list[str] = Field(default_factory=list)    # 派往部门（design/electric/produce）
    req_text: str = ""
    receiver: Optional[SalesReceiverIn] = None


class SalesOrderOut(BaseModel):
    project_id: int
    code: str
    order_ids: list[int]
    ledger_id: Optional[int] = None   # 🆕 待审批下单暂存资料用（前端按此调 pending-files）


class SpareOrderCreate(BaseModel):
    """🆕 备机下单（设计部负责人/管理层）：建项目+派各部门，不建销售台账。"""
    code: str = ""
    name: str = Field(min_length=1, max_length=255)
    qty: int = 1
    unit: str = "台"
    depts: list[str] = Field(default_factory=lambda: ["produce", "electric"])
    req_text: str = ""


class SalesLedgerUpdate(BaseModel):
    name: Optional[str] = None            # 设备名称（同步 Project.name）
    customer: Optional[str] = None
    cust_type: Optional[str] = None
    contract: Optional[str] = None
    amount: Optional[float] = None
    tax_rate: Optional[str] = None
    prepay: Optional[float] = None
    prepay_note: Optional[str] = None        # 🆕 预付收款批注
    before_ship: Optional[float] = None
    before_ship_note: Optional[str] = None   # 🆕 发货前付收款批注
    ship_receivable: Optional[float] = None
    balance: Optional[float] = None
    balance_date: Optional[str] = None
    sign_date: Optional[str] = None       # 🆕 下单日期(=合同签订日期)，回写项目一览
    deliver_date: Optional[str] = None    # 🆕 交货日期，回写项目一览
    sales_uid: Optional[int] = None       # 🆕 销售员改派


class PaymentNoteUpdate(BaseModel):
    """🆕 单笔收款批注（预付 / 发货前付）独立更新——销售本人即可记录，不受开票锁限制。"""
    field: str            # 'prepay' | 'before_ship'
    note: str = ""


class NextCodeOut(BaseModel):
    code: str


# ---------- 🆕 售后部 ----------
class AfterSalesRow(BaseModel):
    id: int
    project_id: Optional[int] = None   # #158：以往项目只填名称时为空
    kind: str = "aftersales"   # 🆕 需求一：aftersales 售后 / install 安装
    code: str
    name: str
    problem: str
    cost: float
    status: str
    mat_file_id: Optional[int] = None
    mat_file_name: Optional[str] = None
    created_by_name: Optional[str] = None
    created_at: datetime


class AfterSalesStats(BaseModel):
    total: int = 0
    pending: int = 0
    approved_cost: float = 0
    total_cost: float = 0


class AfterSalesListOut(BaseModel):
    rows: list[AfterSalesRow]
    stats: AfterSalesStats


class AfterSalesProjOption(BaseModel):
    id: int
    code: str
    name: str


# ---------- 🆕 生产问题反馈 ----------
class FeedbackCreate(BaseModel):
    project_id: int
    content: str


class FeedbackRow(BaseModel):
    id: int
    project_id: int
    code: str
    name: str
    content: str
    status: str
    created_by_name: Optional[str] = None
    designer_name: Optional[str] = None
    designer_uid: Optional[int] = None   # 空=无在岗设计师(死信)，供前端判断是否可指派
    created_at: datetime
    images: list[dict] = Field(default_factory=list)   # 🆕 #193 反馈附图 [{id,name}]


class FeedbackProjOption(BaseModel):
    id: int
    code: str
    name: str


# ---------- 🆕 仓库组 ----------
class MaterialCategoryIn(BaseModel):
    """🆕 物料编码分类节点：段码=数字(大类1位/中类2位/细分2位)。"""
    parent_id: Optional[int] = None
    seg_code: str = Field(min_length=1, max_length=4, pattern=r"^\d+$")
    name: str = Field(min_length=1, max_length=64)
    sort_order: int = 0
    enabled: bool = True


class MaterialCategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    parent_id: Optional[int] = None
    level: int
    seg_code: str
    name: str
    sort_order: int
    enabled: bool


class WhMaterialIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    spec: Optional[str] = None
    category: Optional[str] = None
    material_grade: Optional[str] = None   # 🆕 材质（字典管理，如 304不锈钢/碳钢/铝合金）
    unit: str = "个"
    unit_price: Optional[float] = None     # 🆕 需求三：参考单价
    location: Optional[str] = None
    safety_stock: float = 0
    init_stock: float = 0
    code: Optional[str] = None
    category_id: Optional[int] = None   # 🆕 编码分类(选到细分类自动发码)
    custom_values: dict = Field(default_factory=dict)   # 🆕 自定义字段值


class WhMaterialOut(BaseModel):
    id: int
    code: Optional[str] = None
    category_id: Optional[int] = None   # 🆕 编码分类
    category_path: Optional[str] = None # 🆕 编码文字说明:大类/中类/细分 名称路径(每个编码的含义)
    name: str
    spec: Optional[str] = None
    category: Optional[str] = None
    material_grade: Optional[str] = None
    unit: str
    unit_price: Optional[float] = None    # 🆕 需求三：参考单价
    location: Optional[str] = None
    safety_stock: float
    init_stock: float
    status: str
    stock: float = 0          # 实时库存
    stock_value: Optional[float] = None   # 🆕 需求三：库存总价=现存×单价
    low: bool = False         # 是否低于安全库存
    custom_values: dict = Field(default_factory=dict)   # 🆕 自定义字段值
    # 🆕 出库反显：物料按项目入库时的关联项目(入库流水唯一项目才反显)
    project_id: Optional[int] = None
    project_code: Optional[str] = None


class WhMaterialCustomFieldIn(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    ftype: str = "text"                           # text/number/date/select
    options: list[str] = Field(default_factory=list)
    required: bool = False
    show_in_list: bool = True
    sort_order: int = 0
    enabled: bool = True


class WhMaterialCustomFieldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    label: str
    ftype: str
    options: list[str] = Field(default_factory=list)
    required: bool
    show_in_list: bool
    sort_order: int
    enabled: bool


class WhLocationIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    note: Optional[str] = Field(default=None, max_length=128)
    sort_order: int = 0
    enabled: bool = True


class WhLocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    note: Optional[str] = None
    sort_order: int
    enabled: bool
    mat_count: int = 0     # 当前挂在该库位的物料数(删除保护提示用)
    # 🆕 #204 占用/空闲：由库存(出入库流水净值)驱动——库位上有物料现存>0=占用,空=空闲
    occupied: bool = False
    occupied_items: list[dict] = Field(default_factory=list)  # 占用内容 [{name,spec,stock}]


class WhTxnIn(BaseModel):
    material_id: int
    biz_date: str
    direction: str            # in / out
    qty: float
    unit_price: Optional[float] = None   # 🆕 单价（选填；填了自动算金额，用于库存金额/成本统计）
    source: Optional[str] = None
    party: Optional[str] = None
    project_id: Optional[int] = None
    location: Optional[str] = None       # 🆕 库位：入库=放到哪(选填,默认物料当前库位并回写物料)
    # 🆕 盈利改善1b·堵「无主领料」黑洞：出库必须挂项目；确属非项目领用需明确勾选并填原因
    non_project: bool = False
    non_project_reason: Optional[str] = None


class WhTxnOut(BaseModel):
    id: int
    material_id: int
    material_name: str
    spec: Optional[str] = None
    biz_date: str
    direction: str
    qty: float
    unit_price: Optional[float] = None
    amount: Optional[float] = None
    source: Optional[str] = None
    party: Optional[str] = None
    project_id: Optional[int] = None
    project_code: Optional[str] = None
    location: Optional[str] = None       # 🆕 库位
    ref_no: str
    is_reversal: bool = False
    reversed: bool = False
    created_at: datetime


class WhSummaryRow(BaseModel):
    material_id: int
    name: str
    spec: Optional[str] = None
    unit: str
    opening: float = 0        # 期初
    in_qty: float = 0         # 本期入
    out_qty: float = 0        # 本期出
    closing: float = 0        # 期末


class WhStockOut(BaseModel):
    materials: list[WhMaterialOut]
    total: int = 0
    low_count: int = 0


class WhMaterialSuggestOut(BaseModel):
    """🆕 #278/#289 物料名称联想：返回 name+spec，选中后前端自动带出「规格型号」
    （采购申请弹窗「名称」、项目详单「名称」列的 el-autocomplete 共用）。"""
    name: str
    spec: Optional[str] = None


# ---------- 🆕 财务部 ----------
class FinanceInvoiceRow(BaseModel):
    ledger_id: int
    code: str
    name: str
    customer: Optional[str] = None
    sales_name: Optional[str] = None
    amount: float = 0
    tax_rate: Optional[str] = None
    invoice_batch_id: Optional[int] = None   # 🆕 合并开票批次号；同批多行共享，财务一次开票
    apply_file_id: Optional[int] = None
    apply_file_name: Optional[str] = None
    invoice_file_id: Optional[int] = None
    invoice_file_name: Optional[str] = None


class OrderOptionsOut(BaseModel):
    workers: list[OrderOptionUser]
    # 🆕 外协人员（dept_config.OUTSOURCE_WORKERS）：其任务单在工作台单列「外协订单」tab
    outsource_workers: list[OrderOptionUser] = Field(default_factory=list)
    notify_pool: list[OrderOptionUser]
    notify_label: str
    dept_name: str
    sheet_check: bool
    start_outputs: list[dict]
    outputs: list[dict]
    start_label: str
    end_label: str
    done_label: str


# ---------- 🆕 站内消息 ----------
class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    text: str
    read: bool
    biz_type: Optional[str] = None
    biz_id: Optional[int] = None
    created_at: datetime


class UnreadCountOut(BaseModel):
    count: int


class WxidIn(BaseModel):
    wxid: str = ""


# ---------- 🆕 导出审批 ----------
class ExportRequestIn(BaseModel):
    scope: str = "数据导出"


class ExportRequestOut(BaseModel):
    id: int
    user_id: int
    user_name: Optional[str] = None
    user_role: Optional[str] = None
    scope: str
    status: str
    created_at: datetime


class ExportConfigOut(BaseModel):
    enabled: bool
    can_export: bool   # 当前用户是否已可导出（管理层或已获权）


# ---------- 认证 ----------
class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=128)


# ---------- 项目 ----------
class ProjectCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    status: Optional[str] = "进行中"
    manager_id: Optional[int] = None


class ProjectUpdate(BaseModel):
    code: Optional[str] = Field(default=None, min_length=1, max_length=64)
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    manager_id: Optional[int] = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    description: Optional[str] = None
    status: str
    manager_id: Optional[int] = None
    manager_name: Optional[str] = None
    member_count: int = 0
    created_at: datetime
    updated_at: datetime
    # 项目级元数据（项目头表的值）：{数量, 制表日期, 销售, 设计师, 电器, 下单日期, 交货日期, ...}
    header_meta: dict = Field(default_factory=dict)
    # 一览字段的值（__o__ 前缀去掉后）：{签订日期, 交货日期, 销售, 设计师, 制图开始, 制图结束, 制图用时, 电工, ...}
    # 项目详情头表「镜像一览」时读这里，保证两处同源同步
    overview_meta: dict = Field(default_factory=dict)


class HeaderCellUpdate(BaseModel):
    """更新项目头单元格 / 项目一览单元格"""
    key: str = Field(min_length=1, max_length=64)
    value: Optional[str] = None  # None 或空串 = 清空
    # is_overview=True 时写入 __o__<key>（一览独立存储）；否则写 __h__<key>
    is_overview: bool = False


class ProjectMemberIn(BaseModel):
    user_id: int
    permission: str = "edit"  # edit / view


class ProjectMemberBatchIn(BaseModel):
    """批量添加成员：一次加多个用户"""
    user_ids: list[int] = Field(default_factory=list)
    permission: str = "edit"  # edit / view


class ProjectMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    username: str
    full_name: Optional[str] = None
    role_name: Optional[str] = None
    permission: str
    added_at: datetime


# ---------- 数据表 ----------
FIELD_TYPES = ("text", "number", "date", "select", "multi_select", "person")


class DatasheetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class DatasheetUpdate(BaseModel):
    name: Optional[str] = None
    sort_order: Optional[int] = None


class DatasheetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    name: str
    sort_order: int
    field_count: int = 0
    record_count: int = 0
    header_lines: Optional[list[list[str]]] = None
    imported: bool = False      # 🆕 四表校验：是否已导入 Excel
    done_flag: bool = False     # 🆕 装配前置完成标记
    created_at: datetime
    updated_at: datetime


# ---------- 字段 ----------
class FieldCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    type: str = "text"
    sort_order: Optional[int] = 0
    config: Optional[dict] = None


class FieldUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    sort_order: Optional[int] = None
    config: Optional[dict] = None


class FieldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    datasheet_id: int
    name: str
    type: str
    sort_order: int
    config: Optional[dict] = None
    created_at: datetime


# ---------- 行 ----------
class RecordCreate(BaseModel):
    values: dict = Field(default_factory=dict)
    sort_order: Optional[int] = 0


class RecordUpdate(BaseModel):
    values: Optional[dict] = None
    sort_order: Optional[int] = None


class RecordCellUpdate(BaseModel):
    field_id: int
    value: object = None


class RecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    datasheet_id: int
    sort_order: int
    values: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


# ---------- 项目一览 ----------
class OverviewFieldCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    type: str = "text"
    config: Optional[dict] = None


class OverviewFieldUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    sort_order: Optional[int] = None
    config: Optional[dict] = None


class OverviewFieldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    type: str
    sort_order: int
    config: Optional[dict] = None


class OverviewProjectRow(BaseModel):
    """一行 = 一个项目，含基础字段 + extra"""
    id: int
    code: str
    name: str
    status: str
    description: Optional[str] = None
    manager_id: Optional[int] = None
    manager_name: Optional[str] = None
    extra: dict = Field(default_factory=dict)
    updated_at: datetime


class OverviewBundle(BaseModel):
    fields: list[OverviewFieldOut]
    rows: list[OverviewProjectRow]


class OverviewCellUpdate(BaseModel):
    field_id: int
    value: object = None


# ---------- 字段权限 ----------
class FieldPermissionItem(BaseModel):
    role_id: int
    role_name: Optional[str] = None
    can_view: bool = True
    can_edit: bool = True


class FieldPermissionSetIn(BaseModel):
    """批量设置某字段在所有角色下的权限"""
    permissions: list[FieldPermissionItem]


class ClonePermsIn(BaseModel):
    """从某个源项目克隆字段级权限到当前项目"""
    source_project_id: int


class ClonePermsResult(BaseModel):
    """权限克隆结果汇总"""
    cloned_field_count: int = 0
    matched_datasheets: list[str] = Field(default_factory=list)
    unmatched_target_datasheets: list[str] = Field(default_factory=list)
    skipped_target_fields: list[str] = Field(default_factory=list)
    message: str = "克隆成功"


# ---------- 审计 ----------
class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: Optional[int] = None
    username: Optional[str] = None
    action: str
    target_type: Optional[str] = None
    target_id: Optional[int] = None
    detail: Optional[str] = None
    ip: Optional[str] = None
    created_at: datetime


# ---------- 🆕 用户反馈小助手 ----------
class UserFeedbackRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    content: str
    page_url: Optional[str] = None
    user_agent: Optional[str] = None
    status: str
    created_at: datetime
    user_id: Optional[int] = None
    user_name: Optional[str] = None
    user_role: Optional[str] = None
    shot_file_id: Optional[int] = None
    shot_file_name: Optional[str] = None
    # 🆕 系统回信
    reply: Optional[str] = None
    replied_at: Optional[datetime] = None
    replier_name: Optional[str] = None
    reply_read: bool = False


class UserFeedbackStats(BaseModel):
    """🆕 反馈概览统计（按当前可见范围全量算，不受 kind/status 过滤与分页影响）。"""
    total: int = 0
    open: int = 0
    done: int = 0
    bug: int = 0
    suggest: int = 0
    other: int = 0


class UserFeedbackListOut(BaseModel):
    """🆕 反馈分页结果：items=当前页，total=过滤后总数，stats=概览统计。"""
    items: list[UserFeedbackRow] = []
    total: int = 0
    page: int = 1
    page_size: int = 20
    stats: UserFeedbackStats = UserFeedbackStats()


# ==================== 🆕 销售线索跟踪 ====================
class SalesLeadCreate(BaseModel):
    source: str = Field(min_length=1, max_length=32)        # 询盘来源(必填)
    customer: Optional[str] = None
    contact: Optional[str] = None
    phone: Optional[str] = None
    wechat: Optional[str] = None
    requirement: Optional[str] = None
    owner_uid: Optional[int] = None                         # 分配给的销售(可留空=进线索池)
    status: Optional[str] = None                            # 默认 潜在需求
    follow_log: Optional[str] = None


class SalesLeadUpdate(BaseModel):
    source: Optional[str] = None                            # 仅主管/管理层可改
    customer: Optional[str] = None
    contact: Optional[str] = None
    phone: Optional[str] = None
    wechat: Optional[str] = None
    requirement: Optional[str] = None
    owner_uid: Optional[int] = None                         # 改派,仅主管/管理层
    status: Optional[str] = None
    follow_log: Optional[str] = None
    lost_reason: Optional[str] = None


class SalesLeadRow(BaseModel):
    id: int
    source: str
    customer: Optional[str] = None
    contact: Optional[str] = None
    phone: Optional[str] = None
    wechat: Optional[str] = None
    requirement: Optional[str] = None
    owner_uid: Optional[int] = None
    owner_name: Optional[str] = None
    status: str
    follow_log: Optional[str] = None
    lost_reason: Optional[str] = None
    created_by: Optional[int] = None
    created_by_name: Optional[str] = None
    assigned_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class SalesLeadListOut(BaseModel):
    rows: list[SalesLeadRow]
    total: int = 0


class LeadReportItem(BaseModel):
    key: str                  # 来源名 / 销售名
    leads: int = 0            # 线索数
    deal: int = 0             # 成交数
    quote: int = 0            # 报价中
    potential: int = 0        # 潜在需求
    lost: int = 0             # 丢单
    rate: float = 0           # 成交率 = deal / leads


class SalesLeadReport(BaseModel):
    by_source: list[LeadReportItem]
    by_owner: list[LeadReportItem]
    total_leads: int = 0
    total_deal: int = 0
    total_rate: float = 0


# ==================== 🆕 采购管理模块 ====================

# ---------- 供应商 ----------
class SupplierCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    code: Optional[str] = None
    category: Optional[str] = None
    contact: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    tax_no: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account: Optional[str] = None
    settlement_type: Optional[str] = None
    credit_days: Optional[int] = None
    notes: Optional[str] = None


class SupplierUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    category: Optional[str] = None
    contact: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    tax_no: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account: Optional[str] = None
    settlement_type: Optional[str] = None
    credit_days: Optional[int] = None
    notes: Optional[str] = None


class SupplierOut(BaseModel):
    id: int
    name: str
    code: Optional[str] = None
    category: Optional[str] = None
    contact: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    tax_no: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account: Optional[str] = None
    settlement_type: Optional[str] = None
    credit_days: Optional[int] = None
    status: str
    notes: Optional[str] = None
    created_by: Optional[int] = None          # 🆕 需求五：建档采购员
    created_by_name: Optional[str] = None
    created_at: datetime


# ---------- 采购明细 ----------
class PurchaseItemCreate(BaseModel):
    supplier_id: int
    delivery_date: Optional[str] = None
    expected_arrival: Optional[str] = None   # 🆕 预计到货日期（选填；到期未到货每日提醒）
    contract_no: Optional[str] = None
    project_code: Optional[str] = None
    delivery_note_no: Optional[str] = None
    item_name: str = Field(min_length=1, max_length=128)
    spec: Optional[str] = None
    brand: Optional[str] = None
    qty: Optional[float] = None
    unit_price: Optional[float] = None
    received_amount: float = 0
    invoice_date: Optional[str] = None
    tax_rate: Optional[str] = None
    invoice_amount: float = 0
    payment_method: Optional[str] = None
    prepay_ratio: Optional[float] = None   # 🆕 预付比例(%)，仅现金预付/对公预付时有意义
    invoice_status: str = "待对账"
    notes: Optional[str] = None
    custom_values: dict = Field(default_factory=dict)   # 🆕 R6 {str(field_id): value}


# ---------- 🆕 采购单：同一供应商一次录入多个零件行 ----------
class PurchaseOrderLine(BaseModel):
    item_name: str = Field(min_length=1, max_length=128)
    spec: Optional[str] = None
    brand: Optional[str] = None
    project_code: Optional[str] = None          # 行级项目编号（留空则用采购单默认项目）
    qty: Optional[float] = None
    unit_price: Optional[float] = None           # 选填：后填价格流程可留空，货到仓库再补
    received_amount: Optional[float] = None
    expected_arrival: Optional[str] = None       # 🆕 逐行预计到货（留空用表头/供应商级值）
    tax_rate: Optional[str] = None
    notes: Optional[str] = None
    custom_values: dict = Field(default_factory=dict)   # 🆕 R6


# ---------- 🆕 R6 采购自定义字段 ----------
class PurchaseCustomFieldIn(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    ftype: str = "text"                           # text/number/date/select
    options: list[str] = Field(default_factory=list)
    required: bool = False
    show_in_list: bool = True
    sort_order: int = 0
    enabled: bool = True


class PurchaseCustomFieldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    label: str
    ftype: str
    options: list[str] = Field(default_factory=list)
    required: bool
    show_in_list: bool
    sort_order: int
    enabled: bool


# ---------- 🆕 物料字典（类别 / 单位 / 供应商分类 受管理取值） ----------
class MaterialDictIn(BaseModel):
    dtype: str = Field(pattern="^(category|unit|supplier_category|material_grade|order_no)$")   # + order_no 订单编号(非项目)
    value: str = Field(min_length=1, max_length=64)
    sort_order: int = 0
    enabled: bool = True


class MaterialDictOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    dtype: str
    value: str
    sort_order: int
    enabled: bool


class PurchaseOrderCreate(BaseModel):
    supplier_id: int
    delivery_date: Optional[str] = None          # 下单日期（总的）
    expected_arrival: Optional[str] = None        # 🆕 预计到货日期（供应商级默认，选填；逐行 expected_arrival 优先）
    contract_no: Optional[str] = None
    project_code: Optional[str] = None           # 默认项目编号（行可覆盖）
    payment_method: Optional[str] = None          # 🆕 付款方式（表头，作用于全单）
    prepay_ratio: Optional[float] = None          # 🆕 预付比例(%)（表头，作用于全单）
    is_stock: bool = True                          # 🆕 是否备货：True=收货只入库；False=收货入库+出库
    stock_location: Optional[str] = None           # 🆕 库位（整单一个，仓库收货按此入库）
    lines: list[PurchaseOrderLine] = Field(min_length=1)


class PurchaseImportResult(BaseModel):
    created: int = 0
    suppliers_created: int = 0
    failed: int = 0
    errors: list[str] = Field(default_factory=list)


# ---------- 🆕 清单 → 采购下单 / 仓库需求 ----------
class PurchasableRow(BaseModel):
    sheet_id: int
    record_id: int
    item_name: str
    spec: Optional[str] = None
    brand: Optional[str] = None          # 🆕 清单里带出的品牌（下单时可改）
    material: Optional[str] = None       # 🆕 材质（不锈钢下料单专有列，单独成列不再塞备注）
    drawing: Optional[str] = None        # 🆕 #159/#160 图纸名称（不锈钢下料单专有；下单折进 spec 带上采购单）
    qty: Optional[float] = None          # 清单需求量
    stock: float = 0                      # 现有库存（按名称+规格匹配物料）
    suggest_purchase: float = 0           # 建议采购量 = 需求 - 库存
    notes: Optional[str] = None
    status: str = "未下单"          # 未下单 / 已下单 / 已到货
    # 🆕 跨项目待下单聚合用（单项目 purchasable 不填）
    sheet_key: Optional[str] = None      # 清单类型 key(standard/elec_po/...)
    project_id: Optional[int] = None
    project_code: Optional[str] = None
    project_name: Optional[str] = None


class OrderFromListLine(BaseModel):
    source_sheet_id: Optional[int] = None
    source_record_id: Optional[int] = None
    item_name: str = Field(min_length=1, max_length=128)
    spec: Optional[str] = None
    brand: Optional[str] = None          # 🆕 逐行品牌
    qty: Optional[float] = None
    unit_price: Optional[float] = None
    expected_arrival: Optional[str] = None   # 🆕 逐行预计到货（跟着零件走，回写该行清单；留空用整单/供应商级值）
    payment_method: Optional[str] = None  # 🆕 逐行付款方式（不同批次可能不一样，不随供应商固定）
    prepay_ratio: Optional[float] = None  # 🆕 逐行预付比例(%)
    notes: Optional[str] = None


class OrderFromListCreate(BaseModel):
    supplier_id: int
    delivery_date: Optional[str] = None
    expected_arrival: Optional[str] = None   # 🆕 预计到货（供应商级默认，选填；逐行 expected_arrival 优先，逐行回写清单「预计到货」列）
    project_code: Optional[str] = None
    payment_method: Optional[str] = None
    prepay_ratio: Optional[float] = None
    stock_location: Optional[str] = None   # 🆕 库位（整单一个，仓库收货按此入库）
    lines: list[OrderFromListLine] = Field(min_length=1)


class WarehouseDemandRow(BaseModel):
    item_name: str
    spec: Optional[str] = None
    material_id: Optional[int] = None        # 🆕 需求二：命中的物料 id（有则可一键领用出库）
    location: Optional[str] = None           # 🆕 #204 材料库位（命中物料的库位,供各组知道去哪拿）
    demand_qty: Optional[float] = None      # 需求量（清单数量）
    stock: float = 0                         # 现有库存
    suggest_purchase: float = 0              # 建议采购量 = 需求 - 库存
    purchase_status: str = "未下单"          # 未下单 / 已下单 / 已到货
    in_stock: bool = False                   # 是否有货可出
    issued_qty: float = 0                    # 🆕 需求二：已领用出库到本项目的数量
    source: str = "清单"                     # 🆕 清单=标准件清单需求；采购=采购单入库到本项目(不在清单)


class WarehouseDemandOverviewRow(BaseModel):
    # 🆕 #157：物料需求总览一行（一个项目）
    project_id: int
    code: str
    name: str
    total_lines: int = 0      # 清单物料行数
    pending_out: int = 0      # 待出库：有货且仍有未领需求的行数
    issued_out: int = 0       # 已出库：已领用过的行数


class DemandIssueLine(BaseModel):
    material_id: int
    qty: float


class DemandIssueIn(BaseModel):
    """🆕 需求二：物料需求一键领用出库。"""
    lines: list[DemandIssueLine] = Field(default_factory=list)


class WhClearIn(BaseModel):
    """🆕 需求十五：仓库一键清空确认（需输入确认词「清空仓库」）。"""
    confirm: str = ""


class PurchaseItemUpdate(BaseModel):
    supplier_id: Optional[int] = None
    delivery_date: Optional[str] = None
    contract_no: Optional[str] = None
    project_code: Optional[str] = None
    delivery_note_no: Optional[str] = None
    item_name: Optional[str] = None
    spec: Optional[str] = None
    brand: Optional[str] = None
    qty: Optional[float] = None
    unit_price: Optional[float] = None
    received_amount: Optional[float] = None
    invoice_date: Optional[str] = None
    tax_rate: Optional[str] = None
    invoice_amount: Optional[float] = None
    payment_method: Optional[str] = None
    prepay_ratio: Optional[float] = None
    # 注意：不允许在这里开放 arrival_date——到货日期只能由仓库收货接口写入，
    # 否则采购员可绕过收货流程直接填/清到货日期，让「到期未到货提醒」失真（且不入库、不回写清单）。
    expected_arrival: Optional[str] = None   # 🆕 预计到货日期（可改；改动会回写清单「预计到货」列并通知管理层）
    custom_values: Optional[dict] = None   # 🆕 R6
    invoice_status: Optional[str] = None
    notes: Optional[str] = None


class PurchaseReceiveIn(BaseModel):
    """仓库收货：填送货单号 / 到货日期；后填价格流程可一并补单价与收货金额。"""
    delivery_note_no: Optional[str] = None
    arrival_date: Optional[str] = None
    unit_price: Optional[float] = None
    received_amount: Optional[float] = None
    stock_location: Optional[str] = None  # 🆕 #204 库位改由仓库收货时填（取代采购下单填）
    project_code: Optional[str] = None    # 🆕 #253 订单编号：手工采购单没填的，仓库收货可补/改


class BatchReceiveLine(BaseModel):
    item_id: int
    unit_price: Optional[float] = None
    received_amount: Optional[float] = None


class BatchReceiveIn(BaseModel):
    """🆕 需求四：合并零件收货。total_amount 有值=合并总价(按数量分摊)；否则用 lines 逐行填价。"""
    item_ids: list[int]
    delivery_note_no: Optional[str] = None
    arrival_date: str
    total_amount: Optional[float] = None
    lines: list[BatchReceiveLine] = Field(default_factory=list)
    stock_location: Optional[str] = None  # 🆕 #204 库位改由仓库收货时填（整批一个）
    project_code: Optional[str] = None    # 🆕 #253 订单编号：整批补/改（合并收货）


class PurchaseItemOut(BaseModel):
    id: int
    po_no: Optional[str] = None
    supplier_id: int
    supplier_name: str
    delivery_date: Optional[str] = None
    contract_no: Optional[str] = None
    project_code: Optional[str] = None
    delivery_note_no: Optional[str] = None
    arrival_date: Optional[str] = None
    expected_arrival: Optional[str] = None   # 🆕 预计到货日期
    item_name: str
    spec: Optional[str] = None
    brand: Optional[str] = None
    qty: Optional[float] = None
    unit_price: Optional[float] = None
    received_amount: float = 0
    invoice_date: Optional[str] = None
    tax_rate: Optional[str] = None
    invoice_amount: float = 0
    invoice_no: Optional[str] = None   # 🆕 需求十三：开票号
    paid_amount: float = 0
    paid_date: Optional[str] = None
    payment_method: Optional[str] = None
    prepay_ratio: Optional[float] = None
    invoice_status: str
    pay_status: str = "未付款"   # 🆕 未付款/已请款/已批待付/部分付款/已付款（B1=a：记录付款才算已付）
    # 🆕 反馈#277：已付款请款单的付款凭证（财务回执）——明细行可直接下载；无则 None
    pay_voucher_file_id: Optional[int] = None
    pay_voucher_name: Optional[str] = None
    custom_values: dict = Field(default_factory=dict)   # 🆕 R6 自定义字段值
    buyer_id: Optional[int] = None
    buyer_name: Optional[str] = None
    receipt_count: int = 0   # 🆕 需求十四：已上传收货单数量
    is_kit: bool = False                       # 🆕 成套采购：是否成套明细
    kit_parts: Optional[list] = None           # 🆕 成套采购：套内零件清单[{name,spec,qty}]
    is_stock: bool = True                      # 🆕 备货：True=收货只入库；False=收货入库+出库
    stock_location: Optional[str] = None       # 🆕 库位（采购下单填）
    notes: Optional[str] = None
    created_at: datetime


class KitFromListPart(BaseModel):
    """🆕 成套采购：套内一个零件（来自清单勾选行，描述性，不参与交易）。"""
    source_record_id: Optional[int] = None   # 来源清单行（用于回写"已下单"）
    name: str
    spec: Optional[str] = None
    qty: Optional[float] = None               # 每套数量


class KitFromListCreate(BaseModel):
    """🆕 按套下单（从清单打包成套）：从清单勾选一组零件(同一供应商)打包成「一套」，
    建**一条**成套采购明细，并回写这些清单行的订购日期/采购负责人(与从清单下单一致)。
    套单价 = kit_total / kit_qty；received_amount(套总价) = kit_total。"""
    supplier_id: int
    project_code: Optional[str] = None
    delivery_date: Optional[str] = None
    expected_arrival: Optional[str] = None   # 🆕 预计到货日期（整套一个，选填；落采购明细并回写各零件行清单「预计到货」列）
    payment_method: Optional[str] = None
    prepay_ratio: Optional[float] = None
    stock_location: Optional[str] = None   # 🆕 库位（整套一个）
    source_sheet_id: Optional[int] = None                  # 来源清单（同一张）
    kit_name: str = Field(min_length=1, max_length=128)    # 套名称（手填）
    kit_qty: float = Field(gt=0)                            # 套数
    kit_total: float = Field(ge=0)                          # 套总价
    parts: list[KitFromListPart] = Field(default_factory=list)   # 套内零件（清单勾选行）
    notes: Optional[str] = None


class PurchaseRequestLineIn(BaseModel):
    """🆕 #167 采购申请明细行。"""
    item_name: str = Field(min_length=1, max_length=128)
    spec: Optional[str] = None
    qty: Optional[float] = None
    project_code: Optional[str] = None
    notes: Optional[str] = None


class PurchaseRequestCreate(BaseModel):
    buyer_id: Optional[int] = None   # 🆕 #2 指定采购员（推送给他）
    notes: Optional[str] = None
    lines: list[PurchaseRequestLineIn] = Field(default_factory=list)
    # 🆕 #245/#246 二选一：可不填明细行，改为直接上传文件（先传 /attachments 拿 id 再带进来）
    attachment_ids: list[int] = Field(default_factory=list)


class PurchaseRequestLineOut(PurchaseRequestLineIn):
    id: int


class PurchaseRequestOut(BaseModel):
    id: int
    requester_id: Optional[int] = None
    requester_name: Optional[str] = None
    buyer_id: Optional[int] = None            # 🆕 #2 指定采购员
    buyer_name: Optional[str] = None
    status: str = "pending"       # pending/done/rejected
    notes: Optional[str] = None
    handler_name: Optional[str] = None
    handled_at: Optional[datetime] = None
    reject_reason: Optional[str] = None
    created_at: datetime
    lines: list[PurchaseRequestLineOut] = Field(default_factory=list)
    # 🆕 #245/#246 直接上传的文件（电气清单等）——采购员可下载
    attachments: list[dict] = Field(default_factory=list)   # [{id,name}]


class PurchaseItemSummary(BaseModel):
    received_total: float = 0
    uninvoiced: float = 0
    paid_total: float = 0
    outstanding: float = 0
    count: int = 0


class BatchInvoiceIn(BaseModel):
    item_ids: list[int]
    invoice_date: Optional[str] = None
    invoice_amount: Optional[float] = None  # 仅单条时有效


class GroupSummaryIn(BaseModel):
    """🆕 #4 合并父行整单维护（不分摊）：开票金额/已付款作为整单总额记在首行(其余置0,保持汇总合计正确)，
    对账状态套用到所有子行。空字段不改。"""
    item_ids: list[int]
    invoice_amount: Optional[float] = None
    paid_amount: Optional[float] = None
    paid_date: Optional[str] = None
    invoice_status: Optional[str] = None    # 待对账/已对账


class SetInvoiceNoIn(BaseModel):
    """🆕 需求十三：对多个零件统一维护同一开票号；开票金额=各行收货金额。
    #2：invoice_amount=合并开票金额(发票总额)，后端校验==Σ勾选零件收货金额，一致才放行。"""
    item_ids: list[int]
    invoice_no: str = Field(min_length=1, max_length=64)
    invoice_date: Optional[str] = None
    invoice_amount: Optional[float] = None   # 合并开票金额(发票总额)，用于与Σ收货金额比对


# ---------- 期初余额 ----------
class SupplierOpeningBalanceIn(BaseModel):
    balance_date: str
    outstanding_amount: float = 0
    notes: Optional[str] = None


class SupplierOpeningBalanceOut(BaseModel):
    id: int
    supplier_id: int
    balance_date: str
    outstanding_amount: float
    notes: Optional[str] = None


# ---------- 供应商账目一览 ----------
class SupplierStatementRow(BaseModel):
    supplier_id: int
    supplier_name: str
    category: Optional[str] = None
    opening_balance: float = 0
    received_total: float = 0
    invoice_total: float = 0
    paid_total: float = 0
    outstanding: float = 0
    uninvoiced: float = 0
    item_count: int = 0


class SupplierStatementList(BaseModel):
    rows: list[SupplierStatementRow]
    total_opening: float = 0
    total_received: float = 0
    total_paid: float = 0
    total_outstanding: float = 0


# ---------- 请款单 ----------
class PaymentRequestItemIn(BaseModel):
    item_id: int
    allocated_amount: float = 0


class PaymentRequestCreate(BaseModel):
    supplier_id: int
    requested_amount: float
    notes: Optional[str] = None
    items: list[PaymentRequestItemIn] = Field(default_factory=list)


class PaymentRequestOut(BaseModel):
    id: int
    supplier_id: int
    supplier_name: str
    requested_amount: float
    requester_id: Optional[int] = None
    requester_name: Optional[str] = None
    status: str
    notes: Optional[str] = None
    finance_approver_id: Optional[int] = None
    approver_name: Optional[str] = None
    approved_at: Optional[datetime] = None
    paid_amount: Optional[float] = None
    paid_date: Optional[str] = None
    payment_method: Optional[str] = None
    pay_voucher_file_id: Optional[int] = None
    pay_voucher_name: Optional[str] = None
    reject_reason: Optional[str] = None
    # 🆕 需求十六：付款时可见的收款账户信息（供应商）+ 关联采购单号
    supplier_bank_name: Optional[str] = None
    supplier_bank_account: Optional[str] = None
    supplier_tax_no: Optional[str] = None
    po_nos: list[str] = Field(default_factory=list)
    # 🆕 盈利改善2·应付账期利用：最早到期日=min(到货日)+供应商账期天数;距到期天数(负=已逾期)。
    #   供应商未维护 credit_days 或明细未到货时为 None。
    earliest_due: Optional[str] = None
    due_in_days: Optional[int] = None
    created_at: datetime
    items: list[dict] = Field(default_factory=list)


class PaymentRejectIn(BaseModel):
    reason: str = ""


class PaymentPayIn(BaseModel):
    paid_amount: float
    paid_date: str
    payment_method: Optional[str] = None


# ---------- 报表 ----------
class PurchaseKPI(BaseModel):
    month_amount: float = 0
    quarter_amount: float = 0
    year_amount: float = 0
    total_outstanding: float = 0
    pending_requests: int = 0


class PurchaseMonthlyPoint(BaseModel):
    month: str
    amount: float = 0
    paid: float = 0


class PurchaseBuyerRow(BaseModel):
    buyer_id: Optional[int] = None
    buyer_name: str
    amount: float = 0
    count: int = 0


class PurchaseProjectRow(BaseModel):
    project_code: str
    amount: float = 0
    count: int = 0


class PurchaseTopSupplier(BaseModel):
    supplier_id: int
    supplier_name: str
    amount: float = 0
    count: int = 0


# ---------- 🆕 发货清单（设计推送仓库 -> 仓库备货完成 -> 物流可见） ----------
class ShipListPendingRow(BaseModel):
    project_id: int
    code: str
    name: str
    requested_at: Optional[datetime] = None
    requested_by_name: Optional[str] = None
    packlist_status: str = "requested"          # requested 待备货 / ready 已备齐
    ready_at: Optional[datetime] = None
    ready_by_name: Optional[str] = None
    files: list[AttachmentOut] = []            # 设计推送的发货清单文件（仓库只看/下载/打印）


# ==================== 🆕 OA 审批 ====================
class DepartmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    lead_role: Optional[str] = None
    sort_order: int = 0
    enabled: bool = True


class DepartmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    lead_role: Optional[str] = None
    sort_order: int
    enabled: bool


class OaDocTypeIn(BaseModel):
    key: str = Field(min_length=1, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    category: str = Field(pattern="^(business|reimbursement|purchase)$")
    label: str = Field(min_length=1, max_length=64)
    sort_order: int = 0
    enabled: bool = True


class OaDocTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    key: str
    category: str
    label: str
    sort_order: int
    enabled: bool


class OaApprovalStepIn(BaseModel):
    department_id: int
    doc_type: str
    step_order: int
    approver_role: str
    step_label: Optional[str] = None
    enabled: bool = True


class OaApprovalStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    department_id: int
    doc_type: str
    step_order: int
    approver_role: str
    step_label: Optional[str] = None
    enabled: bool


class OaRequestStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    step_order: int
    approver_role: str
    step_label: Optional[str] = None
    status: str
    acted_by: Optional[int] = None
    actor_name: Optional[str] = None
    acted_at: Optional[datetime] = None
    note: Optional[str] = None


class OaCcUserOut(BaseModel):
    id: int
    name: str


class OaRequestCreate(BaseModel):
    category: str
    doc_type: str
    department_id: int
    title: Optional[str] = None
    amount: Optional[float] = None
    detail: dict = Field(default_factory=dict)
    related_request_id: Optional[int] = None
    cc_user_ids: list[int] = Field(default_factory=list)   # 🆕 抄送人（用户id）


class OaRequestOut(BaseModel):
    id: int
    request_no: str
    category: str
    doc_type: str
    department_id: int
    department_name: str
    requester_id: int
    requester_name: str
    title: Optional[str] = None
    amount: Optional[float] = None
    detail: dict = Field(default_factory=dict)
    related_request_id: Optional[int] = None
    related_request_no: Optional[str] = None
    status: str
    current_step_order: Optional[int] = None
    settle_amount: Optional[float] = None
    settle_note: Optional[str] = None
    reject_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    steps: list[OaRequestStepOut] = []
    cc_users: list[OaCcUserOut] = []   # 🆕 抄送人名单
    can_approve: bool = False   # 当前登录人是否能对"当前待处理步骤"操作
    can_withdraw: bool = False  # 当前登录人（提交人）是否能撤回
    can_mark_paid: bool = False  # 🆕 待付款状态下，财务/admin/manager 是否能标记已付款


class OaActionIn(BaseModel):
    note: Optional[str] = None
    settle_amount: Optional[float] = None   # 审批时可选录入核定金额（通常财务环节使用）


class OaRejectIn(BaseModel):
    reason: str = Field(min_length=1)


class OaSummaryRow(BaseModel):
    department_id: int
    department_name: str
    doc_type: str
    count: int = 0
    amount: float = 0


class OaSummaryDetailRow(BaseModel):
    """🆕 #247 汇总报表下钻：某部门+单据类型下，各条已批准申请明细。"""
    id: int
    request_no: str
    requester_name: Optional[str] = None
    title: Optional[str] = None
    amount: float = 0          # 有效金额=核定金额优先，否则申请金额
    settled: bool = False      # 是否用了核定金额
    created_at: datetime
    updated_at: datetime


# ---------- 🆕 管理层待办 ----------
class MgmtTodoCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: Optional[str] = None
    priority: str = "normal"                     # normal / urgent
    due_date: Optional[str] = None               # 🆕 #251 截止日期 YYYY-MM-DD（选填）
    recipient_ids: list[int] = Field(min_length=1)   # 勾选的收件人


class MgmtTodoReplyIn(BaseModel):
    """收件人回复承诺完成时间（可带进展说明）。"""
    committed_at: str = Field(min_length=10, max_length=10)  # YYYY-MM-DD
    progress: Optional[str] = None


class MgmtTodoProgressIn(BaseModel):
    progress: Optional[str] = None


class MgmtTodoExtendIn(BaseModel):
    """收件人申请顺延承诺日（需管理层同意）。"""
    extend_to: str = Field(min_length=10, max_length=10)
    reason: str = Field(min_length=1)


class MgmtTodoExtendDecideIn(BaseModel):
    approve: bool
    note: Optional[str] = None


class MgmtTodoTargetOut(BaseModel):
    id: int
    user_id: int
    user_name: Optional[str] = None
    status: str                                   # pending/committed/done
    committed_at: Optional[str] = None
    progress: Optional[str] = None
    reply_at: Optional[datetime] = None
    done_at: Optional[datetime] = None
    overdue: bool = False                          # 承诺日已过且未完成
    extend_status: Optional[str] = None            # None/pending/approved/rejected
    extend_to: Optional[str] = None
    extend_reason: Optional[str] = None


class MgmtTodoOut(BaseModel):
    """管理层「我发出的 / 监控」视角：一条待办 + 全部收件人处理态。"""
    id: int
    title: str
    content: Optional[str] = None
    priority: str = "normal"
    due_date: Optional[str] = None
    created_by: int
    creator_name: Optional[str] = None
    created_at: datetime
    targets: list[MgmtTodoTargetOut] = []
    # 汇总
    total: int = 0
    done_count: int = 0
    overdue_count: int = 0
    pending_reply_count: int = 0


class MyTodoRow(BaseModel):
    """收件人「我收到的」视角：一行 = 我的一个待办处理态。"""
    target_id: int
    todo_id: int
    title: str
    content: Optional[str] = None
    priority: str = "normal"
    due_date: Optional[str] = None
    creator_name: Optional[str] = None
    created_at: datetime
    status: str
    committed_at: Optional[str] = None
    progress: Optional[str] = None
    done_at: Optional[datetime] = None
    overdue: bool = False
    extend_status: Optional[str] = None
    extend_to: Optional[str] = None
    extend_reason: Optional[str] = None
