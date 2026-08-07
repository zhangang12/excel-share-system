"""反馈#348（杨坛）：OA 付款申请里加收款账户。

原话：「OA付款申请里面加一下收款账户」。他同时是 finance_lead——
批完这张单要真去打款，而单子上只有收款单位、没有账号和开户行，
只能回头一个个问，钱就压在那儿。

要锁死的：
  1. 收款账号 + 开户行都必填，**少一样就拒**——少一样照样打不出款，
     等于这张单还得再走一轮，那这个字段就白加了。
  2. 校验必须在**服务端**：只拦前端的话，H5、旧客户端、直接打接口都能绕过去。
  3. 传全了要能存下来，并且在详情里读得到（财务是从详情里抄账号去付款的）。
  4. **别的单据类型不受影响**——付款申请的必填不能溢出到差旅/报销上。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb348")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns

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

        depts = (await c.get("/api/oa/departments", headers=H)).json()
        dept = [d for d in depts if d["name"] == "销售部"][0]
        docs = (await c.get("/api/oa/doc-types", headers=H)).json()
        pay = [d for d in docs if d["key"] == "payment_public"]
        chk(bool(pay), f"有「对公付款申请」单据类型: {[d['key'] for d in docs][:8]}")
        if not pay:
            return
        pay = pay[0]
        # 配一条审批链，否则提交会先被「尚未配置审批流程」挡掉，测不到账户校验
        await c.post("/api/oa/chains", headers=H, json={
            "department_id": dept["id"], "doc_type": "payment_public", "step_order": 1,
            "approver_role": "manager", "enabled": True})

        base = {"category": pay["category"], "doc_type": "payment_public",
                "department_id": dept["id"], "title": "付供应商货款", "amount": 5000}

        # 1+2) 少账号 / 少开户行都要被服务端拒
        r = await c.post("/api/oa/requests", headers=Hs, json={
            **base, "detail": {"payee": "无锡某某公司", "reason": "采购款"}})
        chk(r.status_code == 400 and "收款账号" in r.text, f"缺账号被拒: {r.status_code} {r.text[:90]}")

        r = await c.post("/api/oa/requests", headers=Hs, json={
            **base, "detail": {"payee": "无锡某某公司", "reason": "采购款",
                               "payee_account": "6222021234567890"}})
        chk(r.status_code == 400 and "开户行" in r.text, f"只有账号没开户行也被拒: {r.status_code} {r.text[:90]}")

        # 空白字符不算填了
        r = await c.post("/api/oa/requests", headers=Hs, json={
            **base, "detail": {"payee": "无锡某某公司", "reason": "采购款",
                               "payee_account": "   ", "payee_bank": "   "}})
        chk(r.status_code == 400, f"填空格不算填: {r.status_code}")

        # 3) 传全了能存能读
        r = await c.post("/api/oa/requests", headers=Hs, json={
            **base, "detail": {"payee": "无锡某某机械有限公司", "reason": "6月份采购款",
                               "payee_account": "6222021234567890",
                               "payee_bank": "工商银行无锡分行营业部",
                               "expect_pay_date": "2026-08-20"}})
        chk(r.status_code == 200, f"账户填全能提交: {r.status_code} {r.text[:120]}")
        if r.status_code != 200:
            return
        rid_ = r.json()["id"]
        d = (await c.get(f"/api/oa/requests/{rid_}", headers=H)).json()
        det = d.get("detail") or {}
        chk(det.get("payee_account") == "6222021234567890", f"账号存下来了: {det.get('payee_account')}")
        chk(det.get("payee_bank") == "工商银行无锡分行营业部", f"开户行存下来了: {det.get('payee_bank')}")
        chk(det.get("payee") == "无锡某某机械有限公司", "原有的收款单位没被弄丢")

        # 4) 别的单据类型不受影响——付款申请的必填不能溢出
        other = [x for x in docs if x["key"] not in ("payment", "payment_public", "payment_cash") and x["enabled"]]
        if other:
            o = other[0]
            await c.post("/api/oa/chains", headers=H, json={
                "department_id": dept["id"], "doc_type": o["key"], "step_order": 1,
                "approver_role": "manager", "enabled": True})
            r = await c.post("/api/oa/requests", headers=Hs, json={
                "category": o["category"], "doc_type": o["key"],
                "department_id": dept["id"], "title": "别的单子", "amount": 100,
                "detail": {}})
            chk(r.status_code == 200,
                f"「{o['label']}」不受付款申请必填影响: {r.status_code} {r.text[:100]}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
