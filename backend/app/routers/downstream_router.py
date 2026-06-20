"""🆕 v3 M05 钣金组 + M06 采购部 —— 消费 M04 电工/设计接单上传的下游承接页。

均为只读承接，数据源是统一附件表（撤回天然联动：M04 移除附件即从收件箱/图纸包消失）：
- 钣金组：项目列表 + PDF 图纸包(order_start_output/sheetpkg) + 钣金装配表只读引用(datasheet_id)
- 采购部：采购清单收件箱(order_start_output/plist) — 编号/名称/来源/文件/收到时间
"""
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from .. import models, schemas
from ..deps import require_roles

router = APIRouter(prefix="/api", tags=["下游承接"])


# ==================== M05 钣金组 ====================
class SheetMetalRow(BaseModel):
    project_id: int
    code: str
    name: str
    designer: Optional[str] = None
    sheetmetal_datasheet_id: Optional[int] = None   # 钣金装配表（只读引用）
    sheetmetal_done: bool = False                    # 钣金装配是否已标记完成
    pkg_files: list[schemas.AttachmentOut] = []      # PDF 图纸包


@router.get("/sheetmetal/projects", response_model=List[SheetMetalRow])
async def sheetmetal_projects(
    _: models.User = Depends(require_roles("sheetmetal")),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(models.Project).where(models.Project.is_deleted == False)  # noqa: E712
        .order_by(models.Project.code)
    )
    projects = list(res.scalars().all())
    pids = [p.id for p in projects]
    if not pids:
        return []

    # 图纸包附件（排除已作废来源单的图纸包 #7）
    res = await db.execute(select(models.Attachment)
        .join(models.DeptOrder, models.Attachment.biz_id == models.DeptOrder.id)
        .where(
            models.Attachment.biz_type == "order_start_output",
            models.Attachment.kind == "sheetpkg",
            models.Attachment.project_id.in_(pids),
            models.DeptOrder.status != "voided").order_by(models.Attachment.id))
    pkg_by_pid: dict[int, list] = {}
    for a in res.scalars().all():
        pkg_by_pid.setdefault(a.project_id, []).append(schemas.AttachmentOut.model_validate(a))

    # 钣金装配表
    res = await db.execute(select(models.Datasheet).where(
        models.Datasheet.project_id.in_(pids), models.Datasheet.name == "钣金装配"))
    bj_by_pid: dict[int, models.Datasheet] = {d.project_id: d for d in res.scalars().all()}

    rows = []
    for p in projects:
        extra = p.extra or {}
        bj = bj_by_pid.get(p.id)
        rows.append(SheetMetalRow(
            project_id=p.id, code=p.code, name=p.name,
            designer=extra.get("__o__设计师"),
            sheetmetal_datasheet_id=bj.id if bj else None,
            sheetmetal_done=bool(bj.done_flag) if bj else False,
            pkg_files=pkg_by_pid.get(p.id, []),
        ))
    return rows


# ==================== M06 采购部收件箱 ====================
class PurchaseInboxRow(BaseModel):
    project_id: int
    code: str
    name: str
    source: str
    file: schemas.AttachmentOut
    received_at: str


class PurchaseProjectRow(BaseModel):
    project_id: int
    code: str
    name: str
    designer: Optional[str] = None
    # 数据表引用（前端按 datasheet_id 走导出下载）
    outsource_sheet_id: Optional[int] = None        # 外协加工
    material_sheet_id: Optional[int] = None          # 不锈钢原料下料单
    elec_po_sheet_id: Optional[int] = None           # 电工采购单
    standard_sheet_id: Optional[int] = None          # 标准件清单
    # 设计师推送的附件
    cad_laser_files: list[schemas.AttachmentOut] = []      # CAD激光图纸(order_start_output/sheetpkg)
    outsource_img_files: list[schemas.AttachmentOut] = []  # 外购附图(order_start_output/outsource_img)


@router.get("/purchase/projects", response_model=List[PurchaseProjectRow])
async def purchase_projects(
    year: Optional[str] = None,
    _: models.User = Depends(require_roles("buyer", "buyer_standard", "buyer_outsource", "admin", "manager")),
    db: AsyncSession = Depends(get_db),
):
    """🆕 采购部项目列表：按项目汇总采购所需数据表(引用下载)与设计师推送的图纸附件。
    前端按采购员子角色(外协采购员/标准件采购员)显示对应列；未细分的 buyer 看全部列。"""
    proj_q = select(models.Project).where(models.Project.is_deleted == False)  # noqa: E712
    if year:
        proj_q = proj_q.where(models.Project.code.like(f"{year}-%"))
    res = await db.execute(proj_q.order_by(models.Project.code))
    projects = list(res.scalars().all())
    pids = [p.id for p in projects]
    if not pids:
        return []

    _NAME2KEY = {"外协加工": "outsource", "不锈钢原料下料单": "material",
                 "电工采购单": "elec_po", "标准件清单": "standard"}
    res = await db.execute(select(models.Datasheet).where(
        models.Datasheet.project_id.in_(pids),
        models.Datasheet.name.in_(tuple(_NAME2KEY.keys()))))
    sheet_ids: dict[int, dict[str, int]] = {}
    for d in res.scalars().all():
        sheet_ids.setdefault(d.project_id, {})[_NAME2KEY[d.name]] = d.id

    # 设计师推送的 CAD激光图纸 / 外购附图（排除已作废来源单）
    res = await db.execute(select(models.Attachment)
        .join(models.DeptOrder, models.Attachment.biz_id == models.DeptOrder.id)
        .where(models.Attachment.biz_type == "order_start_output",
               models.Attachment.kind.in_(("sheetpkg", "outsource_img")),
               models.Attachment.project_id.in_(pids),
               models.DeptOrder.status != "voided").order_by(models.Attachment.id))
    cad_by_pid: dict[int, list] = {}
    img_by_pid: dict[int, list] = {}
    for a in res.scalars().all():
        (cad_by_pid if a.kind == "sheetpkg" else img_by_pid).setdefault(
            a.project_id, []).append(schemas.AttachmentOut.model_validate(a))

    # 设计师名：优先一览 __o__设计师（接单回写），否则回退设计任务单负责人
    res = await db.execute(
        select(models.DeptOrder.project_id, models.User.full_name, models.User.username)
        .join(models.User, models.DeptOrder.worker_id == models.User.id)
        .where(models.DeptOrder.dept == "design", models.DeptOrder.project_id.in_(pids)))
    designer_by_pid: dict[int, str] = {}
    for did, fn, un in res.all():
        designer_by_pid.setdefault(did, fn or un)

    rows = []
    for p in projects:
        sm = sheet_ids.get(p.id, {})
        rows.append(PurchaseProjectRow(
            project_id=p.id, code=p.code, name=p.name,
            designer=(p.extra or {}).get("__o__设计师") or designer_by_pid.get(p.id),
            outsource_sheet_id=sm.get("outsource"), material_sheet_id=sm.get("material"),
            elec_po_sheet_id=sm.get("elec_po"), standard_sheet_id=sm.get("standard"),
            cad_laser_files=cad_by_pid.get(p.id, []),
            outsource_img_files=img_by_pid.get(p.id, []),
        ))
    return rows


@router.get("/purchase/inbox", response_model=List[PurchaseInboxRow])
async def purchase_inbox(
    _: models.User = Depends(require_roles("buyer", "buyer_standard", "buyer_outsource")),
    db: AsyncSession = Depends(get_db),
):
    """采购清单收件箱：电工接单上传的采购清单（撤回即消失）。"""
    res = await db.execute(
        select(models.Attachment, models.Project)
        .join(models.Project, models.Attachment.project_id == models.Project.id)
        .join(models.DeptOrder, models.Attachment.biz_id == models.DeptOrder.id)
        .where(
            models.Attachment.biz_type == "order_start_output",
            models.Attachment.kind == "plist",
            models.Project.is_deleted == False,  # noqa: E712
            models.DeptOrder.status != "voided",  # 🆕 #7 作废的电工单不再出现在采购收件箱
        ).order_by(models.Attachment.id.desc()).limit(300)
    )
    rows = []
    for a, p in res.all():
        rows.append(PurchaseInboxRow(
            project_id=p.id, code=p.code, name=p.name,
            source="电工部",
            file=schemas.AttachmentOut.model_validate(a),
            received_at=a.created_at.strftime("%Y-%m-%d %H:%M"),
        ))
    return rows
