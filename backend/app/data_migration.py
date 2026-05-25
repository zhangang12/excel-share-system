"""存量数据迁移：启动时一次性运行，所有函数必须幂等。

这里只处理"业务规则变更带来的存量数据修正"，
不做 schema 改动（schema 由 Base.metadata.create_all 负责）。
"""
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import models

log = logging.getLogger("data_migration")


async def backfill_empty_project_members(db: AsyncSession) -> int:
    """对"还没添加任何成员的活跃项目"，回填全员 edit 权限。

    - 候选用户：is_active=true 且 role 不是 admin/manager
      （admin/manager 在权限层自动拥有所有项目，不需要塞 member 列表）
    - 已有成员的项目保持不动（尊重已做的精细化配置）
    - 已软删的项目跳过（这些会被另一个 migration 清掉）
    - 幂等：再次运行不会重复添加
    """
    res = await db.execute(
        select(models.User).join(models.Role).where(
            models.User.is_active == True,
            models.Role.code.notin_(("admin", "manager")),
        )
    )
    candidates = res.scalars().all()
    if not candidates:
        return 0

    res = await db.execute(
        select(models.Project).where(models.Project.is_deleted == False)
    )
    projects = res.scalars().all()
    if not projects:
        return 0

    # 已经有任何成员的项目集合
    res = await db.execute(select(models.ProjectMember.project_id).distinct())
    projects_with_members = {r[0] for r in res.all()}

    empty_projects = [p for p in projects if p.id not in projects_with_members]
    if not empty_projects:
        return 0

    added = 0
    for p in empty_projects:
        for u in candidates:
            db.add(models.ProjectMember(
                project_id=p.id, user_id=u.id, permission="edit"
            ))
            added += 1

    await db.commit()
    log.info(
        "[backfill_empty_project_members] 回填 %d 条 (无成员项目 %d 个 × 候选用户 %d 个)",
        added, len(empty_projects), len(candidates),
    )
    return added


async def cleanup_deleted_project_data(db: AsyncSession) -> dict:
    """清理软删项目（is_deleted=true）挂着的孤儿数据。

    - 项目本身保留（is_deleted=true 是审计追溯需要）
    - 显式删整条链：datasheets / fields / records / field_permissions / project_members
    - 幂等：没有孤儿数据时是空操作
    """
    # 避免循环 import：函数内 import
    from .routers.projects_router import _purge_project_derived_data

    res = await db.execute(
        select(models.Project.id).where(models.Project.is_deleted == True)
    )
    deleted_pids = [r[0] for r in res.all()]
    if not deleted_pids:
        return {"projects": 0}

    counts = await _purge_project_derived_data(db, deleted_pids)
    await db.commit()

    if any(counts.values()):
        log.info(
            "[cleanup_deleted_project_data] 清理 %d 个软删项目的孤儿数据：%s",
            len(deleted_pids), counts,
        )
    return {"projects": len(deleted_pids), **counts}


async def run_all(db: AsyncSession) -> None:
    """启动时调用：依次跑所有迁移；任一失败只 warn 不阻塞启动。"""
    try:
        await cleanup_deleted_project_data(db)
    except Exception as e:
        log.warning("cleanup_deleted_project_data failed: %s", e)
    try:
        await backfill_empty_project_members(db)
    except Exception as e:
        log.warning("backfill_empty_project_members failed: %s", e)
