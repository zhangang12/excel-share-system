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
    project_id: Mapped[Optional[int]] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))             # 原始文件名
    ext: Mapped[Optional[str]] = mapped_column(String(16))
    size: Mapped[int] = mapped_column(default=0)
    path: Mapped[str] = mapped_column(String(512))             # 相对 files_dir 的存储路径
    uploaded_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
