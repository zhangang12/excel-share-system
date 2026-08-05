"""🆕 v3 M10 售后部（§十五）：登记(物料清单必传)→主管审批→自动同步财务部。

- 售后部无项目目录菜单：登记弹窗的项目下拉走 GET /aftersales/projects 轻量端点（仅 code/name）
- 审批通过 → 推送财务部角色池（含费用/问题摘要/物料清单），财务部「售后费用」tab 展示已审批记录
"""
import json
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user, require_roles
from ..notify import push_message
from ..utils import write_audit
from .attachments_router import save_upload, delete_attachment_file

router = APIRouter(prefix="/api/aftersales", tags=["售后部"])


def _uname(u: Optional[models.User]) -> Optional[str]:
    return (u.full_name or u.username) if u else None


def _parse_items(raw: str) -> list[schemas.AfterSalesItemIn]:
    """费用清单从 multipart 里以 JSON 字符串传过来（因为同一请求还带文件上传）。

    宽松解析：空/坏 JSON 一律当成"没传明细"退回旧的手填总额路径，
    不能因为清单格式问题把整条登记卡住——登记是一线在现场做的。
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise HTTPException(400, "费用清单格式不对，请重新填写")
    if not isinstance(data, list):
        return []
    out: list[schemas.AfterSalesItemIn] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or "").strip()
        try:
            amt = float(d.get("amount") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, f"「{name or '费用行'}」的金额不是数字")
        if not name and not amt:
            continue                      # 整行空白，跳过（前端加了行没填）
        if not name:
            raise HTTPException(400, "费用清单里有金额没填费用项名称")
        out.append(schemas.AfterSalesItemIn(
            name=name, amount=amt,
            invoice_file_id=d.get("invoice_file_id") or None,
            note=d.get("note") or None))
    return out


async def _rows(db: AsyncSession, items: list[models.AfterSales]) -> list[schemas.AfterSalesRow]:
    # 🆕 明细一次性取回，别在循环里逐条查（N+1）
    rec_ids = [a.id for a in items]
    by_parent: dict[int, list[models.AfterSalesItem]] = {}
    if rec_ids:
        rr = await db.execute(
            select(models.AfterSalesItem)
            .where(models.AfterSalesItem.aftersales_id.in_(rec_ids))
            .order_by(models.AfterSalesItem.sort_order, models.AfterSalesItem.id))
        for it in rr.scalars().all():
            by_parent.setdefault(it.aftersales_id, []).append(it)

    att_ids = [a.mat_file_id for a in items if a.mat_file_id]
    att_ids += [it.invoice_file_id for lst in by_parent.values()
                for it in lst if it.invoice_file_id]
    names: dict[int, str] = {}
    if att_ids:
        r = await db.execute(select(models.Attachment).where(models.Attachment.id.in_(att_ids)))
        names = {x.id: x.name for x in r.scalars().all()}
    uids = [a.created_by for a in items if a.created_by]
    uids += [a.pay_by for a in items if a.pay_by]
    unames: dict[int, str] = {}
    if uids:
        r = await db.execute(select(models.User).where(models.User.id.in_(uids)))
        unames = {u.id: _uname(u) for u in r.scalars().all()}
    out = []
    for a in items:
        p = a.project
        its = by_parent.get(a.id, [])
        out.append(schemas.AfterSalesRow(
            id=a.id, project_id=a.project_id, kind=a.kind or "aftersales",
            # #158：以往项目只填了名称(project_id 为空)时，用 project_name 展示
            code=p.code if p else ("历史" if a.project_name else ""),
            name=p.name if p else (a.project_name or ""),
            problem=a.problem, cost=a.cost or 0, status=a.status,
            pay_status=a.pay_status, pay_note=a.pay_note,
            pay_by_name=unames.get(a.pay_by), pay_at=a.pay_at,
            items=[schemas.AfterSalesItemOut(
                id=it.id, name=it.name, amount=it.amount or 0,
                invoice_file_id=it.invoice_file_id,
                invoice_file_name=names.get(it.invoice_file_id),
                note=it.note,
            ) for it in its],
            missing_invoice=len([it for it in its if not it.invoice_file_id]),
            mat_file_id=a.mat_file_id, mat_file_name=names.get(a.mat_file_id),
            created_by_name=unames.get(a.created_by), created_at=a.created_at,
        ))
    return out


@router.get("", response_model=schemas.AfterSalesListOut)
async def list_aftersales(
    current: models.User = Depends(require_roles("as_worker", "as_lead", "finance")),
    db: AsyncSession = Depends(get_db),
):
    """售后/管理层看全部；财务只读已审批（财务页「售后费用」tab 复用此端点 status=approved 过滤）。"""
    q = select(models.AfterSales)
    if current.has_role("finance") and not current.has_role("as_worker", "as_lead", "admin", "manager"):
        q = q.where(models.AfterSales.status == "approved")
    r = await db.execute(q.order_by(models.AfterSales.id.desc()).limit(500))
    items = list(r.scalars().all())
    rows = await _rows(db, items)
    stats = schemas.AfterSalesStats(
        total=len(items),
        pending=sum(1 for a in items if a.status == "pending"),
        approved_cost=sum(a.cost or 0 for a in items if a.status == "approved"),
        total_cost=sum(a.cost or 0 for a in items),
    )
    return schemas.AfterSalesListOut(rows=rows, stats=stats)


@router.get("/projects", response_model=List[schemas.AfterSalesProjOption])
async def project_options(
    _: models.User = Depends(require_roles("as_worker", "as_lead")),
    db: AsyncSession = Depends(get_db),
):
    """登记弹窗项目下拉（售后无目录菜单，单开轻量只读端点，仅 code/name）。"""
    r = await db.execute(
        select(models.Project.id, models.Project.code, models.Project.name)
        .where(models.Project.is_deleted == False)  # noqa: E712
        .order_by(models.Project.code)
    )
    return [schemas.AfterSalesProjOption(id=i, code=c, name=n) for i, c, n in r.all()]


@router.post("", response_model=schemas.Msg)
async def create_aftersales(
    project_id: Optional[int] = Form(None),
    project_name: Optional[str] = Form(None),   # #158：以往项目(系统里没有的)只填名称
    problem: str = Form(""),
    cost: float = Form(0),
    items: str = Form(""),            # 🆕 费用清单 JSON: [{name,amount,invoice_file_id,note}, ...]
    kind: str = Form("aftersales"),   # 🆕 需求一：aftersales 售后 / install 安装
    file: UploadFile = File(...),
    current: models.User = Depends(require_roles("as_worker")),
    db: AsyncSession = Depends(get_db),
):
    """售后/安装登记：项目+问题(安装可空)+费用清单(可加行,带发票)+物料清单 → 待审批，推售后主管。
    项目二选一：project_id(系统里的项目) 或 project_name(以往项目，系统里没有，只填名称)。

    🆕 费用改成清单：每行 费用项+金额+发票，总额由明细自动合计。
       手填总额只在**没传明细**时才用（兼容旧客户端/旧表单）——手填的话跟发票必然对不上。"""
    kind = kind if kind in ("aftersales", "install") else "aftersales"
    label = "安装" if kind == "install" else "售后"
    if kind != "install" and not problem.strip():
        raise HTTPException(400, "请填写售后问题")
    parsed = _parse_items(items)
    if parsed:
        cost = round(sum(float(x.amount or 0) for x in parsed), 2)
    if cost <= 0:
        raise HTTPException(400, f"请填写{label}费用" if not parsed else "费用清单合计必须大于 0")
    p = None
    hist_name = (project_name or "").strip()
    if project_id:
        r = await db.execute(select(models.Project).where(
            models.Project.id == project_id, models.Project.is_deleted == False))  # noqa: E712
        p = r.scalar_one_or_none()
        if not p:
            raise HTTPException(404, "项目不存在")
        hist_name = None            # 命中系统项目就不存历史名，避免两边不一致
    elif not hist_name:
        raise HTTPException(400, "请选择项目或填写以往项目名称")
    a = models.AfterSales(project_id=(p.id if p else None), project_name=hist_name, kind=kind,
                          problem=problem.strip() or ("安装登记" if kind == "install" else ""),
                          cost=cost, status="pending", created_by=current.id)
    db.add(a)
    await db.flush()
    biz = "install_mat" if kind == "install" else "aftersales_mat"
    att = await save_upload(db, file, biz_type=biz, biz_id=a.id,
                            project_id=(p.id if p else None), user=current)
    a.mat_file_id = att.id
    for i, it in enumerate(parsed):
        db.add(models.AfterSalesItem(
            aftersales_id=a.id, name=it.name.strip()[:64], amount=float(it.amount or 0),
            invoice_file_id=it.invoice_file_id, note=(it.note or "").strip()[:200] or None,
            sort_order=i))
    await db.commit()
    summary = (problem.strip()[:20] + "…") if problem.strip() else "安装登记"
    disp = f"{p.code} {p.name}" if p else f"[以往]{hist_name}"
    await push_message(db, to_role="as_lead", kind="info",
                       text=f"【{label}待审批】{disp}：{summary} 费用 ¥{cost:,.0f}",
                       biz_type="aftersales", biz_id=a.id)
    await write_audit(db, user=current, action="create", target_type="aftersales", target_id=a.id)
    return schemas.Msg(message=f"已登记，等待售后主管审批")


@router.post("/{aid}/approve", response_model=schemas.Msg)
async def approve(
    aid: int,
    current: models.User = Depends(require_roles("as_lead")),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.AfterSales).where(models.AfterSales.id == aid))
    a = r.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "记录不存在")
    if a.status != "pending":
        raise HTTPException(400, "该记录不在待审批状态")
    a.status = "approved"
    a.appr_by = current.id
    a.appr_at = datetime.now(timezone.utc)
    # 🆕 审批通过 = 费用确认发生（成本口径就按 status=approved，没变）；
    #    钱还没付，进入财务核对发票这一步。有明细的才走这条支腿——
    #    旧流程登记的（没有费用清单）保持 pay_status=NULL，别把历史记录塞进财务待办。
    n_items = (await db.execute(select(func.count(models.AfterSalesItem.id)).where(
        models.AfterSalesItem.aftersales_id == a.id))).scalar() or 0
    if n_items:
        a.pay_status = "checking"
    await db.commit()
    p = a.project
    disp = f"{p.code} {p.name}" if p else f"[以往]{a.project_name or ''}"  # #158 历史项目无 project
    label = "安装" if (a.kind or "aftersales") == "install" else "售后"
    mat = ""
    if a.mat_file_id:
        rr = await db.execute(select(models.Attachment).where(models.Attachment.id == a.mat_file_id))
        m = rr.scalar_one_or_none()
        if m:
            mat = f"，{'安装' if label == '安装' else '物料'}清单：{m.name}"
    if a.pay_status == "checking":
        miss = (await db.execute(select(func.count(models.AfterSalesItem.id)).where(
            models.AfterSalesItem.aftersales_id == a.id,
            models.AfterSalesItem.invoice_file_id.is_(None)))).scalar() or 0
        warn = f"，其中 {miss} 行没传发票" if miss else ""
        await push_message(db, to_role="finance", kind="info",
                           text=(f"【{label}报销待核对】{disp} {label}费用 ¥{a.cost:,.0f} "
                                 f"已经售后主管审批，请核对报销明细与发票{warn}"),
                           biz_type="aftersales", biz_id=a.id)
    else:
        await push_message(db, to_role="finance", kind="info",
                           text=f"【{label}费用同步】{disp} {label}费用 ¥{a.cost:,.0f} 已审批{mat}",
                           biz_type="aftersales", biz_id=a.id)
    # 🆕 盈利改善1a·售后侵蚀率报警：该项目累计已审批安装/售后费用 ÷ 合同额 > 5% → 提醒管理层
    if a.project_id:
        lr = await db.execute(select(models.SalesLedger.amount).where(
            models.SalesLedger.project_id == a.project_id))
        contract = lr.scalar_one_or_none()
        if contract and contract > 0:
            tr = await db.execute(select(func.sum(models.AfterSales.cost)).where(
                models.AfterSales.project_id == a.project_id,
                models.AfterSales.status == "approved"))
            total_as = tr.scalar() or 0
            ratio = total_as / contract * 100
            if ratio > 5:
                await push_message(
                    db, to_role="manager", kind="warn",
                    text=(f"【售后侵蚀预警】{disp} 累计安装/售后费用 ¥{total_as:,.0f}"
                          f"，已达合同额 {ratio:.1f}%（阈值 5%），请关注该项目/机型的质量成本"),
                    biz_type="aftersales", biz_id=a.id)
    await write_audit(db, user=current, action="approve", target_type="aftersales", target_id=aid)
    return schemas.Msg(message=f"已通过，{label}费用/清单已同步财务部")


@router.post("/{aid}/reject", response_model=schemas.Msg)
async def reject(
    aid: int,
    reason: str = Form(""),
    current: models.User = Depends(require_roles("as_lead")),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(models.AfterSales).where(models.AfterSales.id == aid))
    a = r.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "记录不存在")
    if a.status != "pending":
        raise HTTPException(400, "该记录不在待审批状态")
    a.status = "rejected"
    a.appr_by = current.id
    a.appr_at = datetime.now(timezone.utc)
    a.reject_reason = (reason or "").strip() or None  # 🆕 #98
    # 🆕 #97/#98 通知登记人(含原因)——commit 前捕获关系值避免 greenlet 失效
    p_code = a.project.code if a.project else ("[以往]" if a.project_name else f"#{a.project_id}")
    p_name = a.project.name if a.project else (a.project_name or "")
    creator_id, problem, rr = a.created_by, a.problem, a.reject_reason
    await db.commit()
    if creator_id:
        suffix = f"，原因：{rr}" if rr else ""
        await push_message(db, to_user_id=creator_id, kind="warn",
                           text=f"【售后驳回】{p_code} {p_name}：{problem[:20]}… 已被驳回{suffix}",
                           biz_type="aftersales", biz_id=aid)
    await write_audit(db, user=current, action="reject", target_type="aftersales", target_id=aid)
    return schemas.Msg(message="已驳回")



# ==================== 🆕 报销支腿：财务核对发票 → 安排报销 ====================
#
# 口径（业务已签字）：售后主管审批 → 财务部核对报销明细与发票 → 安排报销。
# 「财务出纳」不新建角色，就用现有的 finance；发票对不上退回**登记人**重传，
# 改完直接回到财务这一步，不用售后主管再批一遍。

def _pay_disp(a: models.AfterSales) -> str:
    p = a.project
    return f"{p.code} {p.name}" if p else f"[以往]{a.project_name or ''}"


async def _get_for_pay(db: AsyncSession, aid: int) -> models.AfterSales:
    r = await db.execute(select(models.AfterSales).where(models.AfterSales.id == aid))
    a = r.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "记录不存在")
    if a.status != "approved":
        raise HTTPException(400, "只有售后主管审批通过的记录才进报销环节")
    return a


@router.post("/{aid}/pay-reject", response_model=schemas.Msg)
async def pay_reject(
    aid: int,
    reason: str = Form(""),
    current: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    """财务核对发现发票对不上 → 退回登记人重传。

    ⚠️ 不动 status：费用本身已经被售后主管认过了，退回的是**发票**不是费用。
       把 status 打回 pending 会让这笔钱从售后成本里消失，月底数就对不上了。
    """
    a = await _get_for_pay(db, aid)
    if a.pay_status not in ("checking",):
        raise HTTPException(400, "只有待核对的记录能退回")
    why = (reason or "").strip()
    if not why:
        raise HTTPException(400, "请说明发票哪里对不上，否则登记人不知道要改什么")
    a.pay_status = "invoice_fix"
    a.pay_note = why
    a.pay_by = current.id
    a.pay_at = datetime.now(timezone.utc)
    disp, creator = _pay_disp(a), a.created_by
    await db.commit()
    if creator:
        await push_message(db, to_user_id=creator, kind="warn",
                           text=f"【发票退回】{disp} 的报销发票需要重传：{why}",
                           biz_type="aftersales", biz_id=aid)
    await write_audit(db, user=current, action="pay_reject", target_type="aftersales", target_id=aid)
    return schemas.Msg(message="已退回登记人重传发票")


@router.post("/{aid}/resubmit-invoice", response_model=schemas.Msg)
async def resubmit_invoice(
    aid: int,
    items: str = Form(""),
    current: models.User = Depends(require_roles("as_worker", "as_lead")),
    db: AsyncSession = Depends(get_db),
):
    """登记人重传发票后交回财务。整份费用清单覆盖式替换（前端把改好的整表传回来）。

    ⚠️ 合计变了要同步写回 cost——不然成本口径跟明细对不上。
    """
    a = await _get_for_pay(db, aid)
    if a.pay_status != "invoice_fix":
        raise HTTPException(400, "这条记录当前没有被退回")
    if a.created_by and a.created_by != current.id and not current.has_role("as_lead", "admin", "manager"):
        raise HTTPException(403, "只有登记人本人（或售后主管）能重传")
    parsed = _parse_items(items)
    if not parsed:
        raise HTTPException(400, "费用清单不能为空")
    await db.execute(models.AfterSalesItem.__table__.delete().where(
        models.AfterSalesItem.aftersales_id == a.id))
    for i, it in enumerate(parsed):
        db.add(models.AfterSalesItem(
            aftersales_id=a.id, name=it.name.strip()[:64], amount=float(it.amount or 0),
            invoice_file_id=it.invoice_file_id, note=(it.note or "").strip()[:200] or None,
            sort_order=i))
    a.cost = round(sum(float(x.amount or 0) for x in parsed), 2)
    a.pay_status = "checking"
    a.pay_note = None
    disp, cost = _pay_disp(a), a.cost
    await db.commit()
    await push_message(db, to_role="finance", kind="info",
                       text=f"【发票已重传】{disp} 报销 ¥{cost:,.0f} 请重新核对",
                       biz_type="aftersales", biz_id=aid)
    await write_audit(db, user=current, action="resubmit_invoice",
                      target_type="aftersales", target_id=aid)
    return schemas.Msg(message="已重新提交财务核对")


@router.post("/{aid}/reimburse", response_model=schemas.Msg)
async def reimburse(
    aid: int,
    note: str = Form(""),
    current: models.User = Depends(require_roles("finance")),
    db: AsyncSession = Depends(get_db),
):
    """财务核对无误 → 安排报销（这条支腿的终点）。"""
    a = await _get_for_pay(db, aid)
    if a.pay_status != "checking":
        raise HTTPException(400, "只有待核对的记录能安排报销")
    miss = (await db.execute(select(func.count(models.AfterSalesItem.id)).where(
        models.AfterSalesItem.aftersales_id == a.id,
        models.AfterSalesItem.invoice_file_id.is_(None)))).scalar() or 0
    if miss:
        raise HTTPException(400, f"还有 {miss} 行没传发票，请先退回让登记人补齐")
    a.pay_status = "reimbursed"
    a.pay_note = (note or "").strip() or None
    a.pay_by = current.id
    a.pay_at = datetime.now(timezone.utc)
    disp, creator, cost = _pay_disp(a), a.created_by, a.cost
    await db.commit()
    if creator:
        await push_message(db, to_user_id=creator, kind="info",
                           text=f"【报销已安排】{disp} 报销 ¥{cost:,.0f} 财务已核对无误并安排报销",
                           biz_type="aftersales", biz_id=aid)
    await write_audit(db, user=current, action="reimburse", target_type="aftersales", target_id=aid)
    return schemas.Msg(message="已安排报销")


@router.post("/{aid}/finance-void", response_model=schemas.Msg)
async def finance_void(
    aid: int,
    current: models.User = Depends(require_roles("admin", "manager")),
    db: AsyncSession = Depends(get_db),
):
    """财务部管理层作废已审批的售后费用记录，退回待审批状态供售后部重新处理。"""
    r = await db.execute(select(models.AfterSales).where(models.AfterSales.id == aid))
    a = r.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "记录不存在")
    if a.status != "approved":
        raise HTTPException(400, "只能作废已审批的售后费用记录")
    p_code = a.project.code if a.project else ("[以往]" if a.project_name else f"#{a.project_id}")
    p_name = a.project.name if a.project else (a.project_name or "")
    problem = a.problem
    creator_id = a.created_by
    a.status = "pending"
    a.appr_by = None
    a.appr_at = None
    await db.commit()
    if creator_id:
        await push_message(db, to_user_id=creator_id, kind="warn",
                           text=f"【售后费用退回】{p_code} {p_name}：{problem[:20]}… 财务已退回，请售后主管重新审核。",
                           biz_type="aftersales", biz_id=aid)
    await push_message(db, to_role="as_lead", kind="warn",
                       text=f"【售后费用退回】{p_code} 财务退回售后费用记录，请重新处理。",
                       biz_type="aftersales", biz_id=aid)
    await write_audit(db, user=current, action="finance_void", target_type="aftersales", target_id=aid)
    return schemas.Msg(message="已作废并退回售后部重新审批")


@router.delete("/{aid}", response_model=schemas.Msg)
async def delete_aftersale(
    aid: int,
    current: models.User = Depends(require_roles("admin", "manager")),
    db: AsyncSession = Depends(get_db),
):
    """管理层永久删除售后记录（物料清单附件一并删除）。"""
    r = await db.execute(select(models.AfterSales).where(models.AfterSales.id == aid))
    a = r.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "记录不存在")
    # a.project 用 joined 关系，即使项目已软删/已被硬删也能取到（软删）或为 None（硬删）
    p_code = a.project.code if a.project else ("[以往]" if a.project_name else f"#{a.project_id}")
    label = "安装" if (a.kind or "aftersales") == "install" else "售后"
    # 🆕 需求八：先把 mat_file_id 置 NULL 并 flush 解除外键引用，再删附件——
    # 否则 Postgres 下"删附件时 aftersales 仍引用它"会触发外键约束(关联的数据不存在或被引用)，
    # 令关联已软删项目/孤立附件的脏记录在 UI 上删不掉（SQLite 不校验外键，沙箱测不出，故本改动在真库同源）。
    att = None
    if a.mat_file_id:
        att = (await db.execute(select(models.Attachment).where(
            models.Attachment.id == a.mat_file_id))).scalar_one_or_none()
        a.mat_file_id = None
        await db.flush()
    if att is not None:
        await delete_attachment_file(db, att)
    await db.delete(a)
    await db.commit()
    await write_audit(db, user=current, action="delete", target_type="aftersales", target_id=aid,
                      detail=f"{p_code} {label}记录已删除")
    return schemas.Msg(message=f"{p_code} {label}记录已删除")
