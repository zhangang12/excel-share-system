"""🆕 销售线索跟踪：主管/管理层集中录入网络询盘 → 分配销售员 → 跟进/补全/改状态 → 成交率报表。

- 行级隔离：销售员仅见分配给自己(owner_uid)的线索；主管/管理层全量。
- 录入/分配/改派/删除/报表：仅销售主管/管理层（sales_lead，admin/manager 始终放行）。
- 跟进（补全客户信息 + 改状态 + 写跟进记录）：被分配的销售员对本人线索即可。
- 状态：潜在需求(默认)/报价/成交/丢单；成交率 = 成交数 ÷ 线索总数。
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from .. import models, schemas
from ..deps import require_roles
from ..notify import push_message
from ..utils import write_audit

router = APIRouter(prefix="/api/sales/leads", tags=["销售线索"])

# 询盘来源（固定下拉，报表分组维度；前端同源 LEAD_SOURCES）
LEAD_SOURCES = ["1688", "Geo", "爱采购", "百度推广", "其他"]
LEAD_STATUSES = ["潜在需求", "报价", "成交", "丢单"]
_TERMINAL = {"成交", "丢单"}


def _all_view(u: models.User) -> bool:
    return u.has_role("admin", "manager", "sales_lead")


def _uname(u: Optional[models.User]) -> Optional[str]:
    return (u.full_name or u.username) if u else None


def _lead_title(l: models.SalesLead) -> str:
    return l.customer or l.contact or l.phone or l.wechat or "(待补全线索)"


async def _lead_or_404(db: AsyncSession, lid: int) -> models.SalesLead:
    res = await db.execute(select(models.SalesLead).where(models.SalesLead.id == lid))
    lead = res.scalar_one_or_none()
    if not lead:
        raise HTTPException(404, "线索不存在")
    return lead


async def _name_map(db: AsyncSession, uids: set[int]) -> dict[int, Optional[str]]:
    uids = {u for u in uids if u}
    if not uids:
        return {}
    res = await db.execute(select(models.User).where(models.User.id.in_(uids)))
    return {u.id: _uname(u) for u in res.scalars().all()}


async def _lead_rows(db: AsyncSession, leads: list[models.SalesLead]) -> list[schemas.SalesLeadRow]:
    names = await _name_map(db, {l.owner_uid for l in leads} | {l.created_by for l in leads})
    return [
        schemas.SalesLeadRow(
            id=l.id, source=l.source,
            customer=l.customer, contact=l.contact, phone=l.phone, wechat=l.wechat,
            requirement=l.requirement,
            owner_uid=l.owner_uid, owner_name=names.get(l.owner_uid),
            status=l.status, follow_log=l.follow_log, lost_reason=l.lost_reason,
            created_by=l.created_by, created_by_name=names.get(l.created_by),
            assigned_at=l.assigned_at, closed_at=l.closed_at, created_at=l.created_at,
        )
        for l in leads
    ]


@router.get("", response_model=schemas.SalesLeadListOut)
async def list_leads(
    source: Optional[str] = Query(None),
    owner_uid: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    kw: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current: models.User = Depends(require_roles("sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """线索列表：销售员仅本人(owner_uid)；主管/管理层全量，可按负责人筛选。"""
    q = select(models.SalesLead)
    if not _all_view(current):
        q = q.where(models.SalesLead.owner_uid == current.id)  # 行级隔离
    elif owner_uid:
        q = q.where(models.SalesLead.owner_uid == owner_uid)
    if source:
        q = q.where(models.SalesLead.source == source)
    if status:
        q = q.where(models.SalesLead.status == status)
    res = await db.execute(q.order_by(models.SalesLead.id.desc()))
    leads = list(res.scalars().all())
    if kw:
        k = kw.strip()
        leads = [l for l in leads if any(
            k in (v or "") for v in (l.customer, l.contact, l.phone, l.wechat, l.requirement))]
    total = len(leads)
    start = (page - 1) * page_size
    rows = await _lead_rows(db, leads[start:start + page_size])
    return schemas.SalesLeadListOut(rows=rows, total=total)


@router.post("", response_model=schemas.SalesLeadRow)
async def create_lead(
    data: schemas.SalesLeadCreate,
    current: models.User = Depends(require_roles("sales_lead")),   # 仅主管/管理层录入分配
    db: AsyncSession = Depends(get_db),
):
    """录入线索（可同时分配给某销售员，留空=先进线索池）。"""
    src = (data.source or "").strip()
    if src not in LEAD_SOURCES:
        raise HTTPException(400, f"询盘来源无效，应为：{'、'.join(LEAD_SOURCES)}")
    status = (data.status or "潜在需求").strip()
    if status not in LEAD_STATUSES:
        raise HTTPException(400, "线索状态无效")
    now = datetime.now(timezone.utc)
    lead = models.SalesLead(
        source=src,
        customer=(data.customer or "").strip() or None,
        contact=(data.contact or "").strip() or None,
        phone=(data.phone or "").strip() or None,
        wechat=(data.wechat or "").strip() or None,
        requirement=(data.requirement or "").strip() or None,
        owner_uid=data.owner_uid or None,
        status=status,
        follow_log=(data.follow_log or "").strip() or None,
        lost_reason=None,
        created_by=current.id,
        assigned_at=now if data.owner_uid else None,
        closed_at=now if status in _TERMINAL else None,
    )
    db.add(lead)
    await db.commit()
    title, source_, owner_uid_, lid = _lead_title(lead), lead.source, lead.owner_uid, lead.id
    if owner_uid_:
        await push_message(db, to_user_id=owner_uid_, kind="info",
                           text=f"【新线索待跟进】{title}（来源：{source_}），请尽快跟进。",
                           biz_type="sales_lead", biz_id=lid)
    await write_audit(db, user=current, action="create", target_type="sales_lead",
                      target_id=lid, detail=f"{source_} {title}")
    rows = await _lead_rows(db, [lead])
    return rows[0]


@router.get("/report", response_model=schemas.SalesLeadReport)
async def lead_report(
    year: Optional[str] = Query(None),     # YYYY；留空=全部
    month: Optional[str] = Query(None),    # YYYY-MM（优先于 year）
    current: models.User = Depends(require_roles("sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """线索报表：① 按询盘来源 ② 按跟进销售；各算 线索数/成交数/成交率。"""
    res = await db.execute(select(models.SalesLead))
    leads = list(res.scalars().all())
    if month and month.strip():
        m = month.strip()
        leads = [l for l in leads if l.created_at and l.created_at.strftime("%Y-%m") == m]
    elif year and year.strip():
        y = year.strip()
        leads = [l for l in leads if l.created_at and l.created_at.strftime("%Y") == y]

    def _agg(items: list[models.SalesLead]) -> dict:
        n = len(items)
        deal = sum(1 for x in items if x.status == "成交")
        return dict(
            leads=n, deal=deal,
            quote=sum(1 for x in items if x.status == "报价"),
            potential=sum(1 for x in items if x.status == "潜在需求"),
            lost=sum(1 for x in items if x.status == "丢单"),
            rate=round(deal / n, 4) if n else 0.0,
        )

    # ① 按来源（固定顺序，仅列出有数据的；兼容历史不在列表里的来源）
    by_source = []
    extra = sorted({l.source for l in leads} - set(LEAD_SOURCES))
    for s in LEAD_SOURCES + extra:
        items = [l for l in leads if l.source == s]
        if items:
            by_source.append(schemas.LeadReportItem(key=s, **_agg(items)))

    # ② 按销售（仅已分配）
    owner_ids = {l.owner_uid for l in leads if l.owner_uid}
    names = await _name_map(db, owner_ids)
    by_owner = [
        schemas.LeadReportItem(key=names.get(oid) or f"#{oid}",
                               **_agg([l for l in leads if l.owner_uid == oid]))
        for oid in owner_ids
    ]
    by_owner.sort(key=lambda x: (-x.deal, -x.leads))

    tl = len(leads)
    td = sum(1 for l in leads if l.status == "成交")
    return schemas.SalesLeadReport(
        by_source=by_source, by_owner=by_owner,
        total_leads=tl, total_deal=td,
        total_rate=round(td / tl, 4) if tl else 0.0,
    )


@router.put("/{lead_id}", response_model=schemas.SalesLeadRow)
async def update_lead(
    lead_id: int, data: schemas.SalesLeadUpdate,
    current: models.User = Depends(require_roles("sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """跟进/编辑线索：销售员补全信息 + 改状态 + 写跟进记录（仅本人线索）；
    来源修改与改派(owner_uid)仅主管/管理层。"""
    lead = await _lead_or_404(db, lead_id)
    allview = _all_view(current)
    if not allview and lead.owner_uid != current.id:
        raise HTTPException(403, "只能跟进分配给本人的线索")
    now = datetime.now(timezone.utc)

    for f in ("customer", "contact", "phone", "wechat", "requirement", "follow_log", "lost_reason"):
        v = getattr(data, f)
        if v is not None:
            setattr(lead, f, v.strip() or None)

    notify_deal = False
    if data.status is not None:
        st = data.status.strip()
        if st not in LEAD_STATUSES:
            raise HTTPException(400, "线索状态无效")
        prev = lead.status
        lead.status = st
        if st in _TERMINAL and prev not in _TERMINAL:
            lead.closed_at = now
        elif st not in _TERMINAL:
            lead.closed_at = None
        if st == "成交" and prev != "成交":
            notify_deal = True

    notify_owner = None
    if allview:
        if data.source is not None:
            s = data.source.strip()
            if s and s not in LEAD_SOURCES:
                raise HTTPException(400, "询盘来源无效")
            if s:
                lead.source = s
        if data.owner_uid is not None:
            new_owner = data.owner_uid or None
            if new_owner != lead.owner_uid:
                lead.owner_uid = new_owner
                if new_owner:
                    lead.assigned_at = now
                    notify_owner = new_owner

    title, source_, lid = _lead_title(lead), lead.source, lead.id
    actor = _uname(current)
    await db.commit()
    if notify_owner:
        await push_message(db, to_user_id=notify_owner, kind="info",
                           text=f"【线索分配】{title}（来源：{source_}）已分配给你，请跟进。",
                           biz_type="sales_lead", biz_id=lid)
    if notify_deal:
        await push_message(db, to_role="sales_lead", kind="info",
                           text=f"【线索成交】{title}（{actor}）已成交 🎉",
                           biz_type="sales_lead", biz_id=lid)
    await write_audit(db, user=current, action="update", target_type="sales_lead", target_id=lid)
    rows = await _lead_rows(db, [lead])
    return rows[0]


@router.delete("/{lead_id}", response_model=schemas.Msg)
async def delete_lead(
    lead_id: int,
    current: models.User = Depends(require_roles("sales_lead")),   # 仅主管/管理层
    db: AsyncSession = Depends(get_db),
):
    """删除线索（无效/重复/垃圾询盘清理）。"""
    lead = await _lead_or_404(db, lead_id)
    title = _lead_title(lead)
    await db.delete(lead)
    await db.commit()
    await write_audit(db, user=current, action="delete", target_type="sales_lead",
                      target_id=lead_id, detail=title)
    return schemas.Msg(message="线索已删除")
