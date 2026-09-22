"""🆕 2026-09-23 智能体的用量闸：每人每日上限 + 全局并发上限。

为什么现在才加：智能体此前实际只有管理层和少数几个人在用，一天几十次，
没有闸也不出事。推广到全部 26 个在用账号之后，两件事会同时变：

  1. **钱**。一次 ReAct 问答平均 14 秒、往返多轮，单次成本不小。
     没有上限时，一个人把助手当聊天框玩一下午，账单是全公司的。
  2. **这台机器**。生产是 3.5GB 内存的单机（backend/frontend/nginx/postgres/redis 全在上面）。
     流式问答期间连接一直挂着，十几个人同时点「问」，Postgres 连接池先被占满，
     然后是**整个系统**卡，不只是助手卡。

两道闸的处理方式刻意不同：
  · 超日限 → **降级到规则引擎**，不是报错。规则路径不花钱，仍然能查到真数据，
    用户至少拿得到答案。直接 429 会被理解成「系统坏了」。
  · 并发满 → 先排队等（有上限地等），等不到再降级。瞬时并发多半是几秒的尖峰，
    等一下就过去了，为几秒钟把人踢到降级路径不划算。

上限都可调（app_settings，管理员改完即时生效，不用重启）：
  agent_daily_llm_limit    每人每天多少次 LLM 问答，0 = 不限制
  agent_max_concurrent_llm 同时允许几路 LLM 在跑，0 = 不限制
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models
from ..overdue import _CN_TZ

log = logging.getLogger(__name__)

_KEY_DAILY = "agent_daily_llm_limit"
_KEY_CONC = "agent_max_concurrent_llm"

# 默认值。60 次/人/天是按生产日志定的：现网最重度的用户（杨坛）单日峰值 23 次，
# 60 给了两倍多的余量——正常干活的人一辈子撞不到，只拦住异常用法。
_DEFAULT_DAILY = 60
# 4 路并发也是按这台机器定的：Postgres 连接池 10，流式问答期间每路占 1 条，
# 留一半给网页端的普通请求。别照抄到更大的机器上，那样是白白限速。
_DEFAULT_CONC = 4

# 等并发位最多等这么久。超过这个时间用户已经在盯着转圈了，
# 与其继续等，不如走规则路径给个真答案。
_WAIT_SECONDS = 8.0

# 管理层不受日限约束：他们是这套东西的主要使用者，也是为它付钱的人。
_EXEMPT = ("admin", "manager")

_sema: asyncio.Semaphore | None = None
_sema_size = -1


async def _setting(db: AsyncSession, key: str, default: int) -> int:
    """读 app_settings 里的整数配置。读不到/坏值一律回默认值——
    配置写坏了不能把功能整个关掉。"""
    r = (await db.execute(select(models.AppSetting).where(
        models.AppSetting.key == key))).scalar_one_or_none()
    if not r or not r.value:
        return default
    try:
        v = json.loads(r.value)
        return max(0, int(v))
    except (TypeError, ValueError):
        log.warning("[agent] 配置 %s 值不是整数（%r），按默认 %d", key, r.value, default)
        return default


def _day_start_utc() -> datetime:
    """中国时间今天 00:00 对应的 UTC 时刻。

    ⚠️ 必须按**中国**的一天切，不能用 UTC 的。UTC 切的话每天早上 8 点之前
       算的是昨天的额度，用户上午刚开工就可能被昨天的用量顶住。
       （这个坑在金额审计里按月统计时踩过一次，同源问题。）
    """
    cn_today = datetime.now(_CN_TZ).date()
    return datetime(cn_today.year, cn_today.month, cn_today.day,
                    tzinfo=_CN_TZ).astimezone(timezone.utc)


async def used_today(db: AsyncSession, user: models.User) -> int:
    """这个人今天（中国时间）已经用掉几次 LLM 问答。

    数据源就是审计日志 agent_chat_logs —— 不另建计数表：
    多一张表就多一处可能与日志对不上的地方，而对不上的用量数字没人敢信。
    只数 via='llm'：规则降级不花钱，不该占额度。
    """
    since = _day_start_utc()
    n = (await db.execute(
        select(func.count()).select_from(models.AgentChatLog).where(
            models.AgentChatLog.user_id == user.id,
            models.AgentChatLog.via == "llm",
            models.AgentChatLog.created_at >= since,
        ))).scalar_one()
    return int(n or 0)


async def check_daily(db: AsyncSession, user: models.User) -> tuple[bool, str]:
    """(还能不能用 LLM, 用不了时给用户看的话)。

    第二个返回值是**给一线看的中文**，不是错误码：这句话会原样出现在聊天窗口里。
    """
    limit = await _setting(db, _KEY_DAILY, _DEFAULT_DAILY)
    if limit <= 0 or user.has_role(*_EXEMPT):
        return True, ""
    used = await used_today(db, user)
    if used < limit:
        return True, ""
    return False, (f"今天的 AI 问答次数用完了（{used}/{limit} 次）。"
                   f"接下来的问题我改用固定查询来答——数据一样是真的，"
                   f"只是不会展开分析。明天零点恢复。")


async def acquire(db: AsyncSession) -> bool:
    """占一个并发位。占到返回 True（调用方必须配 release），等不到返回 False。

    ⚠️ 信号量是**进程内**的。生产就一个 backend 容器，够用；
       将来要是横向扩了多个副本，这里得换成 Redis 计数，别忘了这一条。
    """
    global _sema, _sema_size
    n = await _setting(db, _KEY_CONC, _DEFAULT_CONC)
    if n <= 0:
        return True
    if _sema is None or _sema_size != n:
        # 配置改了就换一个新的。旧信号量上正在跑的那几路照常跑完，
        # 只是它们 release 到一个没人再等的对象上——无害。
        _sema, _sema_size = asyncio.Semaphore(n), n
    try:
        await asyncio.wait_for(_sema.acquire(), timeout=_WAIT_SECONDS)
        return True
    except asyncio.TimeoutError:
        log.info("[agent] 并发已满（上限 %d），本次转规则降级", n)
        return False


def release() -> None:
    """还回并发位。**必须放在 finally 里** —— 漏一次，这个位子就永久少一个，
    少几次之后所有人都在排队，表现是「助手越来越慢」，而且重启才好。"""
    if _sema is not None:
        try:
            _sema.release()
        except ValueError:
            # release 多于 acquire。说明调用方配对写错了，记下来别吞掉。
            log.warning("[agent] 并发位 release 多于 acquire，检查调用配对")


def busy_note() -> str:
    """并发满时给用户看的话。"""
    return "（这会儿同时用的人有点多，这次先用固定查询答你，数据是一样的。）"
