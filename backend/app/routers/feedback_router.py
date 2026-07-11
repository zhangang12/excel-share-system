"""🆕 v3 M13 生产问题反馈流（§十四）：
装配工人(本人在手项目)提交 → 生产主管审批(通过→待设计接收/驳回) →
该项目设计师接收(存档)/驳回。状态：pending_pm→pending_design→archived/rejected_by_*。

设计师反查：用项目当前 design 任务的 worker_id（非姓名匹配，修正原型缺陷）；
无 design 任务时降级推 design_lead 池。
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Form, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user, require_roles
from ..menus import user_can_view_detail
from ..notify import push_message
from ..utils import write_audit
from .attachments_router import save_upload

router = APIRouter(prefix="/api/feedbacks", tags=["问题反馈"])


def _uname(u: Optional[models.User]) -> Optional[str]:
    return (u.full_name or u.username) if u else None


async def _designer_uid(db: AsyncSession, project_id: int) -> Optional[int]:
    """项目当前设计任务的负责人 uid（按 worker_id 反查，非姓名）。"""
    r = await db.execute(
        select(models.DeptOrder).where(
            models.DeptOrder.project_id == project_id,
            models.DeptOrder.dept == "design",
            models.DeptOrder.status != "voided",
        ).order_by(models.DeptOrder.id.desc())
    )
    o = r.scalars().first()
    return o.worker_id if o else None


async def _fb_or_404(db: AsyncSession, fid: int) -> models.Feedback:
    r = await db.execute(select(models.Feedback).where(models.Feedback.id == fid))
    fb = r.scalar_one_or_none()
    if not fb:
        raise HTTPException(404, "反馈不存在")
    return fb


async def _rows(db: AsyncSession, items: list[models.Feedback]) -> list[schemas.FeedbackRow]:
    # 🆕 #193 反馈附图（biz_type=feedback）
    imgs: dict[int, list[dict]] = {}
    ids = [f.id for f in items]
    if ids:
        ar = await db.execute(select(models.Attachment).where(
            models.Attachment.biz_type == "feedback", models.Attachment.biz_id.in_(ids))
            .order_by(models.Attachment.id))
        for a in ar.scalars().all():
            imgs.setdefault(a.biz_id, []).append({"id": a.id, "name": a.name})
    return [schemas.FeedbackRow(
        id=f.id, project_id=f.project_id,
        code=f.project.code if f.project else "", name=f.project.name if f.project else "",
        content=f.content, status=f.status,
        created_by_name=_uname(f.creator), designer_name=_uname(f.designer),
        created_at=f.created_at, images=imgs.get(f.id, []),
    ) for f in items]


@router.get("", response_model=List[schemas.FeedbackRow])
async def list_feedbacks(
    project_id: Optional[int] = Query(None),
    mine: bool = Query(False),
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """协作 tab 存档（按 project_id）/ 工作台卡片（mine=按角色取待我处理的）。"""
    q = select(models.Feedback)
    if project_id:
        # 🆕 越权修复(#31)：协作存档(按 project_id)与项目详单同源闸门——
        # 收紧角色(销售/电工/装配/售后)无详单权限，不得读取项目反馈内容
        if not user_can_view_detail(current):
            raise HTTPException(403, "你没有项目详单权限")
        q = q.where(models.Feedback.project_id == project_id)
    codes = current.role_codes
    if mine:
        # 多角色取并集：合并各角色「待我处理」的条件（OR）
        conds = []
        if "assembler" in codes:
            conds.append(models.Feedback.created_by == current.id)
        if "pm_lead" in codes:
            conds.append(models.Feedback.status == "pending_pm")
        if "designer" in codes:
            conds.append(and_(models.Feedback.status == "pending_design",
                              models.Feedback.designer_uid == current.id))
        if "design_lead" in codes:
            # 🆕 #29 设计负责人看「待接收但无在岗设计师(死信)」的反馈，可指派
            conds.append(and_(models.Feedback.status == "pending_design",
                              models.Feedback.designer_uid.is_(None)))
        if not conds:
            return []
        q = q.where(or_(*conds))
    r = await db.execute(q.order_by(models.Feedback.id.desc()).limit(300))
    return await _rows(db, list(r.scalars().all()))


@router.get("/projects", response_model=List[schemas.FeedbackProjOption])
async def my_projects(
    current: models.User = Depends(require_roles("assembler")),
    db: AsyncSession = Depends(get_db),
):
    """在手项目列表：返回所有进行中的项目供提交问题反馈时选择。"""
    r = await db.execute(
        select(models.Project.id, models.Project.code, models.Project.name)
        .where(models.Project.status == "进行中", models.Project.is_deleted == False)
        .order_by(models.Project.code)
    )
    return [schemas.FeedbackProjOption(id=i, code=c, name=n) for i, c, n in r.all()]


@router.post("", response_model=schemas.Msg)
async def create_feedback(
    project_id: int = Form(...),
    content: str = Form(...),
    files: List[UploadFile] = File(default=[]),
    current: models.User = Depends(require_roles("assembler")),
    db: AsyncSession = Depends(get_db),
):
    """装配工人提交问题反馈（限本人在手项目）→ 待生产主管审批。
    🆕 #193 改 multipart：可附现场照片(多张,选填)，主管审批/设计师接收时可查看。"""
    if not content.strip():
        raise HTTPException(400, "请填写问题内容")
    # 校验是本人在手项目
    r = await db.execute(select(models.DeptOrder).where(
        models.DeptOrder.project_id == project_id,
        models.DeptOrder.dept == "produce",
        models.DeptOrder.worker_id == current.id,
        models.DeptOrder.status != "voided",
    ))
    if not r.scalars().first():
        raise HTTPException(403, "只能对自己在手的项目提交反馈")
    rp = await db.execute(select(models.Project).where(models.Project.id == project_id))
    proj = rp.scalar_one()
    p_code, p_name = proj.code, proj.name
    fb = models.Feedback(project_id=project_id, content=content.strip(),
                         status="pending_pm", created_by=current.id)
    db.add(fb)
    await db.flush()
    fid = fb.id
    n_img = 0
    for f in files or []:
        if not f or not f.filename:
            continue
        await save_upload(db, f, biz_type="feedback", biz_id=fid,
                          kind="img", project_id=project_id, user=current)
        n_img += 1
    await db.commit()
    await push_message(db, to_role="pm_lead", kind="info",
                       text=f"【问题反馈待审批】{p_code} {p_name}：{content[:24]}…"
                            + (f"（附图{n_img}张）" if n_img else ""),
                       biz_type="feedback", biz_id=fid)
    await write_audit(db, user=current, action="create", target_type="feedback", target_id=fid)
    return schemas.Msg(message="已提交，等待生产主管审批")


@router.post("/{fid}/pm-approve", response_model=schemas.Msg)
async def pm_approve(
    fid: int,
    current: models.User = Depends(require_roles("pm_lead")),
    db: AsyncSession = Depends(get_db),
):
    """生产主管通过 → 待设计接收（按项目设计师 worker_id 推送）。"""
    fb = await _fb_or_404(db, fid)
    if fb.status != "pending_pm":
        raise HTTPException(400, "该反馈不在待主管审批状态")
    p_code, p_name, content = fb.project.code, fb.project.name, fb.content
    designer_uid = await _designer_uid(db, fb.project_id)
    fb.status = "pending_design"
    fb.designer_uid = designer_uid
    fb.appr_by = current.id
    await db.commit()
    if designer_uid:
        await push_message(db, to_user_id=designer_uid, kind="info",
                           text=f"【问题反馈待接收】{p_code} {p_name}：{content[:24]}…，请接收或驳回。",
                           biz_type="feedback", biz_id=fid)
    else:
        await push_message(db, to_role="design_lead", kind="warn",
                           text=f"【问题反馈】{p_code} 无在岗设计师，请设计负责人指派处理：{content[:24]}…",
                           biz_type="feedback", biz_id=fid)
    await write_audit(db, user=current, action="pm_approve", target_type="feedback", target_id=fid)
    return schemas.Msg(message="已通过，已回馈设计师")


@router.post("/{fid}/pm-reject", response_model=schemas.Msg)
async def pm_reject(
    fid: int,
    current: models.User = Depends(require_roles("pm_lead")),
    db: AsyncSession = Depends(get_db),
):
    fb = await _fb_or_404(db, fid)
    if fb.status != "pending_pm":
        raise HTTPException(400, "该反馈不在待主管审批状态")
    p_code, created_by = fb.project.code, fb.created_by
    fb.status = "rejected_by_pm"
    fb.appr_by = current.id
    await db.commit()
    if created_by:
        await push_message(db, to_user_id=created_by, kind="warn",
                           text=f"【反馈被驳回】{p_code} 你的问题反馈被生产主管驳回。",
                           biz_type="feedback", biz_id=fid)
    await write_audit(db, user=current, action="pm_reject", target_type="feedback", target_id=fid)
    return schemas.Msg(message="已驳回")


@router.post("/{fid}/assign", response_model=schemas.Msg)
async def assign_feedback(
    fid: int,
    worker_id: int = Query(...),
    current: models.User = Depends(require_roles("design_lead")),
    db: AsyncSession = Depends(get_db),
):
    """🆕 #29 设计负责人指派/改派 待接收反馈给某设计师（解决无在岗设计师的死信）。"""
    fb = await _fb_or_404(db, fid)
    if fb.status != "pending_design":
        raise HTTPException(400, "仅待设计接收的反馈可指派")
    r = await db.execute(select(models.User).where(models.User.id == worker_id))
    u = r.scalar_one_or_none()
    if not u or not u.has_role("designer", "design_lead"):
        raise HTTPException(400, "只能指派给设计人员")
    p_code, p_name, content = fb.project.code, fb.project.name, fb.content
    fb.designer_uid = worker_id
    await db.commit()
    await push_message(db, to_user_id=worker_id, kind="info",
                       text=f"【问题反馈待接收】{p_code} {p_name}：{content[:24]}…（设计负责人指派），请接收或驳回。",
                       biz_type="feedback", biz_id=fid)
    await write_audit(db, user=current, action="assign", target_type="feedback", target_id=fid)
    return schemas.Msg(message="已指派给设计师")


@router.post("/{fid}/design-accept", response_model=schemas.Msg)
async def design_accept(
    fid: int,
    current: models.User = Depends(require_roles("designer")),
    db: AsyncSession = Depends(get_db),
):
    """设计师接收 → 已存档（存档到项目协作 tab 问题反馈列）。"""
    fb = await _fb_or_404(db, fid)
    if fb.status != "pending_design":
        raise HTTPException(400, "该反馈不在待设计接收状态")
    if fb.designer_uid and fb.designer_uid != current.id:
        raise HTTPException(403, "只能处理回馈给你的反馈")
    fb.status = "archived"
    await db.commit()
    await write_audit(db, user=current, action="design_accept", target_type="feedback", target_id=fid)
    return schemas.Msg(message="已接收并存档到项目问题反馈")


@router.post("/{fid}/design-reject", response_model=schemas.Msg)
async def design_reject(
    fid: int,
    current: models.User = Depends(require_roles("designer")),
    db: AsyncSession = Depends(get_db),
):
    fb = await _fb_or_404(db, fid)
    if fb.status != "pending_design":
        raise HTTPException(400, "该反馈不在待设计接收状态")
    if fb.designer_uid and fb.designer_uid != current.id:
        raise HTTPException(403, "只能处理回馈给你的反馈")
    p_code, content = fb.project.code, fb.content
    fb.status = "rejected_by_design"
    await db.commit()
    await push_message(db, to_role="pm_lead", kind="warn",
                       text=f"【设计驳回反馈】{p_code} 设计师驳回了问题反馈：{content[:24]}…",
                       biz_type="feedback", biz_id=fid)
    await write_audit(db, user=current, action="design_reject", target_type="feedback", target_id=fid)
    return schemas.Msg(message="已驳回")
