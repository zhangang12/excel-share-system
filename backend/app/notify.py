"""🆕 v3 站内消息推送服务（企微通道降级兜底）。

- push_message(to_user_id=...) 单人推送
- push_message(to_role=...)    角色池推送：写入时按角色扇出成每用户一行
  （替代原型硬编码个人 lo1/sm1/fin1-3；已读状态天然按人独立）
- 企微通道：用户已绑 wxid 且 settings.wecom_* 配置齐 → 尝试外发；
  失败/未配置仅记日志，绝不阻塞主业务事务（F3 口径）。

调用约定：在业务事务 commit **之后**调用（避免幻影通知）；本服务自行 commit。
"""
import logging
import time
from typing import Optional

import httpx
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from . import models
from .config import settings

log = logging.getLogger("notify")

# 企业微信应用消息 API + access_token 缓存（worker 进程级，约 2h 有效）
_WECOM_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
_wecom_token_cache: dict = {"token": "", "expire": 0.0}


async def push_message(
    db: AsyncSession,
    *,
    to_user_id: Optional[int] = None,
    to_role: Optional[str] = None,
    kind: str = "info",          # wx / warn / info
    text: str,
    biz_type: Optional[str] = None,
    biz_id: Optional[int] = None,
    exclude_user_ids: Optional[set] = None,   # 🆕 扇出时排除这些人（如已单独推过的下单人，避免同人同日两条相同文本）
) -> int:
    """推送站内消息。返回写入条数。to_user_id 与 to_role 至少给一个。"""
    user_ids: list[int] = []
    if to_user_id is not None:
        user_ids.append(to_user_id)
    if to_role:
        # 多角色：锚点 role_id 命中 或 user_roles 关联命中任一即推送
        # （否则「该角色仅为副角色」的用户收不到按角色的待办/提醒推送）
        # 🆕 财务主管 ⊇ 财务：推给 finance 时也扇出给 finance_lead
        target_codes = [to_role] + (["finance_lead"] if to_role == "finance" else [])
        rids = [r[0] for r in (await db.execute(
            select(models.Role.id).where(models.Role.code.in_(target_codes)))).all()]
        if rids:
            sub = select(models.UserRole.user_id).where(models.UserRole.role_id.in_(rids))
            res = await db.execute(
                select(models.User.id).where(
                    models.User.is_active == True,  # noqa: E712
                    or_(models.User.role_id.in_(rids), models.User.id.in_(sub)),
                )
            )
            user_ids.extend(r[0] for r in res.all())
    if not user_ids:
        log.info("push_message: 角色 %s 无在线用户，消息丢弃: %s", to_role, text[:50])
        return 0

    excl = set(exclude_user_ids or ())
    seen: set[int] = set()
    rows = []
    for uid in user_ids:
        if uid in seen or uid in excl:
            continue
        seen.add(uid)
        rows.append(models.Message(
            to_user_id=uid, kind=kind, text=text,
            biz_type=biz_type, biz_id=biz_id,
        ))
    db.add_all(rows)
    await db.commit()

    # 企微外发（尽力而为，不影响站内）
    if settings.wecom_corp_id and settings.wecom_secret:
        try:
            await _send_wecom(db, list(seen), text)
        except Exception as e:  # noqa: BLE001
            log.warning("企微推送失败（站内消息已落）: %s", e)
    return len(rows)


async def _wecom_token() -> str:
    """取企微 access_token（带缓存，提前 60s 过期重取）。"""
    now = time.time()
    if _wecom_token_cache["token"] and _wecom_token_cache["expire"] > now + 60:
        return _wecom_token_cache["token"]
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{_WECOM_BASE}/gettoken", params={
            "corpid": settings.wecom_corp_id,
            "corpsecret": settings.wecom_secret,
        })
        data = r.json()
    if data.get("errcode", 0) != 0 or not data.get("access_token"):
        raise RuntimeError(f"企微 gettoken 失败: {data}")
    _wecom_token_cache["token"] = data["access_token"]
    _wecom_token_cache["expire"] = now + int(data.get("expires_in", 7200))
    return data["access_token"]


async def wecom_userid_by_code(code: str) -> str:
    """企微网页授权 code → 企业内 UserId。拿不到返回空串。

    ⚠️ 复用 `_wecom_token()` 的缓存：这是**应用级** access_token，
       和推送用的是同一个，不要另开一套缓存——两套缓存互相不知道对方
       刷新了 token，会来回把对方的顶掉（企微同一应用只认最新签发的那个）。
    """
    token = await _wecom_token()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{_WECOM_BASE}/auth/getuserinfo",
                             params={"access_token": token, "code": code})
        data = r.json()
    if data.get("errcode") in (40014, 42001):
        _wecom_token_cache["token"] = ""
    if data.get("errcode", 0) != 0:
        raise RuntimeError(f"企微 getuserinfo 失败: errcode={data.get('errcode')}")
    # 企业成员返回 UserId；外部联系人返回 OpenId（没有 UserId）——后者不给登录
    return str(data.get("UserId") or data.get("userid") or "")


def h5_url() -> str:
    """企微里点开消息该去哪。空字符串 = 没配可信域名，消息退回纯文本。"""
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/h5/" if base else ""


def _wecom_payload(wxids: list[str], text: str) -> dict:
    """拼企微应用消息体。配了 public_base_url 就发**可点的卡片**，否则发纯文本。

    ⚠️ 为什么非要可点：消息本身只是一句话（「2026-060A 还剩 5 天」），
       看完还得自己开电脑找。textcard 带 url 直接落到 H5 首页，
       这才是「手机版应用」和「一条通知」的区别。

    ⚠️ textcard 的 description 支持的标签很有限，且**整体有长度上限**；
       这里只做截断，不塞 HTML —— 业务文案里带 `<` `&` 的话塞标签必然裂开。
    """
    common = {"touser": "|".join(wxids[:1000]),     # 企微单次最多 1000 人
              "agentid": int(settings.wecom_agent_id), "safe": 0}
    url = h5_url()
    if not url:
        return {**common, "msgtype": "text", "text": {"content": text[:2000]}}
    body = (text or "").strip()
    # 第一行当标题：业务文案基本都是「【xx】yy」开头，正好是摘要
    title = body.split("\n", 1)[0][:36] or "项目管理系统"
    return {**common, "msgtype": "textcard",
            "textcard": {"title": title, "description": body[:500],
                         "url": url, "btntxt": "打开"}}


async def _send_wecom(db: AsyncSession, user_ids: list[int], text: str) -> None:
    """企微应用消息外发：取已绑 wxid 的用户 → gettoken → message/send 文本消息。
    凭证/网络/IP白名单等问题由调用方捕获降级（不阻塞站内消息）。"""
    res = await db.execute(
        select(models.User.wxid).where(
            models.User.id.in_(user_ids), models.User.wxid.isnot(None),
        )
    )
    wxids = [r[0] for r in res.all() if r[0]]
    if not wxids:
        return
    if not settings.wecom_agent_id:
        log.warning("企微 agent_id 未配置，跳过外发（仅站内）")
        return
    token = await _wecom_token()
    payload = _wecom_payload(wxids, text)
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{_WECOM_BASE}/message/send",
                              params={"access_token": token}, json=payload)
        data = r.json()
    errcode = data.get("errcode", 0)
    if errcode in (40014, 42001):             # token 失效 → 清缓存下次重取
        _wecom_token_cache["token"] = ""
    if errcode != 0:
        raise RuntimeError(f"企微 message/send 失败: {data}")
    if data.get("invaliduser"):
        log.warning("企微部分 userid 无效(未关注/不存在): %s", data["invaliduser"])
    log.info("[企微] 已推送 %d 人", len(wxids))
