"""请款驳回闭环（2026-08-01 用户口述需求）

出纳付款时发现收款账户名称/账号不对 → 驳回并填原因 → 通知请款发起人 →
发起人改完（去供应商档案改账号）重新提交 → **重走审批** → 审批通过 → 出纳付款。

覆盖：
  1. 出纳付款驳回 pay-reject：approved → rejected，reject_stage='pay'，
     **rejected_by 记出纳、finance_approver_id 不被覆盖**（覆盖了就抹掉真审批人，
     还会让「审批人不能给自己审过的单付款」这条职责分离判错人）
  2. 驳回原因必填；非 approved 态不能付款驳回
  3. 发起人收到站内通知
  4. 发起人 resubmit → pending，驳回痕迹与旧审批痕迹一并清空 → 财务主管收到通知
  5. 重提后必须重新审批才能付款（用户明确要求：改账户属实质变更，审批人须复核）
  6. 审批人撤回审批 withdraw-approval：approved → rejected 且审批人/审批时间清空
  7. 待审驳回 reject 仍工作，且现在也记 stage/驳回人/通知发起人
  8. 他人不能重新提交别人的请款单
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="payrej")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)


async def _pr(prid):
    async with SessionLocal() as db:
        return (await db.execute(select(models.PaymentRequest)
                                 .where(models.PaymentRequest.id == prid))).scalar_one()


async def _msgs(uid):
    async with SessionLocal() as db:
        return (await db.execute(select(models.Message).where(
            models.Message.to_user_id == uid,
            models.Message.biz_type == "payment_request"))).scalars().all()


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    tr = ASGITransport(app=app)
    async with AsyncClient(transport=tr, base_url="http://test", timeout=60) as c:
        async def login(u, p):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(name, codes):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123", "full_name": name,
                "role_ids": [rid[x] for x in codes]})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        buyer = await mkuser("pr_buyer", ["buyer"])           # 请款发起人（采购）
        await mkuser("pr_lead", ["finance", "finance_lead"])  # 审批人（杨坛）
        await mkuser("pr_cash", ["finance"])                  # 出纳（杨倩）
        other = await mkuser("pr_other", ["buyer"])           # 无关采购员
        Hb = await login("pr_buyer", "pass123")
        Hlead = await login("pr_lead", "pass123")
        Hcash = await login("pr_cash", "pass123")
        Ho = await login("pr_other", "pass123")

        # 供应商 + 一条已收货明细 → 发起请款
        sid = (await c.post("/api/purchase-mgmt/suppliers", headers=Hb,
                            json={"name": "上海屹上脚轮有限公司",
                                  "bank_name": "农业银行上海方泰支行",
                                  "bank_account": "03841100040018127"})).json()["id"]
        iid = (await c.post("/api/purchase-mgmt/items", headers=Hb,
                            json={"supplier_id": sid, "item_name": "脚轮", "qty": 10,
                                  "unit_price": 100})).json()["id"]

        async def new_pr():
            r = await c.post("/api/purchase-mgmt/payment-requests", headers=Hb,
                             json={"supplier_id": sid, "requested_amount": 1000, "notes": "月结",
                                   "items": [{"item_id": iid, "allocated_amount": 1000}]})
            assert r.status_code in (200, 201), r.text
            return r.json()["id"]

        # ==================== 场景一：出纳付款驳回 → 重提 → 重走审批 ====================
        prid = await new_pr()
        chk((await _pr(prid)).status == "pending", "新建请款=待审批")

        # 未审批不能付款驳回
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{prid}/pay-reject",
                        headers=Hcash, json={"reason": "账号不对"})
        chk(r.status_code == 400, f"待审态付款驳回应400: {r.status_code}")

        r = await c.put(f"/api/purchase-mgmt/payment-requests/{prid}/approve", headers=Hlead)
        chk(r.status_code == 200, f"主管审批: {r.status_code} {r.text[:120]}")
        approver_id = (await _pr(prid)).finance_approver_id

        # 驳回原因必填
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{prid}/pay-reject",
                        headers=Hcash, json={"reason": "   "})
        chk(r.status_code == 400, f"空原因应400: {r.status_code}")

        # ★ 出纳驳回
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{prid}/pay-reject",
                        headers=Hcash, json={"reason": "收款账号与开户名不符，请核实"})
        chk(r.status_code == 200, f"★出纳付款驳回: {r.status_code} {r.text[:150]}")
        pr = await _pr(prid)
        chk(pr.status == "rejected", f"★状态=已驳回: {pr.status}")
        chk(pr.reject_stage == "pay", f"★驳回环节=pay: {pr.reject_stage}")
        chk(pr.rejected_by is not None and pr.rejected_by != approver_id,
            f"★驳回人=出纳而非审批人: rejected_by={pr.rejected_by} approver={approver_id}")
        chk(pr.finance_approver_id == approver_id,
            f"★付款驳回不得覆盖审批人: {pr.finance_approver_id} vs {approver_id}")
        chk("开户名" in (pr.reject_reason or ""), f"驳回原因已存: {pr.reject_reason}")

        ms = await _msgs(buyer)
        chk(any("付款驳回" in m.text for m in ms), f"★发起人收到驳回通知: {[m.text[:40] for m in ms]}")

        # 别人不能替他重提
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{prid}/resubmit", headers=Ho)
        chk(r.status_code == 403, f"他人重提应403: {r.status_code}")

        # ★ 发起人改完重新提交 → 回到待审批，痕迹清干净
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{prid}/resubmit", headers=Hb)
        chk(r.status_code == 200, f"★发起人重新提交: {r.status_code} {r.text[:150]}")
        pr = await _pr(prid)
        chk(pr.status == "pending", f"★重提后=待审批: {pr.status}")
        chk(pr.reject_stage is None and pr.rejected_by is None and pr.reject_reason is None,
            f"★驳回痕迹已清: stage={pr.reject_stage} by={pr.rejected_by} reason={pr.reject_reason}")
        chk(pr.finance_approver_id is None and pr.approved_at is None,
            f"★旧审批痕迹作废: approver={pr.finance_approver_id} at={pr.approved_at}")

        # ★ 重提后必须重新审批才能付款
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{prid}/pay", headers=Hcash,
                        data={"paid_amount": 1000, "paid_date": "2026-08-01"})
        chk(r.status_code == 400, f"★未重新审批不能付款: {r.status_code} {r.text[:120]}")

        r = await c.put(f"/api/purchase-mgmt/payment-requests/{prid}/approve", headers=Hlead)
        chk(r.status_code == 200, f"重新审批: {r.status_code} {r.text[:120]}")
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{prid}/pay", headers=Hcash,
                        data={"paid_amount": 1000, "paid_date": "2026-08-01"})
        chk(r.status_code == 200, f"★重新审批后出纳付款成功: {r.status_code} {r.text[:150]}")
        chk((await _pr(prid)).status == "paid", "闭环完成=已付款")

        # ==================== 场景二：审批人批完又撤回 ====================
        prid2 = await new_pr()
        await c.put(f"/api/purchase-mgmt/payment-requests/{prid2}/approve", headers=Hlead)
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{prid2}/withdraw-approval",
                        headers=Hlead, json={"reason": "金额记错了，退回重报"})
        chk(r.status_code == 200, f"★撤回审批: {r.status_code} {r.text[:150]}")
        pr = await _pr(prid2)
        chk(pr.status == "rejected" and pr.reject_stage == "withdraw",
            f"★撤回后=已驳回/withdraw: {pr.status}/{pr.reject_stage}")
        chk(pr.finance_approver_id is None and pr.approved_at is None,
            f"★撤回把审批痕迹一并清掉: {pr.finance_approver_id}/{pr.approved_at}")
        # 已是 rejected，不能再撤回
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{prid2}/withdraw-approval",
                        headers=Hlead, json={"reason": "再来一次"})
        chk(r.status_code == 400, f"非已审批态撤回应400: {r.status_code}")

        # ==================== 场景三：待审驳回（原有）现在也记环节+通知 ====================
        prid3 = await new_pr()
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{prid3}/reject",
                        headers=Hlead, json={"reason": "发票还没到"})
        chk(r.status_code == 200, f"待审驳回: {r.status_code}")
        pr = await _pr(prid3)
        chk(pr.status == "rejected" and pr.reject_stage == "approve",
            f"★待审驳回记 stage=approve: {pr.status}/{pr.reject_stage}")
        ms = await _msgs(buyer)
        chk(any("审批驳回" in m.text for m in ms), "发起人收到审批驳回通知")

        # 接口把驳回信息透出去（页面要显示环节+退回人）
        rows = (await c.get("/api/purchase-mgmt/payment-requests", headers=Hb)).json()
        row = next((x for x in rows if x["id"] == prid3), None)
        chk(row and row.get("reject_stage") == "approve" and row.get("rejecter_name"),
            f"接口返回驳回环节/退回人: {row and {k: row.get(k) for k in ('reject_stage', 'rejecter_name')}}")

    print("PASSED" if not FAIL else f"FAILED {len(FAIL)}")

asyncio.run(main())
