"""装配交给质检的任务范围，供质检部、项目目录与存量迁移共用。"""
from sqlalchemy import select

from .models import DeptOrder, ProduceGroupTask, Project


def completed_assembly_conditions():
    task = ProduceGroupTask
    return (
        task.group == "assembly",
        task.status == "done",
        select(DeptOrder.id).where(
            DeptOrder.id == task.order_id,
            DeptOrder.dept == "produce",
            DeptOrder.status != "voided",
        ).exists(),
        select(Project.id).where(Project.id == task.project_id, Project.is_deleted.is_(False)).exists(),
    )


def quality_queue_conditions():
    return (*completed_assembly_conditions(), ProduceGroupTask.quality_queued.is_(True))
