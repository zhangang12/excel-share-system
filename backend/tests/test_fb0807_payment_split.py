"""反馈 2026-08-07（杨坛）：付款申请拆成「对公付款」和「现金付款」——审批流程不一样。

要锁死的：
  1. 两个新单据类型建出来了
  2. **旧 payment 的审批链要复制到两个新类型**——光加类型不够，
     新类型没有链，一提交就被「尚未配置审批流程」挡住，功能上线即不可用
  3. 旧 payment **停用不删**：在途旧单要走完，历史单据也要留着类型名
  4. 对公付款仍然要求收款账号+开户行（#348）
  5. **现金付款不要求账户**——现金根本没有账号和开户行，
     一刀切要求填只会逼人瞎填一个，比不填更糟
  6. 迁移幂等：跑第二次不会把链复制成两份
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="paysplit")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, func
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns, split_payment_doc_type
from app import models

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        await c.post("/api/admin/users", headers=H, json={
            "username": "sal", "password": "pass123", "full_name": "销售小张", "role_id": rid["sales"]})
        r = await c.post("/api/auth/login", json={"username": "sal", "password": "pass123"})
        Hs = {"Authorization": f"Bearer {r.json()['access_token']}"}

        docs = {d["key"]: d for d in (await c.get("/api/oa/doc-types", headers=H)).json()}
        chk("payment_public" in docs and docs["payment_public"]["label"] == "对公付款申请",
            f"1) 有对公付款申请: {docs.get('payment_public', {}).get('label')}")
        chk("payment_cash" in docs and docs["payment_cash"]["label"] == "现金付款申请",
            f"1) 有现金付款申请: {docs.get('payment_cash', {}).get('label')}")
        chk("payment" in docs and docs["payment"]["enabled"] is False,
            f"3) 旧 payment 停用但没删: enabled={docs.get('payment', {}).get('enabled')}")

        depts = (await c.get("/api/oa/departments", headers=H)).json()
        dept = [d for d in depts if d["name"] == "销售部"][0]

        # ===== 2) 审批链复制 =====
        # 先给旧 payment 配一条链，再跑一次迁移，看有没有复制过去
        async with SessionLocal() as db:
            await db.execute(models.OaApprovalStep.__table__.delete())
            db.add(models.OaApprovalStep(department_id=dept["id"], doc_type="payment",
                                         step_order=1, approver_role="manager",
                                         step_label="管理层", enabled=True))
            await db.commit()
            res = await split_payment_doc_type(db)
        chk(res.get("copied") == 2, f"2) 旧链复制到两个新类型（各 1 条，共 2）: {res}")

        for k, label in (("payment_public", "对公"), ("payment_cash", "现金")):
            r = await c.get("/api/oa/chains", headers=H, params={"department_id": dept["id"], "doc_type": k})
            chk(len(r.json()) == 1, f"2) {label}有审批链，能直接提单: {len(r.json())} 步")

        # 6) 幂等：再跑一次不能复制成两份
        async with SessionLocal() as db:
            res2 = await split_payment_doc_type(db)
        chk(res2.get("copied") == 0, f"6) 迁移幂等，第二次不再复制: {res2}")
        r = await c.get("/api/oa/chains", headers=H,
                        params={"department_id": dept["id"], "doc_type": "payment_public"})
        chk(len(r.json()) == 1, f"6) 链没有被复制成两份: {len(r.json())} 步")

        base = {"category": "business", "department_id": dept["id"], "amount": 5000}
        det = {"payee": "无锡某某机械有限公司", "reason": "6月份货款"}

        # ===== 4) 对公仍要账户 =====
        r = await c.post("/api/oa/requests", headers=Hs, json={
            **base, "doc_type": "payment_public", "title": "对公付款", "detail": det})
        chk(r.status_code == 400 and "收款账号" in r.text, f"4) 对公缺账号被拒: {r.status_code} {r.text[:80]}")

        r = await c.post("/api/oa/requests", headers=Hs, json={
            **base, "doc_type": "payment_public", "title": "对公付款",
            "detail": {**det, "payee_account": "6222021234567890", "payee_bank": "工行无锡分行"}})
        chk(r.status_code == 200, f"4) 对公账户填全可提交: {r.status_code} {r.text[:120]}")

        # ===== 5) 现金不要求账户 =====
        r = await c.post("/api/oa/requests", headers=Hs, json={
            **base, "doc_type": "payment_cash", "title": "现金付款", "detail": det})
        chk(r.status_code == 200, f"5) 现金不填账户也能提交（现金没有账户）: {r.status_code} {r.text[:120]}")

        # 现金仍然要收款单位/金额/事由——这三样跟付款方式无关
        r = await c.post("/api/oa/requests", headers=Hs, json={
            **base, "doc_type": "payment_cash", "title": "现金付款",
            "detail": {"payee": "", "reason": "x"}})
        chk(r.status_code == 400 and "收款单位" in r.text, "5) 现金仍要收款单位")
        r = await c.post("/api/oa/requests", headers=Hs, json={
            **base, "doc_type": "payment_cash", "title": "现金付款", "amount": 0,
            "detail": det})
        chk(r.status_code == 400 and "金额" in r.text, "5) 现金仍要付款金额")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
