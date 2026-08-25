"""🆕 OA 申请审批卡（type = oa_approve）。

用户反馈：「智能体里面的审批功能只能审批采购板块的请款审批，OA 申请不显示」。
`/agent/cards/pending` 原来写死只装 `assemble_pay_req_cards`，所以 OA 一条都出不来。

对应端点：
  · 同意  `PUT /api/oa/requests/{rid}/approve`  body: {note?, settle_amount?}
  · 驳回  `PUT /api/oa/requests/{rid}/reject`   body: {reason}  ← reason 必填

⚠️⚠️ **权限判定一律复用 `oa_router._can_act_on_step`，这里一行都不重写。**
   那条规则有三条路径且顺序敏感（没指定人按角色 / 指定了人本人+admin+manager 兜底 /
   代理人还要等满 3 天且 `activated_at` 为空时不算数）。自己抄一遍必然抄漏：
   抄松了 → 卡片上有按钮、点下去 403；抄紧了 → 该他批的单在手机上看不见。
   `tools_entity` 的模块注释里写着同一条纪律（越权就是那么来的）。

⚠️ OA 是**多步**审批，和请款那种单步不一样：`ref` 是申请单 id，但能不能批
   取决于**当前步**（`current_step_order` 指向的那一步）。所以判定必须拿当前步去问。
"""
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from ... import models
from . import token as card_token

_MAX_CARDS = 20

# 类别/单据类型的中文名。给业务看 business / payment_public 等于没说。
_CAT_CN = {"business": "业务申请", "reimbursement": "报销", "purchase": "采购申请"}
_DOC_CN = {
    "trip": "出差", "hospitality": "招待", "company_car": "用车", "meal": "餐费",
    "payment_public": "对公付款", "payment_cash": "现金付款", "office": "办公用品",
    "training": "培训", "other": "其他",
}


def _money(x) -> str:
    try:
        return f"¥{float(x or 0):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _age_days(d: datetime | None) -> int | None:
    if not d:
        return None
    ts = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return max(0, (date.today() - ts.astimezone(timezone.utc).date()).days)


async def pending_oa_requests(db: AsyncSession, current: models.User,
                              refs: list[int] | None = None) -> list[models.OaRequest]:
    """当前用户能批的 OA 申请。

    ⚠️ 只取 `status='pending'`：`pending_payment`（审批完等财务打钱）是另一回事，
       那一步走的是 `mark-paid` 端点，动作和这里的 approve/reject 不是一套。
       混进来的话卡片上会出现点了必然 400 的按钮。
    """
    q = (select(models.OaRequest)
         .options(selectinload(models.OaRequest.steps)
                  .joinedload(models.OaRequestStep.approver),
                  joinedload(models.OaRequest.requester),
                  joinedload(models.OaRequest.department))
         .where(models.OaRequest.status == "pending")
         .order_by(models.OaRequest.created_at))
    if refs is not None:
        q = q.where(models.OaRequest.id.in_(refs))
    return list((await db.execute(q)).unique().scalars().all())


async def assemble_oa_cards(db: AsyncSession, current: models.User,
                            refs: list[int] | None = None) -> list[dict]:
    """flags 逐条对上端点的 raise（oa_router.approve_request / reject_request）：

      · `status != 'pending'`                → 400「该申请已结束」→ 这里根本不取
      · 当前步不存在 / 不是 pending           → 400「当前没有待处理的步骤」→ no_step (block)
      · `_can_act_on_step` 不通过              → 403 → 这一条**直接不出卡**（见下）

    ⚠️ 批不了的**不出卡**，而不是出个灰按钮。请款那边刻意保留了「自己提交的」
       并把按钮置灰（怕用户以为单子丢了），但那是**他自己的单**；
       OA 待批的单指定给别人时，那是**别人的活**，摆在他手机上只会添乱。
    """
    from ...routers.oa_router import _can_act_on_step, _my_principal_ids

    principals = await _my_principal_ids(db, current)
    cards: list[dict] = []
    for req in (await pending_oa_requests(db, current, refs)):
        steps = sorted(req.steps or [], key=lambda s: s.step_order)
        cur = next((s for s in steps if s.step_order == req.current_step_order), None)

        # ⚠️ 权限：复用业务侧谓词。不通过就跳过，别出卡。
        if cur is None or cur.status != "pending":
            continue
        if not _can_act_on_step(cur, current, principals):
            continue

        who = (req.requester.full_name or req.requester.username) if req.requester else "—"
        dept = req.department.name if req.department else ""
        cat = _CAT_CN.get(req.category, req.category or "")
        doc = _DOC_CN.get(req.doc_type, req.doc_type or "")

        facts = [
            {"k": "单号", "v": req.request_no or f"#{req.id}"},
            {"k": "类型", "v": f"{cat}·{doc}" if doc else cat},
            {"k": "申请人", "v": who + (f"（{dept}）" if dept else "")},
            {"k": "金额", "v": _money(req.amount), "emphasis": True},
        ]
        if req.title:
            facts.append({"k": "事由", "v": req.title[:40]})
        age = _age_days(req.created_at)
        if age is not None:
            facts.append({"k": "已等", "v": f"{age} 天"})
        # 走到第几步 —— 让人知道自己是最后一关还是中间一关
        facts.append({"k": "当前环节",
                      "v": f"第 {cur.step_order} 步"
                           + (f"·{cur.step_label}" if cur.step_label else "")
                           + f"（共 {len(steps)} 步）"})

        flags: list[dict] = []
        # 指定给别人、我靠 admin/manager 兜底批的：得说清楚，别让人以为本来就该他批
        if cur.approver_user_id and cur.approver_user_id != current.id:
            named = (cur.approver.full_name or cur.approver.username) if cur.approver else "别人"
            flags.append({"code": "not_named_approver", "level": "warn",
                          "msg": f"这一步指定的是 {named}，你是以管理层身份代批"})
        if age is not None and age >= 7:
            flags.append({"code": "stale", "level": "warn",
                          "msg": f"已经等了 {age} 天"})
        # 报销/付款类最后一步是财务的，批完还要财务单独点「已付款」，别以为批了钱就出去了
        if not any(s.step_order > cur.step_order for s in steps) and \
                cur.approver_role == "finance":
            flags.append({"code": "to_pay_after", "level": "warn",
                          "msg": "这是最后一步：批完进「待付款」，财务还要再点一次"
                                 "「标记已付款」，钱才算真出去"})

        cards.append({
            "type": "oa_approve",
            "ref": req.id,
            "token": card_token.issue(current.id, "oa_approve", req.id),
            "facts": facts,
            "flags": flags,
            "actions": [
                {"key": "approve", "primary": True},
                # ⚠️ reject 的 reason 是**必填**（schemas.OaRejectIn 用了 min_length=1），
                #    不标 needs_reason 的话前端不弹输入框，点下去 422。
                {"key": "reject", "primary": False, "needs_reason": True},
            ],
        })
        if len(cards) >= _MAX_CARDS:
            break
    return cards
