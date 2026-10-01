"""🆕 2026-10-01 智能体的「今天」一律按北京时间。

生产容器是 UTC（`docker exec pms2_backend date` 显示 UTC，TZ 没设）。工具里原来到处是 `date.today()`，
于是**每天早上 8 点前**工具算的「今天」都是昨天：逾期天数少一天、「今天到期」算成「明天到期」、
月初那几个小时「本月销售额」还在算上个月。生产实例：王芹 09-30 07:40 问「我手上的活」，
模型自己都指出「工具返回的日期是 2026-09-29，与今天差一天」。

业务日期（交货日、预计到货日、到期日）都是北京时间的日子，所以拿来比的「今天」必须也是北京时间。
datetime 字段（created_at 等）存的是 UTC，算「过了几天」前先转成北京时间的日子再减。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))


def today() -> date:
    """北京时间的今天。智能体里别再用 date.today()。"""
    return datetime.now(CN_TZ).date()


def cn_date(ts: datetime | None) -> date | None:
    """datetime → 北京时间的日子。无时区的按 UTC 处理（SQLite 取出来就是这样）。"""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(CN_TZ).date()
