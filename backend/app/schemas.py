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
    role_id: int
    is_active: bool = True



class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    role_id: Optional[int] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=6, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    role_id: int
    role_code: Optional[str] = None
    role_name: Optional[str] = None
    is_active: bool
    password_must_change: bool = False
    wxid: Optional[str] = None  # 🆕 v3 企微绑定
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
    input_files: list[AttachmentOut] = []
    start_files: list[AttachmentOut] = []
    output_files: list[AttachmentOut] = []


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
    invoice_apply_file_id: Optional[int] = None
    invoice_apply_file_name: Optional[str] = None
    invoice_file_id: Optional[int] = None
    invoice_file_name: Optional[str] = None
    prepay: float = 0
    before_ship: float = 0
    ship_receivable: float = 0
    balance: float = 0
    balance_date: Optional[str] = None
    ship_date: Optional[str] = None


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


class SalesReceiverIn(BaseModel):
    name: str = ""
    phone: str = ""
    addr: str = ""


class SalesOrderCreate(BaseModel):
    code_suffix: str = ""                 # 编号后缀字母（可选，如 A）
    name: str = Field(min_length=1, max_length=255)   # 设备名称
    customer: str = ""
    cust_type: str = "经销商"
    contract: str = "有"
    amount: float = 0
    tax_rate: str = "13%"
    prepay: float = 0
    before_ship: float = 0
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


class SalesLedgerUpdate(BaseModel):
    name: Optional[str] = None            # 设备名称（同步 Project.name）
    customer: Optional[str] = None
    cust_type: Optional[str] = None
    contract: Optional[str] = None
    amount: Optional[float] = None
    tax_rate: Optional[str] = None
    prepay: Optional[float] = None
    before_ship: Optional[float] = None
    ship_receivable: Optional[float] = None
    balance: Optional[float] = None
    balance_date: Optional[str] = None


class NextCodeOut(BaseModel):
    code: str


class OrderOptionsOut(BaseModel):
    workers: list[OrderOptionUser]
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
