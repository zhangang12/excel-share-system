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
    created_at: datetime
    last_login: Optional[datetime] = None


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


class ProjectMemberIn(BaseModel):
    user_id: int
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
