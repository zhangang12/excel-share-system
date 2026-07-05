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


# 🆕 用户↔角色 多对多关联（一个用户可配置多个角色，权限取并集）。
# 用户的角色集为「平等多角色」：无主次之分；User.role_id 仅作为存量兼容锚点
# （恒等于其角色之一），新逻辑一律读 User.role_codes / role_ids / has_role()。
class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_role"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), index=True)


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

    # 锚点角色（存量兼容/展示无关）：恒等于 roles 中的一个
    role: Mapped["Role"] = relationship(lazy="joined")
    # 🆕 全部角色（平等，无主次）；selectin 预加载供 deps/menus 同步读取。
    # viewonly：只读关系，写入统一走 user_roles 关联表（Core）——避免 async 下
    # 赋值触发同步 lazy-load(MissingGreenlet) 及与 ORM 二次同步冲突。
    roles: Mapped[list["Role"]] = relationship(
        secondary="user_roles", lazy="selectin", order_by="Role.id", viewonly=True,
    )

    # ---- 多角色辅助（全系统权限判断统一走这三个，取并集语义）----
    @property
    def role_codes(self) -> set[str]:
        """用户全部角色 code 的集合（含锚点 role，去重）。"""
        codes = {r.code for r in self.roles} if self.roles else set()
        if self.role and self.role.code:
            codes.add(self.role.code)
        return codes

    @property
    def role_ids(self) -> list[int]:
        """用户全部角色 id（含锚点 role_id，去重；字段权限按此 IN 查询）。"""
        ids = [r.id for r in self.roles] if self.roles else []
        if self.role_id and self.role_id not in ids:
            ids.append(self.role_id)
        return ids

    def has_role(self, *codes: str) -> bool:
        """用户是否拥有 codes 中的任一角色（并集判断）。"""
        return bool(self.role_codes & set(codes))


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
    design_done_flag:   Mapped[bool] = mapped_column(Boolean, default=False)  # 🆕 设计完成第一步
    electric_done_flag: Mapped[bool] = mapped_column(Boolean, default=False)  # 🆕 接线完成第一步
    ship_prep_done:     Mapped[bool] = mapped_column(Boolean, default=False)  # 🆕 #5 设计部发货准备(说明书/铭牌)完成
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    worker: Mapped[Optional["User"]] = relationship(foreign_keys=[worker_id], lazy="joined")
    project: Mapped["Project"] = relationship(lazy="joined")


# ---------- 🆕 2026-06-19 生产部分组派发（钣金组/装配组）：取代生产单的单人分派 ----------
class ProduceGroupTask(Base):
    """生产部主管把销售下单的生产任务（dept_orders.dept==produce）派发给「钣金组」「装配组」
    两个组，每组一行；两组都标记完成 → 父生产任务单 status=done（驱动发货闸门 D5/部门报表）。
    group: sheetmetal 钣金组 / assembly 装配组。"""
    __tablename__ = "produce_group_tasks"
    __table_args__ = (UniqueConstraint("order_id", "group", name="uq_produce_group"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("dept_orders.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    group: Mapped[str] = mapped_column(String(16), index=True)        # sheetmetal / assembly
    status: Mapped[str] = mapped_column(String(16), default="dispatched")  # dispatched / done
    worker_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)  # 派给本组的具体人
    due_date: Mapped[Optional[str]] = mapped_column(String(10))   # 🆕 本组预计完成(钣金/装配各填一个；报表按此算效率/逾期；填后锁定，仅管理层可改)
    dispatched_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    done_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    done_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

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
    # 🆕 合并开票批次号：同一客户多个项目合并开票时共享同一 batch_id；None=单项目开票
    invoice_batch_id: Mapped[Optional[int]] = mapped_column(index=True)
    # 🆕 订单作废流(2026-06-18)：None 正常 / applying 待销售负责人审批 / voided 已作废(项目软删)
    void_state: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    void_reason: Mapped[Optional[str]] = mapped_column(Text)
    # 🆕 下单审批流(2026-06-18)：None 已生效/历史 / pending 待主管审批 / draft 被退回可改
    # （仅销售员下单需审批；销售主管/管理层下单直接 None 生效。待审批/草稿期间不建各部门任务/发货单）
    order_state: Mapped[Optional[str]] = mapped_column(String(20), index=True)
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
    order_type: Mapped[Optional[str]] = mapped_column(String(16))    # 🆕 调货订单 / 工厂制作订单
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
    receiver_company: Mapped[Optional[str]] = mapped_column(String(128))  # 🆕 收货单位（与收货人分开）
    receiver_phone: Mapped[Optional[str]] = mapped_column(String(64))
    receiver_addr: Mapped[Optional[str]] = mapped_column(String(255))
    ship_doc_file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("attachments.id"))
    shipped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    shipped_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    # 🆕 发货清单：设计部推送仓库准备 -> 仓库备货完成 -> 物流可见（none/requested/ready）
    packlist_status: Mapped[str] = mapped_column(String(16), default="none")
    packlist_requested_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    packlist_requested_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    packlist_ready_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    packlist_ready_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
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
    material_grade: Mapped[Optional[str]] = mapped_column(String(32))    # 🆕 材质（304不锈钢/碳钢/铝合金…，字典管理）
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
    unit_price: Mapped[Optional[float]] = mapped_column()               # 🆕 单价（采购入库带采购单价）
    amount: Mapped[Optional[float]] = mapped_column()                   # 🆕 金额=qty×unit_price（库存金额/成本）
    purchase_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("purchase_items.id"), index=True)  # 🆕 采购收货自动入库来源
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
    # 🆕 系统回信：管理层处理意见回复
    reply: Mapped[Optional[str]] = mapped_column(Text)
    replied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    replied_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    reply_read: Mapped[bool] = mapped_column(default=False)  # 提出人是否已读回复
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    user: Mapped[Optional["User"]] = relationship(lazy="joined", foreign_keys=[user_id])
    replier: Mapped[Optional["User"]] = relationship(lazy="joined", foreign_keys=[replied_by])
    shot: Mapped[Optional["Attachment"]] = relationship(lazy="joined")


class SalesLead(Base):
    """🆕 销售线索池：主管/管理层集中录入网络询盘 → 分配给销售员 → 跟进/补全/改状态 → 成交率报表。
    行级隔离：销售员仅见分配给自己(owner_uid)的线索；主管/管理层全量。
    状态：潜在需求(默认)/报价/成交/丢单；成交率 = 成交数 ÷ 线索总数。"""
    __tablename__ = "sales_leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))                  # 询盘来源(固定下拉, 报表维度)
    customer: Mapped[Optional[str]] = mapped_column(String(128))     # 客户名称(可后补)
    contact: Mapped[Optional[str]] = mapped_column(String(64))       # 联系人(可后补)
    phone: Mapped[Optional[str]] = mapped_column(String(64))         # 联系电话(可后补)
    wechat: Mapped[Optional[str]] = mapped_column(String(64))        # 微信号(可后补)
    requirement: Mapped[Optional[str]] = mapped_column(Text)         # 设备需求
    owner_uid: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)  # 跟进销售(分配)
    status: Mapped[str] = mapped_column(String(16), default="潜在需求", index=True)  # 潜在需求/报价/成交/丢单
    follow_log: Mapped[Optional[str]] = mapped_column(Text)          # 跟进记录(可插入时间戳, 自由编辑)
    lost_reason: Mapped[Optional[str]] = mapped_column(String(255))  # 丢单原因(选填)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))  # 录入人
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # 分配时间
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))    # 成交/丢单时间(报表用)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped[Optional["User"]] = relationship(lazy="joined", foreign_keys=[owner_uid])


class RevisionRequest(Base):
    """🆕 #1 技术资料修订意见：设计/电工对销售下发的「合同技术资料」提出修订意见 →
    推送对应销售员，销售用「更换技术资料」上传修正版后自动标记 resolved 并回通知提出人。"""
    __tablename__ = "revision_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("dept_orders.id"))
    dept: Mapped[Optional[str]] = mapped_column(String(16))     # 提出部门 design/electric
    reason: Mapped[str] = mapped_column(Text)                   # 修订意见内容
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open/resolved
    raised_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    resolved_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    raiser: Mapped[Optional["User"]] = relationship(lazy="joined", foreign_keys=[raised_by])


# ==================== 🆕 采购管理模块 ====================

class Supplier(Base):
    """供应商档案"""
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    code: Mapped[Optional[str]] = mapped_column(String(32), unique=True, index=True)
    category: Mapped[Optional[str]] = mapped_column(String(32))       # 外协/标准件/不锈钢/激光/电气/运输
    contact: Mapped[Optional[str]] = mapped_column(String(64))
    phone: Mapped[Optional[str]] = mapped_column(String(64))
    address: Mapped[Optional[str]] = mapped_column(String(255))
    tax_no: Mapped[Optional[str]] = mapped_column(String(64))
    bank_name: Mapped[Optional[str]] = mapped_column(String(128))
    bank_account: Mapped[Optional[str]] = mapped_column(String(64))
    settlement_type: Mapped[Optional[str]] = mapped_column(String(16))  # 现金/月结/无账期
    credit_days: Mapped[Optional[int]] = mapped_column()
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PurchaseItem(Base):
    """采购明细（唯一录入入口）"""
    __tablename__ = "purchase_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_no: Mapped[Optional[str]] = mapped_column(String(32), index=True)  # 🆕 采购单号（同一供应商多零件行共享）
    # 🆕 来源：从项目清单（标准件清单等）生成采购单时，记来源数据表与行，用于回写进度
    source_sheet_id: Mapped[Optional[int]] = mapped_column(index=True)
    source_record_id: Mapped[Optional[int]] = mapped_column(index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    delivery_date: Mapped[Optional[str]] = mapped_column(String(10))       # 下单日期（采购填）
    contract_no: Mapped[Optional[str]] = mapped_column(String(64))
    project_code: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    delivery_note_no: Mapped[Optional[str]] = mapped_column(String(64))    # 送货单号（仓库收货填）
    arrival_date: Mapped[Optional[str]] = mapped_column(String(10))        # 🆕 到货日期（仓库收货填）
    item_name: Mapped[str] = mapped_column(String(128))
    spec: Mapped[Optional[str]] = mapped_column(String(255))
    brand: Mapped[Optional[str]] = mapped_column(String(64))  # 🆕 品牌（下单时逐行选/填，自由输入）
    qty: Mapped[Optional[float]] = mapped_column()
    unit_price: Mapped[Optional[float]] = mapped_column()
    received_amount: Mapped[float] = mapped_column(default=0)
    invoice_date: Mapped[Optional[str]] = mapped_column(String(10))
    tax_rate: Mapped[Optional[str]] = mapped_column(String(16))
    invoice_amount: Mapped[float] = mapped_column(default=0)
    paid_amount: Mapped[float] = mapped_column(default=0)
    paid_date: Mapped[Optional[str]] = mapped_column(String(10))
    payment_method: Mapped[Optional[str]] = mapped_column(String(16))  # 🆕 付款方式：现金全款/对公全款/账期/现金预付/对公预付
    prepay_ratio: Mapped[Optional[float]] = mapped_column()  # 🆕 预付比例(%)，仅现金预付/对公预付时有意义
    invoice_status: Mapped[str] = mapped_column(String(16), default="待对账", index=True)
    buyer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    # 🆕 R6：自定义字段值 {str(field_id): value}；存量行为空 dict，不受新增字段影响
    custom_values: Mapped[Optional[dict]] = mapped_column(PortableJSON(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    supplier: Mapped["Supplier"] = relationship(lazy="joined")
    buyer: Mapped[Optional["User"]] = relationship(foreign_keys=[buyer_id], lazy="joined")


class PurchaseCustomField(Base):
    """🆕 R6：采购单/采购明细的可配置自定义字段定义（值存 PurchaseItem.custom_values）。"""
    __tablename__ = "purchase_custom_fields"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(64))                 # 显示名称
    ftype: Mapped[str] = mapped_column(String(16), default="text")  # text/number/date/select
    options: Mapped[Optional[dict]] = mapped_column(PortableJSON(), default=list)  # select 选项 list[str]
    required: Mapped[bool] = mapped_column(default=False)           # 是否必填
    show_in_list: Mapped[bool] = mapped_column(default=True)        # 列表是否显示
    sort_order: Mapped[int] = mapped_column(default=0)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MaterialDict(Base):
    """🆕 物料字典：受管理的「类别 / 单位 / 材质 / 供应商分类」取值表（替代原来的预置∪自由输入）。"""
    __tablename__ = "material_dict"
    id: Mapped[int] = mapped_column(primary_key=True)
    # 🆕 原为 String(16)："supplier_category"(17字符) 超长，Postgres 下插入直接报错(SQLite不检查长度，
    # 沙箱测试没发现)——生产那边"供应商分类"字典一直是空的、点新增就500，根因就是这个。
    dtype: Mapped[str] = mapped_column(String(32), index=True)     # category/unit/material_grade/supplier_category
    value: Mapped[str] = mapped_column(String(64))                 # 取值
    sort_order: Mapped[int] = mapped_column(default=0)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SupplierOpeningBalance(Base):
    """供应商期初余额（每家一条）"""
    __tablename__ = "supplier_opening_balances"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), unique=True, index=True)
    balance_date: Mapped[str] = mapped_column(String(10))
    outstanding_amount: Mapped[float] = mapped_column(default=0)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    supplier: Mapped["Supplier"] = relationship(lazy="joined")


class PaymentRequest(Base):
    """请款单"""
    __tablename__ = "payment_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    requested_amount: Mapped[float] = mapped_column(default=0)
    requester_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    finance_approver_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    paid_amount: Mapped[Optional[float]] = mapped_column()
    paid_date: Mapped[Optional[str]] = mapped_column(String(10))
    payment_method: Mapped[Optional[str]] = mapped_column(String(32))
    pay_voucher_file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("attachments.id"))  # 🆕 付款凭证
    reject_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    supplier: Mapped["Supplier"] = relationship(lazy="joined")
    requester: Mapped[Optional["User"]] = relationship(foreign_keys=[requester_id], lazy="joined")
    finance_approver: Mapped[Optional["User"]] = relationship(foreign_keys=[finance_approver_id], lazy="joined")


class PaymentRequestItem(Base):
    """请款单↔采购明细 关联表"""
    __tablename__ = "payment_request_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("payment_requests.id", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_items.id", ondelete="CASCADE"), index=True
    )
    allocated_amount: Mapped[float] = mapped_column(default=0)


# ==================== 🆕 OA 审批 ====================
class Department(Base):
    """OA 部门字典（与角色分组独立、手动维护）。lead_role 设置后，持有该角色的人
    可查看本部门全部 OA 申请（部门负责人视角），不设则无该项额外可见性。"""
    __tablename__ = "departments"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    lead_role: Mapped[Optional[str]] = mapped_column(String(32))
    sort_order: Mapped[int] = mapped_column(default=0)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OaDocTypeDict(Base):
    """OA 单据类型字典（业务/报销/采购三大类下的具体单据类型，管理层可增删改）。
    key 一旦创建不可改（OaRequest/OaApprovalStep 按 key 字符串引用）；可改 label/分类/排序/启用。"""
    __tablename__ = "oa_doc_types"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True)
    category: Mapped[str] = mapped_column(String(16))   # business/reimbursement/purchase
    label: Mapped[str] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(default=0)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OaApprovalStep(Base):
    """审批链配置（管理层维护）：某部门+某单据类型 的第几步由哪个角色审批。
    提交申请时按此快照生成 OaRequestStep，之后改配置不影响在途申请。"""
    __tablename__ = "oa_approval_steps"
    __table_args__ = (UniqueConstraint("department_id", "doc_type", "step_order", name="uq_oa_step"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id", ondelete="CASCADE"), index=True)
    doc_type: Mapped[str] = mapped_column(String(24), index=True)
    step_order: Mapped[int] = mapped_column()
    approver_role: Mapped[str] = mapped_column(String(32))
    step_label: Mapped[Optional[str]] = mapped_column(String(32))  # 展示名，如"部门主管审批"；空则显示角色名
    enabled: Mapped[bool] = mapped_column(default=True)

    department: Mapped["Department"] = relationship(lazy="joined")


class OaRequest(Base):
    """OA 申请单（业务申请/报销申请/采购申请，共用一张表，type-specific 字段落 detail JSON）。"""
    __tablename__ = "oa_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_no: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(16), index=True)   # business/reimbursement/purchase
    doc_type: Mapped[str] = mapped_column(String(24), index=True)   # trip/hospitality/company_car/...
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), index=True)
    requester_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[Optional[str]] = mapped_column(String(128))
    amount: Mapped[Optional[float]] = mapped_column()
    detail: Mapped[Optional[dict]] = mapped_column(PortableJSON(), default=dict)  # 分类型的表单字段
    related_request_id: Mapped[Optional[int]] = mapped_column(ForeignKey("oa_requests.id"))  # 报销↔业务申请
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/approved/rejected/withdrawn
    current_step_order: Mapped[Optional[int]] = mapped_column()
    settle_amount: Mapped[Optional[float]] = mapped_column()   # 财务等环节核定的实际金额（可与申请金额不同）
    settle_note: Mapped[Optional[str]] = mapped_column(Text)
    reject_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    department: Mapped["Department"] = relationship(lazy="joined")
    requester: Mapped["User"] = relationship(foreign_keys=[requester_id], lazy="joined")
    related_request: Mapped[Optional["OaRequest"]] = relationship(remote_side=[id])
    steps: Mapped[list["OaRequestStep"]] = relationship(
        back_populates="request", order_by="OaRequestStep.step_order",
        cascade="all, delete-orphan",
    )


class OaRequestStep(Base):
    """某申请单的审批步骤快照+实际操作记录（与 OaApprovalStep 配置解耦，改配置不影响在途单）。"""
    __tablename__ = "oa_request_steps"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("oa_requests.id", ondelete="CASCADE"), index=True)
    step_order: Mapped[int] = mapped_column()
    approver_role: Mapped[str] = mapped_column(String(32))
    step_label: Mapped[Optional[str]] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/approved/rejected/skipped
    acted_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    acted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    note: Mapped[Optional[str]] = mapped_column(Text)

    request: Mapped["OaRequest"] = relationship(back_populates="steps")
    actor: Mapped[Optional["User"]] = relationship(lazy="joined")
