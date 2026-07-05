"""🆕 OA 审批：部门字典 + 单据类型字典 + 可配置多级审批链 + 业务/报销/采购三类申请单。

设计要点：
- 部门(Department)与角色分组解耦，管理层手动维护；lead_role 设置后该角色视为"部门负责人"，
  能看到本部门全部申请（不限于自己提交/自己审批环节的）。
- 单据类型(OaDocTypeDict)同样是管理层可维护的字典（增删改+排序+启停），三大类
  （业务/报销/采购）本身是固定分类口径，字典项是这三类下具体的单据类型。
- 审批链(OaApprovalStep)按 部门+单据类型 配置，管理层可动态增删改（Δ第4条"由管理层动态配置"）。
- 提交申请时把当时配置的链路"快照"进 OaRequestStep——之后改配置不影响在途申请，避免审批中途改规则。
"""
from datetime import datetime, timezone, date as _date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, exists
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user, require_roles
from ..notify import push_message

router = APIRouter(prefix="/api/oa", tags=["OA审批"])

_OA_CATEGORY_LABELS = {"business": "业务申请", "reimbursement": "报销申请", "purchase": "采购申请"}


async def _doc_types(db: AsyncSession, enabled_only: bool = False) -> list[models.OaDocTypeDict]:
    q = select(models.OaDocTypeDict)
    if enabled_only:
        q = q.where(models.OaDocTypeDict.enabled == True)  # noqa: E712
    q = q.order_by(models.OaDocTypeDict.sort_order, models.OaDocTypeDict.id)
    return list((await db.execute(q)).scalars().all())


async def _doc_type_by_key(db: AsyncSession, key: str) -> Optional[models.OaDocTypeDict]:
    r = await db.execute(select(models.OaDocTypeDict).where(models.OaDocTypeDict.key == key))
    return r.scalar_one_or_none()


@router.get("/doc-types")
async def list_doc_types(_: models.User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """单据类型字典读取——提交表单/审批流程配置的下拉用（所有登录用户可读）。"""
    return [{"id": d.id, "key": d.key, "category": d.category,
             "category_label": _OA_CATEGORY_LABELS.get(d.category, d.category),
             "label": d.label, "sort_order": d.sort_order, "enabled": d.enabled}
            for d in await _doc_types(db)]


@router.post("/doc-types", response_model=schemas.OaDocTypeOut)
async def create_doc_type(
    body: schemas.OaDocTypeIn,
    current: models.User = Depends(require_roles()),
    db: AsyncSession = Depends(get_db),
):
    dup = await db.execute(select(models.OaDocTypeDict).where(models.OaDocTypeDict.key == body.key))
    if dup.scalar_one_or_none():
        raise HTTPException(409, "该单据类型标识已存在")
    d = models.OaDocTypeDict(key=body.key, category=body.category, label=body.label.strip(),
                             sort_order=body.sort_order, enabled=body.enabled)
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return schemas.OaDocTypeOut.model_validate(d)


@router.put("/doc-types/{did}", response_model=schemas.OaDocTypeOut)
async def update_doc_type(
    did: int, body: schemas.OaDocTypeIn,
    current: models.User = Depends(require_roles()),
    db: AsyncSession = Depends(get_db),
):
    """key 创建后不可改（历史申请/审批链配置按 key 字符串引用）；可改分类/展示名/排序/启用。"""
    d = (await db.execute(select(models.OaDocTypeDict).where(models.OaDocTypeDict.id == did))).scalar_one_or_none()
    if not d:
        raise HTTPException(404, "单据类型不存在")
    if body.key != d.key:
        raise HTTPException(400, "单据类型标识创建后不可修改")
    d.category = body.category; d.label = body.label.strip()
    d.sort_order = body.sort_order; d.enabled = body.enabled
    await db.commit()
    await db.refresh(d)
    return schemas.OaDocTypeOut.model_validate(d)


@router.delete("/doc-types/{did}", response_model=schemas.Msg)
async def delete_doc_type(
    did: int,
    current: models.User = Depends(require_roles()),
    db: AsyncSession = Depends(get_db),
):
    d = (await db.execute(select(models.OaDocTypeDict).where(models.OaDocTypeDict.id == did))).scalar_one_or_none()
    if not d:
        raise HTTPException(404, "单据类型不存在")
    used_req = await db.execute(select(func.count(models.OaRequest.id)).where(models.OaRequest.doc_type == d.key))
    used_chain = await db.execute(select(func.count(models.OaApprovalStep.id)).where(models.OaApprovalStep.doc_type == d.key))
    if used_req.scalar() or used_chain.scalar():
        raise HTTPException(400, "该单据类型已有申请记录或审批流程配置，不能删除；可改为「停用」")
    await db.delete(d)
    await db.commit()
    return schemas.Msg(message="已删除该单据类型")


# ==================== 部门字典 ====================
@router.get("/departments", response_model=list[schemas.DepartmentOut])
async def list_departments(
    enabled_only: bool = Query(False),
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(models.Department)
    if enabled_only:
        q = q.where(models.Department.enabled == True)  # noqa: E712
    q = q.order_by(models.Department.sort_order, models.Department.id)
    return [schemas.DepartmentOut.model_validate(d) for d in (await db.execute(q)).scalars().all()]


@router.post("/departments", response_model=schemas.DepartmentOut)
async def create_department(
    body: schemas.DepartmentIn,
    current: models.User = Depends(require_roles()),
    db: AsyncSession = Depends(get_db),
):
    name = body.name.strip()
    dup = await db.execute(select(models.Department).where(models.Department.name == name))
    if dup.scalar_one_or_none():
        raise HTTPException(409, "该部门已存在")
    d = models.Department(name=name, lead_role=body.lead_role or None,
                          sort_order=body.sort_order, enabled=body.enabled)
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return schemas.DepartmentOut.model_validate(d)


@router.put("/departments/{did}", response_model=schemas.DepartmentOut)
async def update_department(
    did: int, body: schemas.DepartmentIn,
    current: models.User = Depends(require_roles()),
    db: AsyncSession = Depends(get_db),
):
    d = (await db.execute(select(models.Department).where(models.Department.id == did))).scalar_one_or_none()
    if not d:
        raise HTTPException(404, "部门不存在")
    name = body.name.strip()
    dup = await db.execute(select(models.Department).where(models.Department.name == name, models.Department.id != did))
    if dup.scalar_one_or_none():
        raise HTTPException(409, "该部门已存在")
    d.name = name; d.lead_role = body.lead_role or None
    d.sort_order = body.sort_order; d.enabled = body.enabled
    await db.commit()
    await db.refresh(d)
    return schemas.DepartmentOut.model_validate(d)


@router.delete("/departments/{did}", response_model=schemas.Msg)
async def delete_department(
    did: int,
    current: models.User = Depends(require_roles()),
    db: AsyncSession = Depends(get_db),
):
    d = (await db.execute(select(models.Department).where(models.Department.id == did))).scalar_one_or_none()
    if not d:
        raise HTTPException(404, "部门不存在")
    used = await db.execute(select(func.count(models.OaRequest.id)).where(models.OaRequest.department_id == did))
    if used.scalar():
        raise HTTPException(400, "该部门已有申请记录，不能删除；可改为「停用」")
    await db.execute(models.OaApprovalStep.__table__.delete().where(models.OaApprovalStep.department_id == did))
    await db.delete(d)
    await db.commit()
    return schemas.Msg(message="已删除该部门")


# ==================== 审批链配置 ====================
@router.get("/chains", response_model=list[schemas.OaApprovalStepOut])
async def list_chain_steps(
    department_id: int = Query(...),
    doc_type: str = Query(...),
    current: models.User = Depends(require_roles()),
    db: AsyncSession = Depends(get_db),
):
    q = (select(models.OaApprovalStep)
         .where(models.OaApprovalStep.department_id == department_id, models.OaApprovalStep.doc_type == doc_type)
         .order_by(models.OaApprovalStep.step_order))
    return [schemas.OaApprovalStepOut.model_validate(s) for s in (await db.execute(q)).scalars().all()]


async def _role_name(db: AsyncSession, code: str) -> str:
    r = await db.execute(select(models.Role.name).where(models.Role.code == code))
    return r.scalar_one_or_none() or code


@router.post("/chains", response_model=schemas.OaApprovalStepOut)
async def create_chain_step(
    body: schemas.OaApprovalStepIn,
    current: models.User = Depends(require_roles()),
    db: AsyncSession = Depends(get_db),
):
    if not await _doc_type_by_key(db, body.doc_type):
        raise HTTPException(400, "未知单据类型")
    dup = await db.execute(select(models.OaApprovalStep).where(
        models.OaApprovalStep.department_id == body.department_id,
        models.OaApprovalStep.doc_type == body.doc_type,
        models.OaApprovalStep.step_order == body.step_order))
    if dup.scalar_one_or_none():
        raise HTTPException(409, "该部门/单据类型下已有相同顺序的步骤")
    label = (body.step_label or "").strip() or await _role_name(db, body.approver_role)
    s = models.OaApprovalStep(department_id=body.department_id, doc_type=body.doc_type,
                              step_order=body.step_order, approver_role=body.approver_role,
                              step_label=label, enabled=body.enabled)
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return schemas.OaApprovalStepOut.model_validate(s)


@router.put("/chains/{sid}", response_model=schemas.OaApprovalStepOut)
async def update_chain_step(
    sid: int, body: schemas.OaApprovalStepIn,
    current: models.User = Depends(require_roles()),
    db: AsyncSession = Depends(get_db),
):
    s = (await db.execute(select(models.OaApprovalStep).where(models.OaApprovalStep.id == sid))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "步骤不存在")
    dup = await db.execute(select(models.OaApprovalStep).where(
        models.OaApprovalStep.department_id == body.department_id,
        models.OaApprovalStep.doc_type == body.doc_type,
        models.OaApprovalStep.step_order == body.step_order,
        models.OaApprovalStep.id != sid))
    if dup.scalar_one_or_none():
        raise HTTPException(409, "该部门/单据类型下已有相同顺序的步骤")
    label = (body.step_label or "").strip() or await _role_name(db, body.approver_role)
    s.department_id = body.department_id; s.doc_type = body.doc_type
    s.step_order = body.step_order; s.approver_role = body.approver_role
    s.step_label = label; s.enabled = body.enabled
    await db.commit()
    await db.refresh(s)
    return schemas.OaApprovalStepOut.model_validate(s)


@router.delete("/chains/{sid}", response_model=schemas.Msg)
async def delete_chain_step(
    sid: int,
    current: models.User = Depends(require_roles()),
    db: AsyncSession = Depends(get_db),
):
    """删除链路配置步骤。已提交的在途/历史申请是各自快照(OaRequestStep)，不受影响。"""
    s = (await db.execute(select(models.OaApprovalStep).where(models.OaApprovalStep.id == sid))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "步骤不存在")
    await db.delete(s)
    await db.commit()
    return schemas.Msg(message="已删除该审批步骤")


# ==================== 申请单 ====================
async def _next_oa_no(db: AsyncSession) -> str:
    prefix = f"OA{_date.today().strftime('%Y%m%d')}-"
    r = await db.execute(select(func.count(func.distinct(models.OaRequest.request_no)))
                         .where(models.OaRequest.request_no.like(f"{prefix}%")))
    n = (r.scalar() or 0) + 1
    return f"{prefix}{n:03d}"


async def _fetch_request(db: AsyncSession, rid: int) -> Optional[models.OaRequest]:
    # populate_existing：强制用本次查询结果覆盖已在 identity map 里的对象（含关系），
    # 避免同一会话里先查后改再查时，关系属性（如 actor）仍停留在改之前的旧值。
    q = (select(models.OaRequest)
         .options(selectinload(models.OaRequest.steps), selectinload(models.OaRequest.cc_entries))
         .where(models.OaRequest.id == rid)
         .execution_options(populate_existing=True))
    return (await db.execute(q)).scalar_one_or_none()


def _can_view(req: models.OaRequest, current: models.User) -> bool:
    if current.has_role("admin", "manager"):
        return True
    if req.requester_id == current.id:
        return True
    if any(s.approver_role in current.role_codes for s in req.steps):
        return True
    if req.department and req.department.lead_role and req.department.lead_role in current.role_codes:
        return True
    if any(c.user_id == current.id for c in req.cc_entries):   # 🆕 抄送人可查看
        return True
    return False


async def _req_out(db: AsyncSession, req: models.OaRequest, current: models.User) -> schemas.OaRequestOut:
    steps_sorted = sorted(req.steps, key=lambda s: s.step_order)
    cur_step = next((s for s in steps_sorted if s.step_order == req.current_step_order), None)
    can_approve = bool(
        req.status == "pending" and cur_step is not None and cur_step.status == "pending"
        and current.has_role(cur_step.approver_role, "admin", "manager")
    )
    can_withdraw = bool(
        req.status == "pending" and req.requester_id == current.id
        and steps_sorted and steps_sorted[0].step_order == req.current_step_order
        and steps_sorted[0].status == "pending"
    )
    can_mark_paid = bool(req.status == "pending_payment" and current.has_role("finance", "admin", "manager"))
    related_no = None
    if req.related_request_id:
        r = await db.execute(select(models.OaRequest.request_no).where(models.OaRequest.id == req.related_request_id))
        related_no = r.scalar_one_or_none()
    return schemas.OaRequestOut(
        id=req.id, request_no=req.request_no, category=req.category, doc_type=req.doc_type,
        department_id=req.department_id, department_name=req.department.name if req.department else "",
        requester_id=req.requester_id,
        requester_name=(req.requester.full_name or req.requester.username) if req.requester else "",
        title=req.title, amount=req.amount, detail=req.detail or {},
        related_request_id=req.related_request_id, related_request_no=related_no,
        status=req.status, current_step_order=req.current_step_order,
        settle_amount=req.settle_amount, settle_note=req.settle_note, reject_reason=req.reject_reason,
        created_at=req.created_at, updated_at=req.updated_at,
        steps=[schemas.OaRequestStepOut(
            id=s.id, step_order=s.step_order, approver_role=s.approver_role, step_label=s.step_label,
            status=s.status, acted_by=s.acted_by,
            actor_name=(s.actor.full_name or s.actor.username) if s.actor else None,
            acted_at=s.acted_at, note=s.note,
        ) for s in steps_sorted],
        cc_users=[schemas.OaCcUserOut(
            id=c.user_id, name=(c.user.full_name or c.user.username) if c.user else f"#{c.user_id}",
        ) for c in req.cc_entries],
        can_approve=can_approve, can_withdraw=can_withdraw, can_mark_paid=can_mark_paid,
    )


@router.get("/cc-candidates")
async def list_cc_candidates(
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """🆕 抄送人可选名单：在职用户（排除 admin 隐身账号）。
    任何登录用户提交 OA 申请时选抄送人用，所以不限角色。"""
    res = await db.execute(
        select(models.User).where(models.User.is_active == True).order_by(models.User.id))  # noqa: E712
    return [{"id": u.id, "name": u.full_name or u.username}
            for u in res.scalars().all() if not u.has_role("admin")]


@router.post("/requests", response_model=schemas.OaRequestOut)
async def create_request(
    body: schemas.OaRequestCreate,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    dt = await _doc_type_by_key(db, body.doc_type)
    if not dt or not dt.enabled:
        raise HTTPException(400, "未知单据类型或已停用")
    category, doc_label = dt.category, dt.label
    dept = (await db.execute(select(models.Department).where(models.Department.id == body.department_id))).scalar_one_or_none()
    if not dept or not dept.enabled:
        raise HTTPException(400, "部门不存在或已停用")
    steps_cfg = (await db.execute(
        select(models.OaApprovalStep)
        .where(models.OaApprovalStep.department_id == dept.id, models.OaApprovalStep.doc_type == body.doc_type,
               models.OaApprovalStep.enabled == True)  # noqa: E712
        .order_by(models.OaApprovalStep.step_order)
    )).scalars().all()
    if not steps_cfg:
        raise HTTPException(400, f"「{dept.name}」的「{doc_label}」尚未配置审批流程，请联系管理层在【审批流程设置】里配置")
    if body.related_request_id:
        rel = await db.execute(select(models.OaRequest.id).where(models.OaRequest.id == body.related_request_id))
        if not rel.scalar_one_or_none():
            raise HTTPException(400, "关联的业务申请不存在")
    req_no = await _next_oa_no(db)
    req = models.OaRequest(
        request_no=req_no, category=category, doc_type=body.doc_type, department_id=dept.id,
        requester_id=current.id, title=(body.title or "").strip() or doc_label, amount=body.amount,
        detail=body.detail or {}, related_request_id=body.related_request_id,
        status="pending", current_step_order=steps_cfg[0].step_order,
    )
    db.add(req)
    await db.flush()
    for s in steps_cfg:
        db.add(models.OaRequestStep(request_id=req.id, step_order=s.step_order,
                                    approver_role=s.approver_role, step_label=s.step_label, status="pending"))
    # 🆕 抄送人：去重、排除提交人自己（本来就能看）、只保留真实在职用户
    cc_ids: list[int] = []
    if body.cc_user_ids:
        uniq = [uid for uid in dict.fromkeys(body.cc_user_ids) if uid != current.id]
        if uniq:
            valid = (await db.execute(select(models.User.id).where(
                models.User.id.in_(uniq), models.User.is_active == True))).scalars().all()  # noqa: E712
            cc_ids = list(valid)
            for uid in cc_ids:
                db.add(models.OaRequestCc(request_id=req.id, user_id=uid))
    await db.commit()
    req = await _fetch_request(db, req.id)
    await push_message(db, to_role=steps_cfg[0].approver_role, kind="info",
                       text=f"【OA审批】{current.full_name or current.username} 提交了「{doc_label}」({req_no})待你审批",
                       biz_type="oa_request", biz_id=req.id)
    # 🆕 抄送通知：抄送人不参与审批，仅告知有一份申请抄送给ta
    for uid in cc_ids:
        await push_message(db, to_user_id=uid, kind="info",
                           text=f"【OA抄送】{current.full_name or current.username} 抄送给你一份「{doc_label}」({req_no})",
                           biz_type="oa_request", biz_id=req.id)
    return await _req_out(db, req, current)


async def _led_department_ids(db: AsyncSession, current: models.User) -> list[int]:
    if not current.role_codes:
        return []
    r = await db.execute(select(models.Department.id).where(models.Department.lead_role.in_(current.role_codes)))
    return [x for (x,) in r.all()]


@router.get("/requests", response_model=list[schemas.OaRequestOut])
async def list_requests(
    scope: str = Query("mine", description="mine/pending_me/acted_by_me/cc_me/dept/all"),
    department_id: Optional[int] = Query(None),
    doc_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(models.OaRequest).options(
        selectinload(models.OaRequest.steps), selectinload(models.OaRequest.cc_entries))
    StepT = models.OaRequestStep
    if scope == "pending_me":
        cond = exists().where(StepT.request_id == models.OaRequest.id,
                              StepT.step_order == models.OaRequest.current_step_order,
                              StepT.status == "pending",
                              StepT.approver_role.in_(current.role_codes or [""]))
        q = q.where(models.OaRequest.status == "pending", cond)
    elif scope == "acted_by_me":
        cond = exists().where(StepT.request_id == models.OaRequest.id, StepT.acted_by == current.id)
        q = q.where(cond)
    elif scope == "cc_me":   # 🆕 抄送我的：当前登录人是抄送人的申请
        CcT = models.OaRequestCc
        cond = exists().where(CcT.request_id == models.OaRequest.id, CcT.user_id == current.id)
        q = q.where(cond)
    elif scope == "dept":
        led_ids = await _led_department_ids(db, current)
        if not led_ids:
            return []
        q = q.where(models.OaRequest.department_id.in_(led_ids))
    elif scope == "all":
        if not current.has_role("admin", "manager"):
            raise HTTPException(403, "无权查看全部申请")
    else:
        q = q.where(models.OaRequest.requester_id == current.id)
    if department_id:
        q = q.where(models.OaRequest.department_id == department_id)
    if doc_type:
        q = q.where(models.OaRequest.doc_type == doc_type)
    if status:
        q = q.where(models.OaRequest.status == status)
    q = q.order_by(models.OaRequest.created_at.desc()).limit(300)
    rows = (await db.execute(q)).unique().scalars().all()
    return [await _req_out(db, r, current) for r in rows]


@router.get("/requests/{rid}", response_model=schemas.OaRequestOut)
async def get_request(
    rid: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    req = await _fetch_request(db, rid)
    if not req:
        raise HTTPException(404, "申请不存在")
    if not _can_view(req, current):
        raise HTTPException(403, "无权查看该申请")
    return await _req_out(db, req, current)


@router.put("/requests/{rid}/approve", response_model=schemas.OaRequestOut)
async def approve_request(
    rid: int, body: schemas.OaActionIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    req = await _fetch_request(db, rid)
    if not req:
        raise HTTPException(404, "申请不存在")
    if req.status != "pending":
        raise HTTPException(400, "该申请已结束，无法操作")
    steps_sorted = sorted(req.steps, key=lambda s: s.step_order)
    cur_step = next((s for s in steps_sorted if s.step_order == req.current_step_order), None)
    if not cur_step or cur_step.status != "pending":
        raise HTTPException(400, "当前没有待处理的步骤")
    if not current.has_role(cur_step.approver_role, "admin", "manager"):
        raise HTTPException(403, "无权审批此步骤")
    cur_step.status = "approved"; cur_step.acted_by = current.id
    cur_step.acted_at = datetime.now(timezone.utc); cur_step.note = (body.note or "").strip() or None
    if body.settle_amount is not None:
        req.settle_amount = body.settle_amount
    next_step = next((s for s in steps_sorted if s.step_order > cur_step.step_order), None)
    # 🆕 最后一步是财务审批的（多见于报销类），先进"待付款"，财务还要再单独点"标记已付款"，
    # 不能审批通过=已付款——审批只代表"同意报销"，钱有没有真的付出去是另一件事，得分开记。
    if next_step:
        req.current_step_order = next_step.step_order
    elif cur_step.approver_role == "finance":
        req.status = "pending_payment"; req.current_step_order = None
    else:
        req.status = "approved"; req.current_step_order = None
    await db.commit()
    if next_step:
        await push_message(db, to_role=next_step.approver_role, kind="info",
                           text=f"【OA审批】{req.request_no} 待你审批", biz_type="oa_request", biz_id=req.id)
    elif req.status == "pending_payment":
        await push_message(db, to_user_id=req.requester_id, kind="info",
                           text=f"【OA审批】你的申请 {req.request_no} 已审批通过，等待财务付款", biz_type="oa_request", biz_id=req.id)
    else:
        await push_message(db, to_user_id=req.requester_id, kind="info",
                           text=f"【OA审批】你的申请 {req.request_no} 已全部审批通过", biz_type="oa_request", biz_id=req.id)
    req = await _fetch_request(db, rid)
    return await _req_out(db, req, current)


@router.put("/requests/{rid}/mark-paid", response_model=schemas.OaRequestOut)
async def mark_paid(
    rid: int,
    current: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    """财务把「待付款」的申请标记为已付款——跟审批通过分开操作，避免"批了=钱到账"的误解。"""
    req = await _fetch_request(db, rid)
    if not req:
        raise HTTPException(404, "申请不存在")
    if req.status != "pending_payment":
        raise HTTPException(400, "该申请当前不是待付款状态")
    req.status = "approved"
    await db.commit()
    await push_message(db, to_user_id=req.requester_id, kind="info",
                       text=f"【OA审批】你的申请 {req.request_no} 财务已付款", biz_type="oa_request", biz_id=req.id)
    req = await _fetch_request(db, rid)
    return await _req_out(db, req, current)


@router.put("/requests/{rid}/reject", response_model=schemas.OaRequestOut)
async def reject_request(
    rid: int, body: schemas.OaRejectIn,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    req = await _fetch_request(db, rid)
    if not req:
        raise HTTPException(404, "申请不存在")
    if req.status != "pending":
        raise HTTPException(400, "该申请已结束，无法操作")
    steps_sorted = sorted(req.steps, key=lambda s: s.step_order)
    cur_step = next((s for s in steps_sorted if s.step_order == req.current_step_order), None)
    if not cur_step or cur_step.status != "pending":
        raise HTTPException(400, "当前没有待处理的步骤")
    if not current.has_role(cur_step.approver_role, "admin", "manager"):
        raise HTTPException(403, "无权审批此步骤")
    cur_step.status = "rejected"; cur_step.acted_by = current.id
    cur_step.acted_at = datetime.now(timezone.utc); cur_step.note = body.reason.strip()
    req.status = "rejected"; req.reject_reason = body.reason.strip(); req.current_step_order = None
    await db.commit()
    await push_message(db, to_user_id=req.requester_id, kind="warn",
                       text=f"【OA审批】你的申请 {req.request_no} 被驳回：{body.reason.strip()[:60]}",
                       biz_type="oa_request", biz_id=req.id)
    req = await _fetch_request(db, rid)
    return await _req_out(db, req, current)


@router.put("/requests/{rid}/withdraw", response_model=schemas.Msg)
async def withdraw_request(
    rid: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    req = await _fetch_request(db, rid)
    if not req:
        raise HTTPException(404, "申请不存在")
    if req.requester_id != current.id and not current.has_role("admin", "manager"):
        raise HTTPException(403, "只能撤回自己提交的申请")
    steps_sorted = sorted(req.steps, key=lambda s: s.step_order)
    if req.status != "pending" or not steps_sorted or steps_sorted[0].step_order != req.current_step_order \
            or steps_sorted[0].status != "pending":
        raise HTTPException(400, "已进入审批流程，无法撤回")
    req.status = "withdrawn"; req.current_step_order = None
    await db.commit()
    return schemas.Msg(message="已撤回该申请")


# ==================== 财务汇总报表 ====================
@router.get("/reports/summary", response_model=list[schemas.OaSummaryRow])
async def oa_summary(
    current: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    """按部门+单据类型汇总已批准申请（金额取核定金额，未核定则取申请金额）。"""
    q = (
        select(
            models.OaRequest.department_id, models.Department.name, models.OaRequest.doc_type,
            func.count(models.OaRequest.id),
            func.sum(func.coalesce(models.OaRequest.settle_amount, models.OaRequest.amount, 0.0)),
        )
        .join(models.Department, models.Department.id == models.OaRequest.department_id)
        .where(models.OaRequest.status == "approved")
        # Department.sort_order 必须进 GROUP BY：Postgres 严格要求 ORDER BY 的列出现在 GROUP BY 或聚合里，
        # 否则报 GroupingError 500（SQLite 宽松不报，沙箱测不出）。sort_order 与部门 1:1，不改变分组结果。
        .group_by(models.OaRequest.department_id, models.Department.name,
                  models.Department.sort_order, models.OaRequest.doc_type)
        .order_by(models.Department.sort_order)
    )
    rows = (await db.execute(q)).all()
    return [schemas.OaSummaryRow(department_id=r[0], department_name=r[1], doc_type=r[2],
                                 count=r[3] or 0, amount=round(r[4] or 0, 2)) for r in rows]
