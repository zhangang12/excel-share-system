"""🆕 OA 审批：部门字典 + 单据类型字典 + 可配置多级审批链 + 业务/报销/采购三类申请单。

设计要点：
- 部门(Department)与角色分组解耦，管理层手动维护；lead_role 设置后该角色视为"部门负责人"，
  能看到本部门全部申请（不限于自己提交/自己审批环节的）。
- 单据类型(OaDocTypeDict)同样是管理层可维护的字典（增删改+排序+启停），三大类
  （业务/报销/采购）本身是固定分类口径，字典项是这三类下具体的单据类型。
- 审批链(OaApprovalStep)按 部门+单据类型 配置，管理层可动态增删改（Δ第4条"由管理层动态配置"）。
- 提交申请时把当时配置的链路"快照"进 OaRequestStep——之后改配置不影响在途申请，避免审批中途改规则。

🆕 指定到人 + 代理人（2026-08-06）
--------------------------------
一步可以配「角色」也可以配「具体某个人」（`approver_user_id`）。为什么要有：
一个人挂了销售部+财务部+采购部，按角色配的话凡是挂着这个角色的人都能收到单，
很多本该别人批的也落到他待办里。

指定到人的代价是**人不在单子就卡死**，所以必须配套代理人（`User.deputy_uid`）：
本人超过 `OA_DEPUTY_TAKEOVER_DAYS` 天没处理，代理人也能批——**本人仍然能批**，
是"多一个人能批"而不是"转移给别人"。按角色配的步骤谁在岗谁批，天然不会卡，
所以代理人只对指定到人的步骤生效。

计时从 `OaRequestStep.activated_at`（这一步真正轮到的时刻）起算，不是建单时刻——
一张单在前面几步排了两周，不该一轮到就直接进代理人待办。
"""
from datetime import datetime, timedelta, timezone, date as _date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, select, func, exists, or_, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pydantic import BaseModel

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user, require_roles
from ..notify import push_message
from ..utils import write_audit
from .attachments_router import delete_attachment_file

router = APIRouter(prefix="/api/oa", tags=["OA审批"])

_OA_CATEGORY_LABELS = {"business": "业务申请", "reimbursement": "报销申请", "purchase": "采购申请"}

# 指定审批人多少天没处理，代理人可以接手。业务定的口径是 3 天。
OA_DEPUTY_TAKEOVER_DAYS = 3


def _deputy_ready_before() -> datetime:
    """代理人可接手的门槛时刻：activated_at 早于这个时刻的步骤才轮得到代理人。"""
    return datetime.now(timezone.utc) - timedelta(days=OA_DEPUTY_TAKEOVER_DAYS)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """⚠️ 列声明的是 DateTime(timezone=True)，但**只有 Postgres 会还回带时区的值**；
    开发和测试用的 SQLite 还回来是 naive 的，直接比较会 TypeError。
    这里统一按 UTC 补上时区——存进去的本来就是 UTC。"""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _my_principal_ids(db: AsyncSession, current: models.User) -> list[int]:
    """我是谁的代理人 —— 返回把我设成 deputy 的那些人的 id。"""
    rows = (await db.execute(
        select(models.User.id).where(models.User.deputy_uid == current.id,
                                     models.User.is_active == True)  # noqa: E712
    )).scalars().all()
    return list(rows)


def _can_act_on_step(step, current: models.User, principal_ids: list[int]) -> bool:
    """这一步当前登录人能不能批。

    ⚠️ 三条路径，顺序不能乱：
      1. 没指定人 → 老逻辑，按角色（谁在岗谁批）
      2. 指定了人 → 本人可以；admin/manager 保留兜底（跟改动前一致，别把老板挡在外面）
      3. 指定了人 → 我是他的代理人，且这一步已经晾了 OA_DEPUTY_TAKEOVER_DAYS 天

    第 3 条里 `activated_at` 为 NULL 的按「还没开始计时」处理——**不能当成很久以前**，
    否则存量在途单一升级就全部立刻落进代理人待办。
    """
    if not step.approver_user_id:
        return current.has_role(step.approver_role, "admin", "manager")
    if current.id == step.approver_user_id or current.has_role("admin", "manager"):
        return True
    if step.approver_user_id in principal_ids:
        act = _as_utc(getattr(step, "activated_at", None))
        return bool(act and act <= _deputy_ready_before())
    return False


def _no_right_msg(step, current: models.User) -> str:
    """403 的话要说清楚为什么——「无权审批此步骤」会让人以为是权限配错了，
    其实往往是"这一步指定了别人"或"代理人还没到接手时间"。"""
    if not step.approver_user_id:
        return "无权审批此步骤"
    who = step.approver.full_name or step.approver.username if step.approver else "指定的审批人"
    act = _as_utc(getattr(step, "activated_at", None))
    if act:
        left = OA_DEPUTY_TAKEOVER_DAYS - (datetime.now(timezone.utc) - act).days
        if left > 0:
            return (f"这一步指定由 {who} 审批。若 ta 不在，"
                    f"{left} 天后其代理人可以接手")
    return f"这一步指定由 {who} 审批，你不是本人也不是 ta 的代理人"


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


# ==================== 🆕 成本归集（部门 → 成本科目）====================
#
# 口径（业务已签字）：
#   · 审批通过就计入成本，另标「是否已付」——看得早，也看得见哪些批了还没付
#   · 金额一律按**财务核定金额**(settle_amount)，为空才回退用申请金额
#     ⚠️ 生产上 6 笔单没有一笔填了核定金额，所以回退是常态不是例外
#   · OA 的采购申请**不计**成本，一律以采购单为准（否则同一笔钱算两次）
#   · 差旅按提交人所在部门归集；唯一例外是售后/安装的差旅——那部分跟着售后单走
#   · 售后成本**只认售后登记**，不吃 OA。售后部的 OA 报销入口已经关掉，
#     存量在途的旧单按签字口径原路走完，报表里单独标出来，不并进合计。

COST_CENTERS = ["销售成本", "售后成本", "制造费用", "管理费用"]


def _norm_cost_center(v) -> Optional[str]:
    v = (v or "").strip()
    if not v:
        return None
    if v not in COST_CENTERS:
        raise HTTPException(400, f"成本科目只能是：{'、'.join(COST_CENTERS)}")
    return v


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
                          cost_center=_norm_cost_center(body.cost_center),
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
    d.cost_center = _norm_cost_center(body.cost_center)
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
    return [_chain_step_out(s) for s in (await db.execute(q)).scalars().all()]


async def _role_name(db: AsyncSession, code: str) -> str:
    r = await db.execute(select(models.Role.name).where(models.Role.code == code))
    return r.scalar_one_or_none() or code


def _chain_step_out(s) -> "schemas.OaApprovalStepOut":
    """配置层步骤 → 出参。approver_name 从 relationship 取——
    model_validate 只认同名属性，不会自己去 join 出人名。"""
    o = schemas.OaApprovalStepOut.model_validate(s)
    if s.approver_user_id and s.approver:
        o.approver_name = s.approver.full_name or s.approver.username
    return o


async def _check_approver(db: AsyncSession, uid) -> "models.User | None":
    """指定审批人必须是**在职**用户。配一个离职的人 = 单子直接卡死，
    这种配置错误必须在配置那一刻就拦住，不能等到有人提交申请才发现。"""
    if not uid:
        return None
    u = (await db.execute(select(models.User).where(models.User.id == uid))).scalar_one_or_none()
    if not u:
        raise HTTPException(400, "指定的审批人不存在")
    if not u.is_active:
        raise HTTPException(400, f"「{u.full_name or u.username}」已停用，不能指定为审批人")
    return u


def _step_label(body, role_label: str, approver) -> str:
    """展示名：手填优先；指定到人就显示人名，否则显示角色名。"""
    manual = (body.step_label or "").strip()
    if manual:
        return manual
    return (approver.full_name or approver.username) if approver else role_label


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
    approver = await _check_approver(db, body.approver_user_id)
    label = _step_label(body, await _role_name(db, body.approver_role), approver)
    s = models.OaApprovalStep(department_id=body.department_id, doc_type=body.doc_type,
                              step_order=body.step_order, approver_role=body.approver_role,
                              approver_user_id=body.approver_user_id or None,
                              step_label=label, enabled=body.enabled)
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return _chain_step_out(s)


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
    approver = await _check_approver(db, body.approver_user_id)
    label = _step_label(body, await _role_name(db, body.approver_role), approver)
    s.department_id = body.department_id; s.doc_type = body.doc_type
    s.step_order = body.step_order; s.approver_role = body.approver_role
    s.approver_user_id = body.approver_user_id or None
    s.step_label = label; s.enabled = body.enabled
    await db.commit()
    await db.refresh(s)
    return _chain_step_out(s)


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


# ==================== 🆕 #200 流程级固定抄送（部门+单据类型 → 抄送角色） ====================
@router.get("/flow-cc")
async def get_flow_cc(
    department_id: int = Query(...),
    doc_type: str = Query(...),
    _: models.User = Depends(require_roles()),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.OaFlowCc.role_code).where(
        models.OaFlowCc.department_id == department_id,
        models.OaFlowCc.doc_type == doc_type))
    return {"roles": [x for (x,) in r.all()]}


class FlowCcIn(BaseModel):
    department_id: int
    doc_type: str
    roles: list[str] = []


@router.put("/flow-cc", response_model=schemas.Msg)
async def save_flow_cc(
    body: FlowCcIn,
    current: models.User = Depends(require_roles()),
    db: AsyncSession = Depends(get_db),
):
    """整组覆盖保存该 部门+单据类型 的自动抄送角色；改配置不影响已提交的申请。"""
    cur = (await db.execute(select(models.OaFlowCc).where(
        models.OaFlowCc.department_id == body.department_id,
        models.OaFlowCc.doc_type == body.doc_type))).scalars().all()
    for row in cur:
        await db.delete(row)
    roles = [r for r in dict.fromkeys(body.roles) if (r or "").strip()]
    for rc in roles:
        db.add(models.OaFlowCc(department_id=body.department_id,
                               doc_type=body.doc_type, role_code=rc))
    await db.commit()
    return schemas.Msg(message=f"已保存自动抄送（{len(roles)} 个角色）")


@router.get("/chains/overview")
async def chains_overview(
    current: models.User = Depends(require_roles()),
    db: AsyncSession = Depends(get_db),
):
    """🆕 已配置审批流程一览：把所有 部门×单据类型 的审批链按顺序汇总，一屏总览。"""
    steps = (await db.execute(select(models.OaApprovalStep).order_by(
        models.OaApprovalStep.department_id, models.OaApprovalStep.doc_type,
        models.OaApprovalStep.step_order))).scalars().all()
    depts = {d.id: d for d in (await db.execute(select(models.Department))).scalars().all()}
    docs = {d.key: d.label for d in await _doc_types(db)}
    roles = {r.code: r.name for r in (await db.execute(select(models.Role))).scalars().all()}
    groups: dict[tuple, dict] = {}
    for s in steps:
        key = (s.department_id, s.doc_type)
        g = groups.get(key)
        if not g:
            dept = depts.get(s.department_id)
            g = {"department_id": s.department_id,
                 "department_name": dept.name if dept else f"#{s.department_id}",
                 "dept_sort": dept.sort_order if dept else 9999,
                 "doc_type": s.doc_type, "doc_label": docs.get(s.doc_type, s.doc_type),
                 "steps": []}
            groups[key] = g
        g["steps"].append({
            "step_order": s.step_order, "approver_role": s.approver_role,
            "role_name": roles.get(s.approver_role, s.approver_role),
            # 🆕 指定到人时一览里要看得出是谁，否则一屏"售后部主管"根本分不清哪条配了人
            "approver_user_id": s.approver_user_id,
            "approver_name": ((s.approver.full_name or s.approver.username)
                              if s.approver_user_id and s.approver else None),
            "step_label": s.step_label, "enabled": s.enabled,
        })
    # 按 部门排序→单据类型 输出
    return sorted(groups.values(), key=lambda g: (g["dept_sort"], g["department_id"], g["doc_label"]))


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
    if current.has_role("finance"):   # 🆕 #256：财务可查看任意申请明细（对账/付款需要）
        return True
    if req.requester_id == current.id:
        return True
    # 🆕 被指定的人必须能打开这张单——他不一定挂着 approver_role 那个角色。
    #    这里**不收窄**原有的按角色可见：能看见不等于能批，看得宽一点没坏处，
    #    真正要精确的是「谁能批」(_can_act_on_step) 和待办队列。
    if any(s.approver_user_id == current.id for s in req.steps):
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
    # 🆕 只有当前步骤真的指定到人时才去查"我是谁的代理人"，省掉绝大多数请求的这次查询
    principals = (await _my_principal_ids(db, current)
                  if cur_step is not None and cur_step.approver_user_id else [])
    can_approve = bool(
        req.status == "pending" and cur_step is not None and cur_step.status == "pending"
        and _can_act_on_step(cur_step, current, principals)
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
            id=s.id, step_order=s.step_order, approver_role=s.approver_role,
            approver_user_id=s.approver_user_id,
            approver_name=((s.approver.full_name or s.approver.username)
                           if s.approver_user_id and s.approver else None),
            activated_at=s.activated_at,
            # 代理人是否已经可以接手：只对「指定到人 + 已轮到 + 晾够天数」的步骤为真
            deputy_ready=bool(s.approver_user_id and s.status == "pending"
                              and _as_utc(s.activated_at)
                              and _as_utc(s.activated_at) <= _deputy_ready_before()),
            step_label=s.step_label,
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
    # 🆕 售后/安装的费用改走「售后部登记」，OA 这条路关掉——否则同一笔钱两边都算。
    #    生产上抓到过：售后登记里 2025-120 记了 ¥216，OA 里又有一张
    #    「2025-120行星搅拌机售后维修」¥1,136。
    #    ⚠️ 只挡**新提交**，已经在途的旧单按约定原路走完（这里不碰它们）。
    if category == "reimbursement" and (dept.cost_center == "售后成本"
                                        or "售后" in (dept.name or "")):
        raise HTTPException(400,
                            f"{dept.name}的费用报销请在【售后部 → 登记售后/登记安装】里提交，"
                            f"在那里可以逐行填费用并上传对应发票，主管审批后直接到财务核对报销。"
                            f"（走 OA 会和售后登记的费用重复计入售后成本）")
    if not steps_cfg:
        raise HTTPException(400, f"「{dept.name}」的「{doc_label}」尚未配置审批流程，请联系管理层在【审批流程设置】里配置")
    if body.related_request_id:
        rel = await db.execute(select(models.OaRequest.id).where(models.OaRequest.id == body.related_request_id))
        if not rel.scalar_one_or_none():
            raise HTTPException(400, "关联的业务申请不存在")
    # 🆕 反馈#285 付款申请：服务端必填校验（收款单位/付款金额/付款事由），与前端校验同口径
    if body.doc_type == "payment":
        _d = body.detail or {}
        if not str(_d.get("payee") or "").strip():
            raise HTTPException(400, "请填写收款单位")
        if body.amount is None or float(body.amount) <= 0:
            raise HTTPException(400, "请填写付款金额")
        if not str(_d.get("reason") or "").strip():
            raise HTTPException(400, "请填写付款事由")
    req_no = await _next_oa_no(db)
    req = models.OaRequest(
        request_no=req_no, category=category, doc_type=body.doc_type, department_id=dept.id,
        requester_id=current.id, title=(body.title or "").strip() or doc_label, amount=body.amount,
        detail=body.detail or {}, related_request_id=body.related_request_id,
        status="pending", current_step_order=steps_cfg[0].step_order,
    )
    db.add(req)
    await db.flush()
    # 🆕 #149：报销费用明细——服务端按明细重算报销金额 + 把逐行发票附件挂到本申请（仅本人上传的未归属发票）
    if category == "reimbursement":
        eitems = (req.detail or {}).get("expense_items") or []
        if isinstance(eitems, list) and eitems:
            req.amount = round(sum(float(x.get("amount") or 0)
                                   for x in eitems if isinstance(x, dict)), 2)
            inv_ids = [x.get("invoice_file_id") for x in eitems
                       if isinstance(x, dict) and x.get("invoice_file_id")]
            if inv_ids:
                await db.execute(
                    sa_update(models.Attachment)
                    .where(models.Attachment.id.in_(inv_ids),
                           models.Attachment.biz_type == "oa_request",
                           models.Attachment.biz_id.is_(None),
                           models.Attachment.uploaded_by == current.id)
                    .values(biz_id=req.id))
    # 🆕 指定审批人一起快照下来；第一步立刻 activated_at=now（代理人计时从这里起算，
    #    后面几步要等真正轮到时才写，见 approve）。
    _now = datetime.now(timezone.utc)
    for i, s in enumerate(steps_cfg):
        db.add(models.OaRequestStep(request_id=req.id, step_order=s.step_order,
                                    approver_role=s.approver_role,
                                    approver_user_id=s.approver_user_id,
                                    activated_at=_now if i == 0 else None,
                                    step_label=s.step_label, status="pending"))
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
    # 🆕 #200 流程级固定抄送：该部门+单据类型配置的抄送角色 → 在职用户自动抄送（与手选合并去重）
    fr = await db.execute(select(models.OaFlowCc.role_code).where(
        models.OaFlowCc.department_id == body.department_id,
        models.OaFlowCc.doc_type == body.doc_type))
    cc_roles = [x for (x,) in fr.all()]
    if cc_roles:
        rids = [r for (r,) in (await db.execute(select(models.Role.id).where(
            models.Role.code.in_(cc_roles)))).all()]
        if rids:
            sub = select(models.UserRole.user_id).where(models.UserRole.role_id.in_(rids))
            aur = await db.execute(select(models.User.id).where(
                models.User.is_active == True,  # noqa: E712
                or_(models.User.role_id.in_(rids), models.User.id.in_(sub))))
            for uid in aur.scalars().all():
                if uid != current.id and uid not in cc_ids:
                    cc_ids.append(uid)
                    db.add(models.OaRequestCc(request_id=req.id, user_id=uid))
    await db.commit()
    req = await _fetch_request(db, req.id)
    # 🆕 指定到人就只推给那个人，不再按角色群发——不然指定到人省下的打扰又回来了
    _notify_text = (f"【OA审批】{current.full_name or current.username} "
                    f"提交了「{doc_label}」({req_no})待你审批")
    if steps_cfg[0].approver_user_id:
        await push_message(db, to_user_id=steps_cfg[0].approver_user_id, kind="info",
                           text=_notify_text, biz_type="oa_request", biz_id=req.id)
    else:
        await push_message(db, to_role=steps_cfg[0].approver_role, kind="info",
                           text=_notify_text, biz_type="oa_request", biz_id=req.id)
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


@router.delete("/requests/{rid}", response_model=schemas.Msg)
async def delete_request(
    rid: int,
    current: models.User = Depends(require_roles()),
    db: AsyncSession = Depends(get_db),
):
    """🆕 #199 管理层删除申请单（误提/测试单清理）。附件文件一并删除；
    报销单对它的关联引用置空；审批步骤/抄送记录随级联删除。任意状态可删,操作留审计。"""
    req = (await db.execute(select(models.OaRequest).where(
        models.OaRequest.id == rid))).scalar_one_or_none()
    if not req:
        raise HTTPException(404, "申请不存在")
    no, st = req.request_no, req.status
    # 解除其它单(报销↔业务)对本单的引用
    await db.execute(sa_update(models.OaRequest).where(
        models.OaRequest.related_request_id == rid).values(related_request_id=None))
    # 删附件(文件+记录)
    ars = (await db.execute(select(models.Attachment).where(
        models.Attachment.biz_type == "oa_request",
        models.Attachment.biz_id == rid))).scalars().all()
    for a in ars:
        await delete_attachment_file(db, a)
    await db.delete(req)
    await db.commit()
    await write_audit(db, user=current, action="oa_delete", target_type="oa_request",
                      target_id=rid, detail=f"{no}(原状态:{st})")
    return schemas.Msg(message=f"已删除申请 {no}")


@router.get("/requests", response_model=list[schemas.OaRequestOut])
async def list_requests(
    scope: str = Query("mine", description="mine/pending_me/pending_pay/acted_by_me/cc_me/dept/all"),
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
        # 🆕 三种情况都算"待我审批"：
        #   ① 这一步没指定人、且我挂着它要求的角色（老行为）
        #   ② 这一步指定的就是我
        #   ③ 这一步指定的人把我设成了代理人，且已经晾了 OA_DEPUTY_TAKEOVER_DAYS 天
        # ⚠️ ① 必须加 approver_user_id IS NULL：不加的话指定到人就白配了——
        #    同角色的人照样会在待办里看到这张单，而这正是要解决的问题。
        principals = await _my_principal_ids(db, current)
        mine = or_(
            StepT.approver_user_id == current.id,
            (StepT.approver_user_id.is_(None)
             & StepT.approver_role.in_(current.role_codes or [""])),
        )
        if principals:
            mine = or_(mine, (StepT.approver_user_id.in_(principals)
                              & StepT.activated_at.isnot(None)
                              & (StepT.activated_at <= _deputy_ready_before())))
        cond = exists().where(StepT.request_id == models.OaRequest.id,
                              StepT.step_order == models.OaRequest.current_step_order,
                              StepT.status == "pending", mine)
        q = q.where(models.OaRequest.status == "pending", cond)
    elif scope == "pending_pay":
        # 🆕 反馈#238：末环节是财务审批的单据审完即转「待付款」且 current_step_order 置空，
        #   于是既不在 pending_me(要求 status=pending)、也不在任何环节队列里——单据"消失"，
        #   财务想点「标记已付款」都找不到入口，只有 admin/manager 能去「全部申请」里翻。
        #   这里给财务一个明确的待付款队列。
        if not current.has_role("finance", "admin", "manager"):
            raise HTTPException(403, "无权查看待付款队列")
        q = q.where(models.OaRequest.status == "pending_payment")
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
        # 🆕 #256：财务需要查看全部申请明细（对账/付款用），放开给 finance
        if not current.has_role("admin", "manager", "finance"):
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
    if not _can_act_on_step(cur_step, current, await _my_principal_ids(db, current)):
        raise HTTPException(403, _no_right_msg(cur_step, current))
    cur_step.status = "approved"; cur_step.acted_by = current.id
    cur_step.acted_at = datetime.now(timezone.utc); cur_step.note = (body.note or "").strip() or None
    if body.settle_amount is not None:
        req.settle_amount = body.settle_amount
    next_step = next((s for s in steps_sorted if s.step_order > cur_step.step_order), None)
    # 🆕 最后一步是财务审批的（多见于报销类），先进"待付款"，财务还要再单独点"标记已付款"，
    # 不能审批通过=已付款——审批只代表"同意报销"，钱有没有真的付出去是另一件事，得分开记。
    if next_step:
        req.current_step_order = next_step.step_order
        # 🆕 下一步现在才算"轮到"，代理人的 3 天从这一刻起算。
        #    用建单时刻起算的话，一张在前面几步排了两周的单，一轮到就直接进代理人待办。
        next_step.activated_at = datetime.now(timezone.utc)
    elif cur_step.approver_role == "finance":
        req.status = "pending_payment"; req.current_step_order = None
    else:
        req.status = "approved"; req.current_step_order = None
    await db.commit()
    if next_step and next_step.approver_user_id:
        # 🆕 指定到人就只推给那个人
        await push_message(db, to_user_id=next_step.approver_user_id, kind="info",
                           text=f"【OA审批】{req.request_no} 待你审批", biz_type="oa_request", biz_id=req.id)
    elif next_step:
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
    if not _can_act_on_step(cur_step, current, await _my_principal_ids(db, current)):
        raise HTTPException(403, _no_right_msg(cur_step, current))
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



# ⚠️ 「审批通过」在数据上是**两个** status：approved 和 pending_payment。
#    末环节是财务的报销类单据审完会转 pending_payment（等财务点已付款），
#    只按 approved 过滤的话这些单会从成本里凭空消失。
_OA_COST_STATUS = ("approved", "pending_payment")


@router.get("/reports/cost", response_model=schemas.CostSummaryOut)
async def cost_summary(
    period: Optional[str] = Query(None, description="YYYY-MM，留空=全部"),
    current: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    """按成本科目归集费用：OA 报销 + 售后/安装登记。

    两个来源分开列，因为它们的口径不一样，合在一起就说不清了：
      · OA 报销  —— 部门的 cost_center 决定进哪个科目；采购申请不计
      · 售后登记 —— 一律进「售后成本」，且**售后成本只认这一个来源**
    """
    def _in_period(col):
        if not period:
            return None
        return func.substr(func.cast(col, String), 1, 7) == period

    notes = [
        "金额按财务核定金额算；没填核定金额的按申请金额算。",
        "审批通过即计入（含已审完待付款的），不等实际付款。",
        "OA 的采购申请不计入——成本一律以采购单为准，否则同一笔钱算两次。",
        "售后成本只统计售后/安装登记，不吃 OA 报销，避免同一笔费用算两遍。",
    ]

    rows: list[schemas.CostRow] = []

    # ---------- 来源一：OA 报销 ----------
    depts = {d.id: d for d in (await db.execute(select(models.Department))).scalars().all()}
    q = select(models.OaRequest).where(
        models.OaRequest.status.in_(_OA_COST_STATUS),
        models.OaRequest.category != "purchase")     # 采购申请不计
    oa_all = list((await db.execute(q)).scalars().all())
    if period:
        oa_all = [r for r in oa_all
                  if (r.updated_at or r.created_at) and
                  (r.updated_at or r.created_at).strftime("%Y-%m") == period]

    agg: dict[str, list] = {}
    skipped_as = {"count": 0, "amount": 0.0}
    for r in oa_all:
        d = depts.get(r.department_id)
        cc = d.cost_center if d else None
        amt = float(r.settle_amount if r.settle_amount is not None else (r.amount or 0))
        # 售后成本只认售后登记：售后部走 OA 的存量报销单单独拎出来，不并进合计
        if cc == "售后成本":
            skipped_as["count"] += 1
            skipped_as["amount"] += amt
            continue
        if not cc:
            continue                       # 部门没配成本科目 → 不归集
        cur = agg.setdefault(cc, [0, 0.0])
        cur[0] += 1
        cur[1] += amt
    for cc, (n, amt) in agg.items():
        rows.append(schemas.CostRow(cost_center=cc, source="oa_reimbursement",
                                    source_label="OA 报销", count=n, amount=round(amt, 2)))
    if skipped_as["count"]:
        notes.append(
            f"另有 {skipped_as['count']} 笔售后部走 OA 的报销单（¥{skipped_as['amount']:,.0f}）"
            f"**没有计入**：售后费用已改为在售后登记里提交，这些是旧流程的在途单，"
            f"按约定原路走完，不并进售后成本以免重复。")

    # ---------- 来源二：售后/安装登记 ----------
    as_q = select(models.AfterSales).where(models.AfterSales.status == "approved")
    as_all = list((await db.execute(as_q)).scalars().all())
    if period:
        as_all = [a for a in as_all
                  if a.created_at and a.created_at.strftime("%Y-%m") == period]
    if as_all:
        rows.append(schemas.CostRow(
            cost_center="售后成本", source="aftersales", source_label="售后/安装登记",
            count=len(as_all), amount=round(sum(float(a.cost or 0) for a in as_all), 2)))

    by_center: dict[str, float] = {}
    for r in rows:
        by_center[r.cost_center] = round(by_center.get(r.cost_center, 0) + r.amount, 2)
    rows.sort(key=lambda x: (COST_CENTERS.index(x.cost_center)
                             if x.cost_center in COST_CENTERS else 99, x.source))
    return schemas.CostSummaryOut(
        period=period or "全部", rows=rows, by_center=by_center,
        total=round(sum(by_center.values()), 2), notes=notes)


@router.get("/reports/summary/detail", response_model=list[schemas.OaSummaryDetailRow])
async def oa_summary_detail(
    department_id: int = Query(...),
    doc_type: str = Query(...),
    current: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    """🆕 #247 汇总报表下钻：某部门+单据类型下的已批准申请逐条明细（申请人/金额/事由/时间）。"""
    q = (
        select(models.OaRequest)
        .where(models.OaRequest.status == "approved",
               models.OaRequest.department_id == department_id,
               models.OaRequest.doc_type == doc_type)
        .order_by(models.OaRequest.updated_at.desc())
    )
    reqs = (await db.execute(q)).scalars().all()
    out = []
    for r in reqs:
        settled = r.settle_amount is not None
        amt = r.settle_amount if settled else (r.amount or 0)
        out.append(schemas.OaSummaryDetailRow(
            id=r.id, request_no=r.request_no,
            requester_name=(r.requester.full_name or r.requester.username) if r.requester else None,
            title=r.title, amount=round(amt or 0, 2), settled=settled,
            created_at=r.created_at, updated_at=r.updated_at))
    return out
