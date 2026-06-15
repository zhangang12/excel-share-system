"""ORM 模型 - SQLAlchemy 2.0"""
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Text, ForeignKey, DateTime, UniqueConstraint, Boolean, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON as _BaseJSON
from sqlalchemy.dialects.postgresql import JSONB
from .database import Base


def PortableJSON():
    """PG 用 JSONB，其它（SQLite）用通用 JSON。"""
    return _BaseJSON().with_variant(JSONB(), "postgresql")




class Role(Base):
    __tablename__ = "roles"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[Optional[str]] = mapped_column(String(255))
    # 🆕 消息推送人标记：主管类角色收逾期/预警推送（权限管理页可改）
    can_push: Mapped[bool] = mapped_column(default=False)




class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(64))
    email: Mapped[Optional[str]] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(255))
    password_must_change: Mapped[bool] = mapped_column(default=False)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"))
    is_active: Mapped[bool] = mapped_column(default=True)
    # 🆕 企业微信 userid（手动绑定，F1 口径；空=未绑定，推送降级站内）
    wxid: Mapped[Optional[str]] = mapped_column(String(64))
    # 🆕 v3 M16：导出权限（审批通过后永久放行；管理层天然有）
    can_export: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    role: Mapped["Role"] = relationship(lazy="joined")


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="进行中")
    manager_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    is_deleted: Mapped[bool] = mapped_column(default=False)
    # 项目一览表的自定义列值：{ field_id: value }
    extra: Mapped[Optional[dict]] = mapped_column(
        PortableJSON(),
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    manager: Mapped[Optional["User"]] = relationship(foreign_keys=[manager_id], lazy="joined")


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    permission: Mapped[str] = mapped_column(String(16), default="edit")  # edit / view
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(lazy="joined")


# ---------- 数据表 ----------
class Datasheet(Base):
    """项目下的"工作表"（飞书叫"数据表"，原 Excel 中的 sheet）"""
    __tablename__ = "datasheets"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    sort_order: Mapped[int] = mapped_column(default=0)
    # 表格上方的标题行（导入时保留 Excel 前 N 行作为只读标题）：JSON list[list[str]]
    header_lines: Mapped[Optional[str]] = mapped_column(Text)
    # 🆕 v3 P-16：最近一次 Excel 导入时间（四表"已导入"以此判定，设计完成 D1 校验读它）
    imported_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # 🆕 v3 §十七：装配前置三表完成标记（管理层/生产主管/设计师标记；与 imported_at 独立）
    done_flag: Mapped[bool] = mapped_column(default=False)
    done_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Field(Base):
    """数据表的字段（列）。type 决定单元格类型与渲染方式。"""
    __tablename__ = "fields"
    id: Mapped[int] = mapped_column(primary_key=True)
    datasheet_id: Mapped[int] = mapped_column(
        ForeignKey("datasheets.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    # text / number / date / select / multi_select / person
    type: Mapped[str] = mapped_column(String(32), default="text")
    sort_order: Mapped[int] = mapped_column(default=0)
    config: Mapped[Optional[str]] = mapped_column(Text)  # JSON 字符串：select 选项 / date 格式等
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Record(Base):
    """行。所有 cell 值都用一个 JSONB 列 values 装着：{ field_id: value }"""
    __tablename__ = "records"
    id: Mapped[int] = mapped_column(primary_key=True)
    datasheet_id: Mapped[int] = mapped_column(
        ForeignKey("datasheets.id", ondelete="CASCADE"), index=True
    )
    sort_order: Mapped[int] = mapped_column(default=0)
    # PostgreSQL JSONB；SQLAlchemy 自动序列化
    values: Mapped[Optional[dict]] = mapped_column(
        PortableJSON(),
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    updated_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))



# ---------- 项目一览自定义列定义 ----------
class OverviewField(Base):
    __tablename__ = "overview_fields"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    # text / number / date / select / multi_select / person
    type: Mapped[str] = mapped_column(String(32), default="text")
    sort_order: Mapped[int] = mapped_column(default=0)
    config: Mapped[Optional[str]] = mapped_column(Text)  # JSON 字符串
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------- 字段级权限 ----------
class FieldPermission(Base):
    """项目数据表的字段级权限：(field_id, role_id) -> can_view/can_edit"""
    __tablename__ = "field_permissions"
    __table_args__ = (UniqueConstraint("field_id", "role_id", name="uq_field_role"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("fields.id", ondelete="CASCADE"), index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"))
    can_view: Mapped[bool] = mapped_column(default=True)
    can_edit: Mapped[bool] = mapped_column(default=True)


class OverviewFieldPermission(Base):
    """项目一览自定义列的字段级权限"""
    __tablename__ = "overview_field_permissions"
    __table_args__ = (UniqueConstraint("field_id", "role_id", name="uq_ovw_field_role"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("overview_fields.id", ondelete="CASCADE"), index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"))
    can_view: Mapped[bool] = mapped_column(default=True)
    can_edit: Mapped[bool] = mapped_column(default=True)


# ---------- 🆕 部门任务单（设计/电工/生产 派单→分派→接单→完成 状态机） ----------
class DeptOrder(Base):
    __tablename__ = "dept_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    dept: Mapped[str] = mapped_column(String(16), index=True)   # design / electric / produce
    # pending_assign 待分派 / assigned 待接单 / in_progress 进行中 / done 已完成 / voided 作废
    status: Mapped[str] = mapped_column(String(20), default="pending_assign", index=True)
    worker_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    req_text: Mapped[Optional[str]] = mapped_column(Text)       # 下单要求
    start_date: Mapped[Optional[str]] = mapped_column(String(10))  # YYYY-MM-DD（B5：本人不可改，仅管理层）
    due_date: Mapped[Optional[str]] = mapped_column(String(10))
    done_date: Mapped[Optional[str]] = mapped_column(String(10))
    notify_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    worker: Mapped[Optional["User"]] = relationship(foreign_keys=[worker_id], lazy="joined")
    project: Mapped["Project"] = relationship(lazy="joined")


# ---------- 🆕 销售台账（§十三 19 列；一项目一行；台账为权威、关键日期同步 __o__） ----------
class SalesLedger(Base):
    __tablename__ = "sales_ledger"
    __table_args__ = (UniqueConstraint("project_id", name="uq_ledger_project"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    sales_uid: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    customer: Mapped[Optional[str]] = mapped_column(String(128))     # 客户单位
    cust_type: Mapped[Optional[str]] = mapped_column(String(16))     # 经销商 / 终端客户
    contract: Mapped[str] = mapped_column(String(8), default="无")   # 有 / 无
    contract_file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("attachments.id"))
    amount: Mapped[float] = mapped_column(default=0)                  # 合同金额(元)
    tax_rate: Mapped[Optional[str]] = mapped_column(String(16))      # 13% / "/"(不开票)
    # 开票状态机(M03)：None 未申请 / applying 待主管审批 / pending_invoice 待财务开票 / invoiced 已开票
    invoice_state: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    invoice_apply_file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("attachments.id"))
    invoice_file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("attachments.id"))
    prepay: Mapped[float] = mapped_column(default=0)                  # 预付
    prepay_note: Mapped[Optional[str]] = mapped_column(Text)          # 🆕 预付收款批注(支持插入时间戳)
    before_ship: Mapped[float] = mapped_column(default=0)             # 发货前付
    before_ship_note: Mapped[Optional[str]] = mapped_column(Text)     # 🆕 发货前付收款批注(支持插入时间戳)
    ship_receivable: Mapped[float] = mapped_column(default=0)         # 发货款应收
    balance: Mapped[float] = mapped_column(default=0)                 # 尾款
    balance_date: Mapped[Optional[str]] = mapped_column(String(10))   # 尾款日期
    ship_date: Mapped[Optional[str]] = mapped_column(String(10))      # 发货日期（物流回传只读）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped["Project"] = relationship(lazy="joined")
    sales_user: Mapped[Optional["User"]] = relationship(foreign_keys=[sales_uid], lazy="joined")


# ---------- 🆕 发货单（E1 一项目一单；M02 下单时建待发货行，M08 物流看板消费） ----------
class Shipment(Base):
    __tablename__ = "shipments"
    __table_args__ = (UniqueConstraint("project_id", name="uq_shipment_project"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending 待发货 / shipped 已发货
    receiver_name: Mapped[Optional[str]] = mapped_column(String(128))
    receiver_phone: Mapped[Optional[str]] = mapped_column(String(64))
    receiver_addr: Mapped[Optional[str]] = mapped_column(String(255))
    ship_doc_file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("attachments.id"))
    shipped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    shipped_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped["Project"] = relationship(lazy="joined")


# ---------- 🆕 统一附件（合同/图纸包/产物/发货单/物料清单等全系统复用） ----------
class Attachment(Base):
    """所有业务文件的统一存储索引。文件本体落 data/files/，按 ID 关联与撤回
    （不按文件名匹配——同名文件不会误删）。biz_type+biz_id 标记归属业务对象。"""
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    # contract / invoice_apply / invoice / order_input / order_start_output /
    # order_output / ship_doc / ship_list / aftersales_mat / purchase_list ...
    biz_type: Mapped[str] = mapped_column(String(32), index=True)
    biz_id: Mapped[Optional[int]] = mapped_column(index=True)  # 业务对象 id（如 order_id / ledger_id）
    kind: Mapped[Optional[str]] = mapped_column(String(32))    # 业务内细分（如产物 circuit/manual/sheetpkg）
    project_id: Mapped[Optional[int]] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))             # 原始文件名
    ext: Mapped[Optional[str]] = mapped_column(String(16))
    size: Mapped[int] = mapped_column(default=0)
    path: Mapped[str] = mapped_column(String(512))             # 相对 files_dir 的存储路径
    uploaded_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------- 🆕 仓库组：物料主数据 + 出入库流水（实时库存=期初+Σ入−Σ出） ----------
class WhMaterial(Base):
    __tablename__ = "wh_materials"
    __table_args__ = (UniqueConstraint("name", "spec", name="uq_wh_material_name_spec"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[Optional[str]] = mapped_column(String(64), index=True)   # 物料编码（可空，留扩展）
    name: Mapped[str] = mapped_column(String(128), index=True)
    spec: Mapped[Optional[str]] = mapped_column(String(128))              # 规格型号
    category: Mapped[Optional[str]] = mapped_column(String(64))          # 类别（搅拌桨/标准件…）
    unit: Mapped[str] = mapped_column(String(16), default="个")
    location: Mapped[Optional[str]] = mapped_column(String(64))          # 库位（单仓仅文本）
    safety_stock: Mapped[float] = mapped_column(default=0)               # 安全库存（低于预警）
    init_stock: Mapped[float] = mapped_column(default=0)                 # 期初库存
    status: Mapped[str] = mapped_column(String(16), default="正常")       # 正常/停用
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WhTxn(Base):
    __tablename__ = "wh_txns"
    id: Mapped[int] = mapped_column(primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("wh_materials.id"), index=True)
    biz_date: Mapped[str] = mapped_column(String(10), index=True)        # YYYY-MM-DD
    direction: Mapped[str] = mapped_column(String(4))                    # in / out
    qty: Mapped[float] = mapped_column(default=0)                        # 正数
    source: Mapped[Optional[str]] = mapped_column(String(32))           # 采购入库/领料出库/冲红…
    party: Mapped[Optional[str]] = mapped_column(String(128))           # 供应商/领用方
    project_id: Mapped[Optional[int]] = mapped_column(ForeignKey("projects.id"), index=True)
    ref_no: Mapped[str] = mapped_column(String(32), index=True)         # 单据号 RK/CKyyyymmdd-NNN
    operator_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    is_reversal: Mapped[bool] = mapped_column(default=False)            # 是否冲红单
    reversal_of: Mapped[Optional[int]] = mapped_column(ForeignKey("wh_txns.id"))  # 冲红指向的原单
    reversed: Mapped[bool] = mapped_column(default=False)              # 原单是否已被冲红
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    material: Mapped["WhMaterial"] = relationship(lazy="joined")


# ---------- 🆕 售后部（登记→审批→同步财务；§十五） ----------
class AfterSales(Base):
    __tablename__ = "aftersales"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    problem: Mapped[str] = mapped_column(Text)
    cost: Mapped[float] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/approved/rejected
    mat_file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("attachments.id"))  # 售后物料清单（登记必传）
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    appr_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    appr_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[Optional[str]] = mapped_column(Text)  # 🆕 #98 售后驳回原因

    project: Mapped["Project"] = relationship(lazy="joined")


# ---------- 🆕 生产问题反馈流（装配组→生产主管→设计师；§十四） ----------
class Feedback(Base):
    __tablename__ = "feedbacks"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    # pending_pm 待主管审批 / pending_design 待设计接收 / archived 已存档 /
    # rejected_by_pm 主管驳回 / rejected_by_design 设计驳回
    status: Mapped[str] = mapped_column(String(20), default="pending_pm", index=True)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    designer_uid: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))  # 反馈指向的设计师
    appr_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    project: Mapped["Project"] = relationship(lazy="joined")
    creator: Mapped[Optional["User"]] = relationship(foreign_keys=[created_by], lazy="joined")
    designer: Mapped[Optional["User"]] = relationship(foreign_keys=[designer_uid], lazy="joined")


# ---------- 🆕 站内消息（角色池路由；企微推送降级兜底） ----------
class Message(Base):
    """站内消息。to_role 推送在写入时按角色扇出成每用户一行（已读状态天然按人）。"""
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="info")  # wx / warn / info
    text: Mapped[str] = mapped_column(Text)
    read: Mapped[bool] = mapped_column(default=False, index=True)
    biz_type: Mapped[Optional[str]] = mapped_column(String(32))
    biz_id: Mapped[Optional[int]]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# ---------- 🆕 导出审批（M16；可逆开关 settings.export_approval_enabled 控制是否生效） ----------
class ExportRequest(Base):
    __tablename__ = "export_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    scope: Mapped[str] = mapped_column(String(64))                 # 导出范围描述
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/approved/rejected
    appr_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(foreign_keys=[user_id], lazy="joined")


# ---------- 操作审计 ----------
class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)  # login/create/update/delete/import...
    target_type: Mapped[Optional[str]] = mapped_column(String(32))  # project/user/datasheet/field/...
    target_id: Mapped[Optional[int]]
    detail: Mapped[Optional[str]] = mapped_column(Text)
    ip: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# ---------- 🆕 用户反馈小助手（任意角色可提交，管理层汇总+导出） ----------
class UserFeedback(Base):
    __tablename__ = "user_feedback"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="bug", index=True)  # bug/suggest/other
    content: Mapped[str] = mapped_column(Text)
    page_url: Mapped[Optional[str]] = mapped_column(String(255))   # 用户当时所在的路径,便于复现
    user_agent: Mapped[Optional[str]] = mapped_column(String(255))
    shot_file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("attachments.id"))
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open/done
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    user: Mapped[Optional["User"]] = relationship(lazy="joined")
    shot: Mapped[Optional["Attachment"]] = relationship(lazy="joined")
