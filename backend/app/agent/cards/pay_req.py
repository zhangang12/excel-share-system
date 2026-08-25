"""请款审批卡（type = pay_req_approve）。

第一期只做这一类，依据是生产数据：杨坛做过 40 笔请款审批（¥80,260），
是第二名订单审批（9 笔）的 4 倍多。

原则二在这里的落地：facts 全部由本模块用 current 重新查库拼装，
模型只能指定 ref（哪一条），碰不到金额、账号、供应商名。
"""
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ... import models
from ...routers.purchase_mgmt_router import _buyer_restricted
from . import token as card_token

# 金额异常判据：高于该供应商历史均值这么多倍就标出来。
# 4 倍是拍的——真正的阈值该由业务定，见下方 _amount_flag 的注释。
_AMOUNT_RATIO = 4.0
_MIN_HISTORY = 3          # 少于这么多条历史，均值没有意义，不出这个标


def _mask_account(acct: str | None) -> str:
    """收款账号只露后 4 位。完整账号不进卡片、也不进喂给模型的上下文。"""
    a = (acct or "").strip()
    if len(a) <= 4:
        return a or "—"
    return f"…{a[-4:]}"


def _brief(items: list[str], limit: int = 3) -> str:
    """采购明细名称摘要。手机上放不下十几条，取前几个 + 「等 N 项」。"""
    if not items:
        return "—"
    head = "、".join(items[:limit])
    return head if len(items) <= limit else f"{head} 等 {len(items)} 项"


def _money(x) -> str:
    return f"¥{float(x or 0):,.2f}"


async def pending_pay_reqs(db: AsyncSession, current: models.User) -> list[models.PaymentRequest]:
    """当前用户视角下的待审请款单。

    行级隔离与 /api/purchase-mgmt/payment-requests 同口径：采购员受限时只看自己相关的。
    注意这里刻意 **不** 过滤掉「自己提交的」——那种单要出现在列表里但按钮置灰，
    否则用户会以为单子丢了（手册 3.5.3：宁可说明原因，不要静默消失）。
    """
    stmt = (select(models.PaymentRequest)
            .where(models.PaymentRequest.status == "pending")
            .order_by(models.PaymentRequest.id.desc()))
    if _buyer_restricted(current):
        sub = (select(models.PurchaseItem.id)
               .where(models.PurchaseItem.buyer_id == current.id))
        # ⚠️ 关联字段是 `item_id`，**不是** `purchase_item_id`（那是 WhTxn 上的列）。
        #   写错不会在导入时报错，只在**受限采购员**真去拿卡片/简报时抛 AttributeError → 500，
        #   管理员因为走不到这个分支永远测不出来。本文件 129 行早写过同一条警告、
        #   下面那段也改对了，唯独漏了这里；2026-08-22 线上实测王芹/李新新两个账号全炸。
        mine = (select(models.PaymentRequestItem.request_id)
                .where(models.PaymentRequestItem.item_id.in_(sub)))
        stmt = stmt.where(models.PaymentRequest.id.in_(mine))
    return list((await db.execute(stmt)).scalars().all())


async def _amount_flag(db: AsyncSession, pr: models.PaymentRequest) -> dict | None:
    """金额 vs 该供应商历史均值。历史不足 3 条就不出标——
    两条数据算出来的「均值」经不起看，标出来只会让人不再信任所有标记。"""
    rows = (await db.execute(
        select(models.PaymentRequest.requested_amount).where(
            models.PaymentRequest.supplier_id == pr.supplier_id,
            models.PaymentRequest.id != pr.id,
            models.PaymentRequest.status.in_(("approved", "paid")),
        ))).scalars().all()
    vals = [float(v) for v in rows if v]
    if len(vals) < _MIN_HISTORY:
        return None
    avg = statistics.fmean(vals)
    if avg <= 0:
        return None
    ratio = float(pr.requested_amount or 0) / avg
    if ratio < _AMOUNT_RATIO:
        return None
    return {"code": "amount_outlier", "level": "warn",
            "msg": f"金额为该供应商历史均值的 {ratio:.1f} 倍"
                   f"（均值 {_money(avg)}，历史 {len(vals)} 笔）"}


async def _bank_changed_flag(db: AsyncSession, pr: models.PaymentRequest) -> dict | None:
    """收款账号近期变更。

    ⚠️ 这条依赖 audit_logs 里 target_type='supplier' 的改动留痕。
    该留痕是 2026-08-03 才补上的（此前 update_supplier 根本不写审计），
    所以对历史数据一律返回 None——不是没变过，是查不到。
    宁可不出标，也不能出一个「查过了，没问题」的假象。
    """
    since = datetime.now(timezone.utc) - timedelta(days=14)
    hit = (await db.execute(
        select(models.AuditLog).where(
            models.AuditLog.target_type == "supplier",
            models.AuditLog.target_id == pr.supplier_id,
            models.AuditLog.action == "update_supplier_bank",
            models.AuditLog.created_at >= since,
        ).order_by(models.AuditLog.created_at.desc()).limit(1))).scalar_one_or_none()
    if not hit:
        return None
    days = (datetime.now(timezone.utc) - hit.created_at.replace(
        tzinfo=hit.created_at.tzinfo or timezone.utc)).days
    return {"code": "bank_changed", "level": "warn",
            "msg": f"收款账号 {days} 天前被 {hit.username or '—'} 修改过"}


async def assemble_pay_req_cards(db: AsyncSession, current: models.User,
                                 refs: list[int] | None = None) -> list[dict]:
    """装配请款审批卡。refs 为空则取当前用户全部待审。

    flags 逐条对应 approve 端点里的 raise（手册 3.5.3）：
      purchase_mgmt_router.py:2576  status != pending      → not_pending  (block)
      purchase_mgmt_router.py:2581  requester_id == 自己   → self_submitted (block)
    端点里有几条前置校验，这里就该有几条 flag；漏一条，用户就会点下去吃 400。
    """
    prs = await pending_pay_reqs(db, current)
    if refs is not None:
        want = set(refs)
        prs = [p for p in prs if p.id in want]

    # 🆕 反馈：请款审批要能看见**这笔钱花在哪个项目上** —— 卡片原来只有
    #    供应商/金额/账号/提交人，审批人没法判断该不该批。
    #    口径与网页端财务请款审批列表的「项目编号」列同源
    #    （purchase_mgmt_router._pr_out：请款单关联采购明细的 project_code，去重排序）。
    #    ⚠️ 关联字段是 PaymentRequestItem.item_id，不是 purchase_item_id。
    codes: dict[int, list[str]] = {}
    names: dict[int, list[str]] = {}
    if prs:
        rows = (await db.execute(
            select(models.PaymentRequestItem.request_id,
                   models.PurchaseItem.project_code,
                   models.PurchaseItem.item_name)
            .join(models.PurchaseItem,
                  models.PurchaseItem.id == models.PaymentRequestItem.item_id)
            .where(models.PaymentRequestItem.request_id.in_([p.id for p in prs])))).all()
        for rid, code, item in rows:
            if code:
                codes.setdefault(rid, [])
                if code not in codes[rid]:
                    codes[rid].append(code)
            if item:
                names.setdefault(rid, [])
                if item not in names[rid]:
                    names[rid].append(item)
        for d in (codes, names):
            for k in d:
                d[k].sort()

    cards = []
    for pr in prs:
        sup = pr.supplier
        requester = pr.requester

        flags: list[dict] = []
        if pr.status != "pending":
            flags.append({"code": "not_pending", "level": "block",
                          "msg": f"该单当前状态为「{pr.status}」，只有待审状态可审批"})
        if pr.requester_id and pr.requester_id == current.id:
            flags.append({"code": "self_submitted", "level": "block",
                          "msg": "职责分离：这是你自己提交的请款单，需由另一位审批人处理"})
        for f in (await _bank_changed_flag(db, pr), await _amount_flag(db, pr)):
            if f:
                flags.append(f)

        blocked = {f["code"] for f in flags if f["level"] == "block"}
        disabled_by = next(iter(blocked), None)

        cards.append({
            "type": "pay_req_approve",
            "ref": pr.id,
            "token": card_token.issue(current.id, "pay_req_approve", pr.id),
            # facts 一律后端重查后回填；模型拿不到这里任何一个值的写权
            "facts": [
                # 项目编号摆在最前：审批人第一眼要知道「这笔钱花在哪个项目上」。
                # 多个编号逗号拼接；一条都没有时明说，别留空让人以为漏了
                {"k": "项目", "v": "、".join(codes.get(pr.id) or []) or "（未关联项目）"},
                {"k": "供应商", "v": sup.name if sup else f"#{pr.supplier_id}"},
                {"k": "请款金额", "v": _money(pr.requested_amount), "emphasis": True},
                {"k": "采购内容", "v": _brief(names.get(pr.id) or [])},
                {"k": "收款账号",
                 "v": f"{(sup.bank_name or '') if sup else ''} {_mask_account(sup.bank_account if sup else None)}".strip(),
                 "sensitive": True},
                {"k": "提交人", "v": (requester.full_name or requester.username) if requester else "—"},
                {"k": "单号", "v": f"#{pr.id}"},
            ],
            "flags": flags,
            "actions": [
                {"key": "approve", "primary": True, "disabled_by": disabled_by},
                {"key": "reject", "primary": False, "disabled_by": disabled_by,
                 "needs_reason": True},
            ],
        })
    return cards
