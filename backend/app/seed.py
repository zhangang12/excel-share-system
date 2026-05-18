"""启动时种子：角色 + 默认管理员"""
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import models
from .auth import hash_password
from .config import settings

log = logging.getLogger("seed")

ROLES = [
    ("admin", "超级管理员", "系统全部权限"),
    ("manager", "管理层", "可配置字段权限，读写所有数据"),
    ("designer", "设计师", "按字段权限读写"),
    ("production_clerk", "生产文员", "按字段权限读写"),
    ("warehouse", "仓库员", "按字段权限读写"),
    ("buyer_standard", "标准件采购员", "按字段权限读写"),
    ("buyer_outsource", "外协机加工采购员", "含原材料采购；按字段权限读写"),
    ("hr", "人事行政", "按字段权限读写"),
]


async def seed(db: AsyncSession) -> None:
    # 角色
    for code, name, desc in ROLES:
        res = await db.execute(select(models.Role).where(models.Role.code == code))
        existing = res.scalar_one_or_none()
        if existing is None:
            db.add(models.Role(code=code, name=name, description=desc))
        else:
            # 同步名称/描述（角色配置变了用户重启就生效）
            existing.name = name
            existing.description = desc
    await db.commit()

    # admin 用户
    res = await db.execute(
        select(models.User).where(models.User.username == settings.default_admin_username)
    )
    if res.scalar_one_or_none() is None:
        admin_role = (
            await db.execute(select(models.Role).where(models.Role.code == "admin"))
        ).scalar_one()
        u = models.User(
            username=settings.default_admin_username,
            full_name="超级管理员",
            password_hash=hash_password(settings.default_admin_password),
            password_must_change=True,
            role_id=admin_role.id,
            is_active=True,
        )
        db.add(u)
        await db.commit()
        log.info(
            "✅ 默认管理员已创建: %s / %s",
            settings.default_admin_username,
            settings.default_admin_password,
        )
    else:
        log.info("ℹ️ admin 已存在")

    # 自动创建一个"管理层"账号（日常使用，admin 仅用于系统底层）
    res = await db.execute(select(models.User).where(models.User.username == "manager"))
    if res.scalar_one_or_none() is None:
        manager_role = (
            await db.execute(select(models.Role).where(models.Role.code == "manager"))
        ).scalar_one()
        m = models.User(
            username="manager",
            full_name="管理员",
            password_hash=hash_password("manager123"),
            password_must_change=True,
            role_id=manager_role.id,
            is_active=True,
        )
        db.add(m)
        await db.commit()
        log.info("✅ 已创建默认管理员账号: manager / manager123")

