"""🆕 外网登录两步闸门：浏览器 + 外网 IP + 非 admin 登录时，需再输 6 位随机码才发 token。
码经 push_message 双通道（站内 + 企微）发给 manager 角色池，由管理层核实后告知本人；
免闸：桌面客户端（X-PMS-Client 头；device_gate 打开后还要 X-PMS-Device 在名单里）/
admin 角色 / 内网 IP / 开关关闭（gate_enabled=0）。
配置存 app_settings：gate_enabled 默认 "1" 开、device_gate_enabled 默认 "0" 关、
intranet_cidrs / device_ids 为 JSON 数组默认 []。"""
import hashlib
import ipaddress
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import models
from .notify import push_message

CODE_TTL_MIN = 10          # 验证码有效期（分钟）
_RATE_PER_MIN = 1          # 同账号发码限频：每分钟（防误点刷屏；🆕 2026-07-28 应要求去掉每日上限与错码锁定）

_CFG_ENABLED = "gate_enabled"
_CFG_CIDRS = "intranet_cidrs"
_CFG_DEVICE_GATE = "device_gate_enabled"
_CFG_DEVICE_IDS = "device_ids"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    """SQLite 读回的是 naive 时间，统一按 UTC 补齐时区再比较。"""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ip_in(ip: str, cidrs: list[str]) -> bool:
    """IP 是否命中名单：单 IP 按 /32、CIDR 按网段，非法条目跳过。纯名单匹配，不含私网自动放行。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in cidrs or []:
        try:
            net = ipaddress.ip_network(str(item).strip(), strict=False)
        except ValueError:
            continue
        if addr in net:
            return True
    return False


def is_intranet(ip: str, cidrs: list[str]) -> bool:
    """客户端 IP 是否按内网处理：命中名单，或本身就是回环/私网地址
    （127/8、10/8、172.16/12、192.168/16、::1、fc00::/7）——这些段不可公网路由，
    天然不可能是外网来源；名单用于覆盖办公网公网出口等场景。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_private:
        return True
    return _ip_in(ip, cidrs)


def desktop_exempt(is_desktop: bool, device_id: str, *,
                   device_gate: bool, device_ids: list[str]) -> bool:
    """桌面客户端是否免闸。

    device_gate 关（默认）→ 装了客户端就免闸，与加此功能之前完全一致。
    device_gate 开        → 还要 X-PMS-Device 落在名单里，否则照样走验证码。

    只认 X-PMS-Client 头是**可伪造**的（curl 加个头就绕过整道闸门）；加上设备名单后，
    伪造者还得先知道某台在册机器的 UUID。

    注意：开关打开而名单为空 = 所有桌面端都要验证码。这是刻意的字面语义——
    开关本身就是安全阀（默认关），管理员主动打开即表示他清楚自己在做什么；
    前端保存时会二次确认。admin 恒免闸，不会锁死没救。"""
    if not is_desktop:
        return False
    if not device_gate:
        return True
    return bool(device_id) and device_id in set(device_ids)


def _json_list(raw: str) -> list[str]:
    """存的是 JSON 数组；解析失败或不是数组，一律当空名单，绝不因脏数据把人挡在外面。"""
    try:
        val = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [str(c) for c in val] if isinstance(val, list) else []


async def get_gate_config(db: AsyncSession) -> dict:
    """生效配置 = app_settings；gate_enabled 默认开，两个名单默认空（每次请求实时读库）。"""
    r = await db.execute(select(models.AppSetting).where(models.AppSetting.key.in_(
        [_CFG_ENABLED, _CFG_CIDRS, _CFG_DEVICE_GATE, _CFG_DEVICE_IDS])))
    stored = {row.key: (row.value or "") for row in r.scalars().all()}
    return {
        "enabled": stored.get(_CFG_ENABLED, "1").strip() != "0",
        "cidrs": _json_list(stored.get(_CFG_CIDRS)),
        # 设备闸默认**关**：不填这个键的存量环境行为完全不变
        "device_gate": stored.get(_CFG_DEVICE_GATE, "0").strip() == "1",
        "device_ids": _json_list(stored.get(_CFG_DEVICE_IDS)),
    }


async def set_gate_config(db: AsyncSession, *, enabled: bool, cidrs: list[str],
                          device_gate: bool = False,
                          device_ids: list[str] | None = None) -> None:
    """写 app_settings（upsert），保存即全局生效。"""
    writes = {
        _CFG_ENABLED: "1" if enabled else "0",
        _CFG_CIDRS: json.dumps(list(cidrs), ensure_ascii=False),
        _CFG_DEVICE_GATE: "1" if device_gate else "0",
        _CFG_DEVICE_IDS: json.dumps(list(device_ids or []), ensure_ascii=False),
    }
    for key, value in writes.items():
        row = await db.get(models.AppSetting, key)
        if row:
            row.value = value
        else:
            db.add(models.AppSetting(key=key, value=value))
    await db.commit()


async def issue_code(db: AsyncSession, user: models.User) -> str:
    """发码：限频（同账号 1 条/分，防误点刷屏；🆕 2026-07-28 应要求去掉每日上限）→
    作废旧未用码 → 生成 6 位码（库中只存 sha256 哈希）+ pre_token → push_message 通知管理层。返回 pre_token。"""
    now = _now()
    cnt_min = (await db.execute(select(func.count(models.LoginGateCode.id)).where(
        models.LoginGateCode.user_id == user.id,
        models.LoginGateCode.created_at >= now - timedelta(minutes=1)))).scalar_one()
    if cnt_min >= _RATE_PER_MIN:
        raise HTTPException(429, "验证码发送过于频繁，请 1 分钟后再试")

    # 作废旧未用码（同一账号同时只有一个有效码）
    await db.execute(update(models.LoginGateCode).where(
        models.LoginGateCode.user_id == user.id,
        models.LoginGateCode.used == False,  # noqa: E712
    ).values(used=True))

    code = f"{secrets.randbelow(1000000):06d}"
    pre_token = secrets.token_urlsafe(24)
    db.add(models.LoginGateCode(
        user_id=user.id,
        code_hash=hashlib.sha256(code.encode()).hexdigest(),
        pre_token=pre_token,
        expires_at=now + timedelta(minutes=CODE_TTL_MIN),
    ))
    await push_message(
        db, to_role="manager", kind="warn",
        text=f"【外网登录验证】{user.full_name}({user.username}) 正在外网登录系统，"
             f"验证码：{code}（10 分钟内有效）。请核实身份后告知本人。",
    )  # push_message 自带 commit，上面的码行随之落库
    return pre_token


async def verify_code(db: AsyncSession, user: models.User, pre_token: str, code: str) -> None:
    """验码：按 pre_token+user_id 找未用行；不存在/已用/过期 → 400；哈希不符 → fail_count+1 后 400；
    成功 → used=True。异常一律 HTTPException，由调用方写审计。
    （🆕 2026-07-28 应要求去掉「错 5 次锁定」：不再锁，错误仅计 fail_count）"""
    row = (await db.execute(select(models.LoginGateCode).where(
        models.LoginGateCode.user_id == user.id,
        models.LoginGateCode.pre_token == pre_token,
        models.LoginGateCode.used == False,  # noqa: E712
    ))).scalar_one_or_none()
    if not row or _aware(row.expires_at) < _now():
        raise HTTPException(400, "验证码无效或已过期，请重新获取")
    if hashlib.sha256(code.strip().encode()).hexdigest() != row.code_hash:
        row.fail_count += 1
        await db.commit()
        raise HTTPException(400, "验证码错误")
    row.used = True
    await db.commit()
