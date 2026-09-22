"""🆕 2026-09-23 「我手上的活」—— 推广到全公司时补的第三批工具。

为什么非补不可：这次要开给全部 26 个账号，其中装配、钣金、封板三个组是人数最多的一批，
而智能体**一个能答他们问题的工具都没有**。

具体缺在哪：这三个组的任务派在 `produce_group_tasks` 表上，
`dept_orders.worker_id` 对他们**始终是 NULL**（生产主管先接部门单、再分派到组，
见 deps.restricted_dir_pids 里同一条注释）。而助手现有的 `overdue_orders`
查的是 dept_orders.worker_id —— 于是装配工问「我今天要干什么」，
查到的永远是 0 条，助手回一句「你没有待办 ✅」。**数据是有的，只是没人去取。**

这个工具把两张表合起来按「派给我的」取：
  · dept_orders        设计 / 电工 / 生产（派到人的那一层）
  · produce_group_tasks 钣金 / 装配 / 封板（派到组内具体人的那一层）

权限上没有任何可争议的地方：**只取 worker_id == 自己**。
主管想看别人的活走 overdue_orders（那里有部门主管的口径），这里不掺和。
"""
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models

# 与 tools_entity 同一套中文名，别各写各的
_DEPT_CN = {"design": "设计", "electric": "电工", "produce": "生产"}
_GROUP_CN = {"sheetmetal": "钣金", "assembly": "装配", "sealing": "封板"}
_STATUS_CN = {"assigned": "待接单", "in_progress": "进行中",
              "dispatched": "已派工", "pending_assign": "待分派"}


def _left(due: str | None) -> int | None:
    """距截止还有几天（负数 = 已超期）。日期坏就返回 None，不猜。"""
    d = (due or "").strip()
    if not d:
        return None
    try:
        return (date.fromisoformat(d) - date.today()).days
    except ValueError:
        return None


async def my_tasks(db: AsyncSession, current: models.User) -> dict:
    """派给我、还没做完的活。部门单 + 生产组任务合成一张表。

    排序：已超期最前，然后按剩余天数升序，没填截止日期的排最后。
    没填截止日期的**不丢掉** —— 生产上确实有一批没填，丢了就是「系统说我没活」。
    """
    rows: list[dict] = []

    # ── 部门单（设计/电工/生产）──
    orders = (await db.execute(
        select(models.DeptOrder)
        .join(models.Project, models.Project.id == models.DeptOrder.project_id)
        .where(models.Project.is_deleted == False,  # noqa: E712
               models.DeptOrder.worker_id == current.id,
               models.DeptOrder.status.notin_(("done", "voided"))))).scalars().all()
    for o in orders:
        p = o.project
        rows.append({
            "project": p.code if p else f"#{o.project_id}",
            "name": (p.name if p else "") or "",
            "what": _DEPT_CN.get(o.dept, o.dept),
            "status": _STATUS_CN.get(o.status, o.status),
            "due_date": o.due_date or "",
            "days_left": _left(o.due_date),
        })

    # ── 生产组任务（钣金/装配/封板）──
    #   ⚠️ 这一段就是以前整个缺掉的。关联的是 produce_group_tasks.worker_id，
    #      不是 dept_orders.worker_id —— 后者对组员永远是 NULL。
    tasks = (await db.execute(
        select(models.ProduceGroupTask)
        .join(models.Project, models.Project.id == models.ProduceGroupTask.project_id)
        .where(models.Project.is_deleted == False,  # noqa: E712
               models.ProduceGroupTask.worker_id == current.id,
               models.ProduceGroupTask.status != "done"))).scalars().all()
    for t in tasks:
        p = t.project
        rows.append({
            "project": p.code if p else f"#{t.project_id}",
            "name": (p.name if p else "") or "",
            "what": _GROUP_CN.get(t.group, t.group),
            "status": _STATUS_CN.get(t.status, t.status),
            "due_date": t.due_date or "",
            "days_left": _left(t.due_date),
        })

    rows.sort(key=lambda r: (r["days_left"] is None,
                             r["days_left"] if r["days_left"] is not None else 0))
    overdue = sum(1 for r in rows if r["days_left"] is not None and r["days_left"] < 0)
    return {
        "count": len(rows),
        "overdue": overdue,
        "today": date.today().isoformat(),
        "columns": ["project", "what", "status", "days_left"],
        "items": rows,
        # 一条都没有时明说「确实没有」，别让模型自己编一句含糊的话。
        # 这个场景下「查不到」和「没有」是两回事，而用户分不出来 —— 我们必须分。
        "hint": None if rows else "系统里现在没有派给你的未完成任务",
    }
