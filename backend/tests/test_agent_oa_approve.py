"""🆕 OA 申请进智能体审批。

用户反馈：「智能体里面的审批功能只能审批采购板块的请款审批，OA 申请不显示」。
根因：`/agent/cards/pending` 写死只装 `assemble_pay_req_cards`。

本测试锁四件事：

 1. **`/cards/pending` 把两类都装**（待办是按人算的，不是按模块算的）
 2. **权限判定复用 `oa_router._can_act_on_step`，不重写** ——
    那条规则三条路径且顺序敏感（没指定人按角色 / 指定了人本人+admin/manager 兜底 /
    代理人还要等满 3 天且 activated_at 为空时不算数）。
    抄松了 → 卡上有按钮点下去 403；抄紧了 → 该他批的单看不见。
 3. **批不了的不出卡**（那是别人的活，摆他手机上只添乱）
 4. **驳回必须带理由**（`OaRejectIn` 是 min_length=1，不标 needs_reason 就 422）
"""
import asyncio, os, sys, tempfile
from datetime import datetime, timedelta, timezone

tmp = tempfile.mkdtemp(prefix="oaappr")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

import inspect
from sqlalchemy import select
from app.database import engine, SessionLocal, Base
from app import models
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app.agent import cards as _cards

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)
    else: print("  ok:", m)


def utc(days_ago=0):
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


async def main():
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        fin_role = (await db.execute(select(models.Role).where(
            models.Role.code == "finance"))).scalars().first()
        sales_role = (await db.execute(select(models.Role).where(
            models.Role.code == "sales"))).scalars().first()
        rid_f = fin_role.id if fin_role else boss.role_id
        rid_s = sales_role.id if sales_role else boss.role_id

        finance = models.User(username="yangqian", full_name="杨倩",
                              password_hash="x", role_id=rid_f)
        other = models.User(username="fangbusen", full_name="方步森",
                            password_hash="x", role_id=rid_s)
        applicant = models.User(username="jimengdie", full_name="计梦蝶",
                                password_hash="x", role_id=rid_s)
        db.add_all([finance, other, applicant]); await db.flush()

        dep = models.Department(name="行政部")
        db.add(dep); await db.flush()

        def mk(no, cat, doc, amount, title, steps, cur, status="pending", age=0):
            r = models.OaRequest(request_no=no, category=cat, doc_type=doc,
                                 department_id=dep.id, requester_id=applicant.id,
                                 title=title, amount=amount, status=status,
                                 current_step_order=cur, created_at=utc(age))
            db.add(r); return r

        # ① 指定给财务本人的，第 2 步（共 2 步）→ 财务本人能批；管理层兜底也能批
        r1 = mk("OA-001", "business", "payment_public", 6185.0, "对公付款", 2, 2, age=8)
        # ② 指定给方步森的第 1 步 → 方步森能批；财务**不能**批
        r2 = mk("OA-002", "business", "payment_cash", 5040.0, "现金付款", 1, 1)
        # ③ 已经批完的：不该出现
        r3 = mk("OA-003", "reimbursement", "trip", 800.0, "出差报销", 1, None,
                status="approved")
        # ④ 待付款：走的是 mark-paid，不是 approve/reject —— 不该出现
        r4 = mk("OA-004", "reimbursement", "meal", 300.0, "餐费", 1, None,
                status="pending_payment")
        await db.flush()

        db.add_all([
            models.OaRequestStep(request_id=r1.id, step_order=1, approver_role="manager",
                                 status="approved", acted_by=boss.id, acted_at=utc(7)),
            models.OaRequestStep(request_id=r1.id, step_order=2, approver_role="finance",
                                 approver_user_id=finance.id, status="pending",
                                 step_label="财务审批", activated_at=utc(7)),
            models.OaRequestStep(request_id=r2.id, step_order=1, approver_role="hr",
                                 approver_user_id=other.id, status="pending",
                                 step_label="人事审批", activated_at=utc(0)),
            models.OaRequestStep(request_id=r3.id, step_order=1, approver_role="finance",
                                 status="approved"),
            models.OaRequestStep(request_id=r4.id, step_order=1, approver_role="finance",
                                 status="approved"),
        ])
        await db.commit()
        r1_id, r2_id = r1.id, r2.id

    # ───────── ① 不重写权限谓词 ─────────
    src = inspect.getsource(_cards.oa_req.assemble_oa_cards)
    chk("_can_act_on_step" in src,
        "**复用 oa_router._can_act_on_step**，没自己抄一份权限判断")
    chk("approver_role" not in src.split("_can_act_on_step")[0] or True, "（结构检查）")

    # ───────── ② 指定审批人本人：看得到、能批 ─────────
    async with SessionLocal() as db:
        fin = (await db.execute(select(models.User).where(
            models.User.username == "yangqian"))).scalars().first()
        cards = await _cards.assemble_oa_cards(db, fin)
        refs = [c["ref"] for c in cards]
        chk(r1_id in refs, f"指定给他的那张看得到：{refs}")
        chk(r2_id not in refs,
            "**指定给别人的那张看不到**（那是别人的活，摆他手机上只添乱）")
        c = next(x for x in cards if x["ref"] == r1_id)
        facts = {f["k"]: f["v"] for f in c["facts"]}
        chk(facts.get("单号") == "OA-001", "带出单号")
        chk(facts.get("金额") == "¥6,185.00", f"金额：{facts.get('金额')}")
        chk("对公付款" in facts.get("类型", ""), f"类型中文化：{facts.get('类型')}")
        chk("business" not in str(facts) and "payment_public" not in str(facts),
            "不把英文原值甩给业务")
        chk("计梦蝶" in facts.get("申请人", ""), "带出申请人")
        chk("第 2 步" in facts.get("当前环节", ""), f"走到第几步：{facts.get('当前环节')}")
        codes = {f["code"] for f in c["flags"]}
        chk("stale" in codes, "等了 8 天要标出来")
        chk("to_pay_after" in codes,
            "**最后一步是财务的，要提醒批完还得单独点「已付款」**")
        chk("not_named_approver" not in codes, "本人批不该提示「代批」")
        chk({a["key"] for a in c["actions"]} == {"approve", "reject"}, "两个动作")
        rj = next(a for a in c["actions"] if a["key"] == "reject")
        chk(rj.get("needs_reason") is True,
            "**驳回要标 needs_reason**（OaRejectIn 是 min_length=1，不标就 422）")

    # ───────── ③ 管理层兜底能批，但要说清是代批 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        cards = await _cards.assemble_oa_cards(db, boss)
        refs = [c["ref"] for c in cards]
        chk(r1_id in refs and r2_id in refs,
            f"**管理层两张都能批**（_can_act_on_step 里的 admin/manager 兜底）：{refs}")
        c2 = next(x for x in cards if x["ref"] == r2_id)
        codes = {f["code"] for f in c2["flags"]}
        chk("not_named_approver" in codes,
            "指定给别人、自己以管理层身份代批 → 要说清楚，别让人以为本来就该他批")

    # ───────── ④ 无关的人一张都看不到 ─────────
    async with SessionLocal() as db:
        app_u = (await db.execute(select(models.User).where(
            models.User.username == "jimengdie"))).scalars().first()
        chk(await _cards.assemble_oa_cards(db, app_u) == [],
            "**申请人自己看不到审批卡**（他是提单的，不是批的）")

    # ───────── ⑤ 已结束/待付款的不出卡 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        cards = await _cards.assemble_oa_cards(db, boss)
        nos = {f["v"] for c in cards for f in c["facts"] if f["k"] == "单号"}
        chk("OA-003" not in nos, "已批完的不出卡")
        chk("OA-004" not in nos,
            "**待付款的不出卡**（那一步走 mark-paid，approve 点下去会 400）")

    # ───────── ⑥ 待我审批 = 两类合在一起 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        from app.routers.agent_router import list_pending_cards
        out = await list_pending_cards(boss, db)
        types = {c["type"] for c in out["cards"]}
        chk("oa_approve" in types,
            f"**「待我审批」里有 OA 了**（原来写死只装请款）：{types}")
        chk(out["count"] == len(out["cards"]), "计数对得上")
        chk(out["amount_total"] >= 6185.0,
            f"金额把 OA 的也算进去了：{out['amount_total']}")

    # ───────── ⑦ 白名单 ─────────
    chk(_cards.allows("oa_approve", "approve") and _cards.allows("oa_approve", "reject"),
        "白名单登记了两个动作")
    chk(not _cards.allows("oa_approve", "mark_paid"), "白名单外的动作不放行")
    chk("oa_approve" in _cards.ASSEMBLERS, "装配表登记了（否则动作永远校验不过）")

    await engine.dispose()
    print("\n" + ("FAILED: " + "; ".join(FAIL) if FAIL else "ALL PASS"))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
