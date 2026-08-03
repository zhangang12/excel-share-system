"""每天早上把简报推给管理层（站内 + 企微双通道）。

为什么要主动推
--------------
助手做得再好，前提是「他记得打开、并且知道该问什么」。管理层不会天天想起来问。
推送通道（notify.push_message → 站内消息 + 企微）早就有了，**智能体一次都没调过**。
这里把两头接上：`briefing.build()` 出判断，`push_message` 送出去，
消息里带 H5 深链，点进去直接是能处理的卡片。

⚠️ 不要变成第二个催办。生产上催办推了 83 条（balance_due 31 + balance_overdue 52），
没解决问题，因为**推的是清单、而且天天推同一批**。所以这里：
  - 只推 3 条，每条带「为什么是它」
  - `_REPEAT_COOLDOWN_DAYS` 天内推过的不再推（已推清单存 app_settings，无 schema 变更）
  - 一条都没有就**不发**——没事别打扰人，这是「主动推」能活下来的前提

收件人
------
`app_settings.agent_briefing_users`（JSON 数组，存 username）。
留空 = 谁都不推（默认关，避免一上线就全员轰炸）。
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models
from ..database import SessionLocal
from ..notify import push_message
from . import briefing

log = logging.getLogger("agent.daily")

_CN_TZ = timezone(timedelta(hours=8))
_SETTING_USERS = "agent_briefing_users"
_SETTING_SENT = "agent_briefing_sent"
_PUSH_HOUR_CN = 8            # 早 8 点（中国时区）
H5_URL = "https://pms.tonghui-tech.com/h5/"


async def _get_setting(db: AsyncSession, key: str, default):
    r = (await db.execute(select(models.AppSetting).where(
        models.AppSetting.key == key))).scalar_one_or_none()
    if not r or not r.value:
        return default
    try:
        return json.loads(r.value)
    except (TypeError, ValueError):
        return default


async def _set_setting(db: AsyncSession, key: str, value):
    r = (await db.execute(select(models.AppSetting).where(
        models.AppSetting.key == key))).scalar_one_or_none()
    payload = json.dumps(value, ensure_ascii=False)
    if r:
        r.value = payload
    else:
        db.add(models.AppSetting(key=key, value=payload))
    await db.commit()


async def run_once(db: AsyncSession, *, dry: bool = False) -> dict:
    """跑一轮：给每个收件人算简报并推送。返回统计，方便手动触发时看结果。"""
    usernames = await _get_setting(db, _SETTING_USERS, [])
    if not usernames:
        log.info("[agent.daily] %s 未配置，跳过", _SETTING_USERS)
        return {"recipients": 0, "sent": 0, "detail": []}

    sent_log: dict = await _get_setting(db, _SETTING_SENT, {})
    cutoff = (datetime.now(timezone.utc) - timedelta(
        days=briefing._REPEAT_COOLDOWN_DAYS)).date().isoformat()

    out, n_sent = [], 0
    for uname in usernames:
        u = (await db.execute(select(models.User).where(
            models.User.username == uname,
            models.User.is_active == True))).scalar_one_or_none()  # noqa: E712
        if not u:
            out.append({"user": uname, "skip": "用户不存在或已停用"})
            continue

        # 冷却期内推过的不再推；顺手把过期记录清掉，免得这个 setting 无限长大
        mine = {k: v for k, v in (sent_log.get(uname) or {}).items() if v >= cutoff}
        skip_refs = {(k.split(":")[0], int(k.split(":")[1])) for k in mine}

        brief = await briefing.build(db, u, top=3, skip_refs=skip_refs)
        text = briefing.render(brief, u.full_name or u.username)
        if not text:
            out.append({"user": uname, "skip": "没有待办，不打扰"})
            continue

        text += f"\n{H5_URL}"        # 企微里可点，直接进 H5 助手
        out.append({"user": uname, "text": text, "items": len(brief["items"])})
        if dry:
            continue

        await push_message(db, to_user_id=u.id, kind="info", text=text,
                           biz_type="agent_briefing", biz_id=u.id)
        today = datetime.now(timezone.utc).date().isoformat()
        for it in brief["items"]:
            mine[f"{it['card']}:{it['ref']}"] = today
        sent_log[uname] = mine
        n_sent += 1

    if not dry:
        await _set_setting(db, _SETTING_SENT, sent_log)
    return {"recipients": len(usernames), "sent": n_sent, "detail": out}


def _seconds_to_next_run() -> float:
    """到下一个「中国时间早 8 点」还有多少秒。

    ⚠️ 服务器是 UTC，直接用 UTC 的小时数会推到北京时间下午 4 点去。
    """
    now = datetime.now(_CN_TZ)
    nxt = now.replace(hour=_PUSH_HOUR_CN, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def _try_lock():
    """与 overdue_scheduler 同一套路：多 worker 部署时只让一个进程推，
    否则 N 个 worker 同时醒来，同一个人收到 N 条一模一样的简报。"""
    try:
        import fcntl
    except ImportError:
        return "no-lock"
    from pathlib import Path
    from ..config import settings
    d = Path(settings.files_dir).resolve()
    d.mkdir(parents=True, exist_ok=True)
    fh = open(d / ".agent_briefing.lock", "w")  # noqa: SIM115 句柄需随进程存活
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


async def briefing_scheduler() -> None:
    """启动期常驻：每天中国时间 8 点推一次。"""
    _lock = _try_lock()
    if _lock is None:
        log.info("[agent.daily] 另一进程已在跑简报调度，本 worker 跳过")
        return
    while True:
        await asyncio.sleep(_seconds_to_next_run())
        try:
            async with SessionLocal() as db:
                r = await run_once(db)
            log.info("[agent.daily] 简报已推：%s", r)
        except Exception as e:  # noqa: BLE001
            # 推送失败不能把调度器带走，否则一次异常之后再也不推了
            log.warning("[agent.daily] 推送失败: %s", e)
