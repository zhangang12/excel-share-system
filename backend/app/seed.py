"""启动时种子：角色 + 默认管理员"""
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import models
from .auth import hash_password
from .config import settings

log = logging.getLogger("seed")

# (code, name, description, can_push 消息推送人默认值)
# 老 8 角色保持不变（红线：只增不改）；🆕 v3 增量角色按《权限矩阵》补齐
ROLES = [
    ("admin", "超级管理员", "系统全部权限", False),
    ("manager", "管理层", "可配置字段权限，读写所有数据", False),
    ("designer", "设计师", "按字段权限读写", False),
    ("production_clerk", "生产文员", "按字段权限读写", False),
    ("warehouse", "仓库员", "按字段权限读写", False),
    ("buyer_standard", "标准件采购员", "按字段权限读写（已并入采购部，保留兼容）", False),
    ("buyer_outsource", "外协机加工采购员", "含原材料采购；已并入采购部，保留兼容", False),
    ("hr", "人事行政", "按字段权限读写", False),
    # ---- 🆕 v3 增量角色 ----
    ("sales", "销售员", "销售台账（仅本人订单）/销售下单", False),
    ("sales_lead", "销售主管", "销售台账全量+合计/编辑/开票审批", True),
    ("design_lead", "设计部负责人", "设计任务分派/作废/部门报表", True),
    ("electrician", "电工", "电工部工作台（本人任务）", False),
    ("electric_lead", "电工部负责人", "电工任务分派/作废/部门报表", True),
    ("assembler", "生产部-装配组", "生产部装配组（被派发项目+前置表备齐状态）/问题反馈", False),
    ("pm_lead", "生产部主管", "生产任务派发钣金/装配两组/问题反馈审批/部门报表", True),
    ("sheetmetal", "生产部-钣金组", "生产部钣金组（被派发项目+只读钣金装配表）", False),
    ("sealing", "生产部-封板组", "生产部封板组（被派发项目+激光件清单/CAD激光图纸）", False),  # 🆕 反馈#209
    ("buyer", "采购部", "采购清单收件箱/第5表采购列维护", False),
    ("buyer_lead", "采购主管", "采购管理全功能+请款审批接收+汇总报表", True),  # 🆕 采购管理模块
    ("warehouse_lead", "仓库主管", "仓库管理全功能+低库存预警接收", True),
    ("logistics", "物流发货部", "发货看板/确认发货/收货信息维护", False),
    ("finance", "财务部", "开票/售后费用查看", False),
    ("finance_lead", "财务主管", "财务部全功能（含请款审批/付款，与财务分权做内控）", True),
    ("as_worker", "售后部员工", "售后问题/费用登记", False),
    ("as_lead", "售后部主管", "售后费用审批→同步财务", True),
]


async def seed(db: AsyncSession) -> None:
    # 角色
    for code, name, desc, can_push in ROLES:
        res = await db.execute(select(models.Role).where(models.Role.code == code))
        existing = res.scalar_one_or_none()
        if existing is None:
            db.add(models.Role(code=code, name=name, description=desc, can_push=can_push))
        else:
            # 同步名称/描述（角色配置变了用户重启就生效）；can_push 仅在空值时初始化，
            # 不覆盖管理层在权限页手动改过的标记
            existing.name = name
            existing.description = desc
            if existing.can_push is None:
                existing.can_push = can_push
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

