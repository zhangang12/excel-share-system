"""全局错误处理 - 把所有错误消息翻译成中文"""
from typing import Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
import logging

log = logging.getLogger("errors")


# 字段名（API 字段 → 中文显示名）
FIELD_NAMES: dict[str, str] = {
    "username": "用户名",
    "password": "密码",
    "full_name": "姓名",
    "email": "邮箱",
    "role_id": "角色",
    "department_id": "部门",
    "is_active": "状态",
    "name": "名称",
    "description": "说明",
    "old_password": "原密码",
    "new_password": "新密码",
    "code": "编号",
    "status": "状态",
    "manager_id": "项目经理",
    "permission": "权限",
    "user_id": "用户",
    "project_id": "项目",
    "field_id": "字段",
    "datasheet_id": "数据表",
    "value": "值",
}


def _translate_field(loc: tuple[Any, ...] | list[Any]) -> str:
    parts = [p for p in loc if p != "body" and p != "query" and p != "path"]
    return ".".join(FIELD_NAMES.get(str(p), str(p)) for p in parts)


def _translate_validation_error(err: dict[str, Any]) -> str:
    t = err.get("type", "")
    msg = err.get("msg", "")
    ctx = err.get("ctx") or {}
    field = _translate_field(err.get("loc", ()))

    if t == "missing":
        return f"{field} 必填"
    if t == "string_too_short":
        return f"{field} 至少 {ctx.get('min_length', '')} 个字符"
    if t == "string_too_long":
        return f"{field} 最多 {ctx.get('max_length', '')} 个字符"
    if t in ("int_parsing", "int_type", "int_from_float"):
        return f"{field} 必须是整数"
    if t in ("float_parsing", "float_type"):
        return f"{field} 必须是数字"
    if t in ("bool_parsing", "bool_type"):
        return f"{field} 必须是布尔值"
    if t in ("string_type",):
        return f"{field} 必须是字符串"
    if t in ("list_type",):
        return f"{field} 必须是数组"
    if t in ("dict_type",):
        return f"{field} 必须是对象"
    if t == "value_error":
        return f"{field}：{msg}"
    if t.startswith("string_pattern"):
        return f"{field} 格式不正确"
    if t == "greater_than":
        return f"{field} 必须大于 {ctx.get('gt', '')}"
    if t == "greater_than_equal":
        return f"{field} 必须大于等于 {ctx.get('ge', '')}"
    if t == "less_than":
        return f"{field} 必须小于 {ctx.get('lt', '')}"
    if t == "less_than_equal":
        return f"{field} 必须小于等于 {ctx.get('le', '')}"
    if t == "enum":
        expected = ctx.get("expected", "")
        return f"{field} 取值必须是 {expected}"
    if t.startswith("json"):
        return f"{field} 不是合法 JSON"
    if t == "url_parsing":
        return f"{field} 不是合法 URL"
    if t == "url_scheme":
        return f"{field} URL 协议不正确"
    if t == "uuid_type" or t.startswith("uuid"):
        return f"{field} 必须是 UUID"
    if t == "datetime_parsing" or t == "datetime_type":
        return f"{field} 必须是合法日期时间"
    if t == "date_parsing" or t == "date_type":
        return f"{field} 必须是合法日期"
    # 兜底
    return f"{field}：{msg}" if field else (msg or "校验失败")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        messages = [_translate_validation_error(e) for e in errors]
        return JSONResponse(
            status_code=422,
            content={"detail": "；".join(m for m in messages if m)},
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(request: Request, exc: IntegrityError):
        orig = str(getattr(exc, "orig", "")).lower()
        log.warning("IntegrityError: %s", orig)
        if "unique" in orig or "duplicate" in orig:
            return JSONResponse(content={"detail": "数据已存在，不能重复"}, status_code=409)
        if "foreign key" in orig:
            return JSONResponse(content={"detail": "关联的数据不存在或被引用，无法操作"}, status_code=400)
        if "not null" in orig:
            return JSONResponse(content={"detail": "必填字段不能为空"}, status_code=400)
        return JSONResponse(content={"detail": "数据库错误"}, status_code=500)

    @app.exception_handler(Exception)
    async def _general(request: Request, exc: Exception):
        # 让 HTTPException 走默认通道
        if isinstance(exc, HTTPException):
            raise exc
        log.exception("未捕获异常: %s", exc)
        return JSONResponse(content={"detail": "服务器内部错误，请联系管理员"}, status_code=500)
