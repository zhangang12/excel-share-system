"""反馈#365（计梦蝶）：「建议使用过的收款账户有保存功能，下次付款同一收款单位的话较为便捷」。

同一个供应商每月都要付款，收款账号每次手敲——又长又容易敲错，敲错了钱就打飞了。

要锁死的：
  1. 历史付款申请里用过的收款单位能被列出来，带上次的账号+开户行
  2. 同一单位换过账号 → **以最近一次为准**（否则会把旧账号推给人，正是最危险的错）
  3. 按使用次数排序，常用的排前面
  4. 现金付款没有银行账户，不能混进来
  5. 没填账号的单据不进账户库（列一个空账号出来等于噪音）
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb365")
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
            "username": "sal9", "password": "pass123", "full_name": "销售小张", "role_id": rid["sales"]})
        r = await c.post("/api/auth/login", json={"username": "sal9", "password": "pass123"})
        Hs = {"Authorization": f"Bearer {r.json()['access_token']}"}

        depts = (await c.get("/api/oa/departments", headers=H)).json()
        dept = [d for d in depts if d["name"] == "销售部"][0]
        for dt in ("payment_public", "payment_cash"):
            await c.post("/api/oa/chains", headers=H, json={
                "department_id": dept["id"], "doc_type": dt, "step_order": 1,
                "approver_role": "manager", "enabled": True})

        async def submit(doc_type, payee, acct=None, bank=None):
            detail = {"payee": payee, "reason": "货款"}
            if acct is not None:
                detail["payee_account"] = acct
            if bank is not None:
                detail["payee_bank"] = bank
            return await c.post("/api/oa/requests", headers=Hs, json={
                "category": "business", "doc_type": doc_type, "department_id": dept["id"],
                "title": f"付{payee}", "amount": 1000, "detail": detail})

        # 甲公司付了两次，第二次换了账号
        r = await submit("payment_public", "无锡甲机械", "6222000000000001", "工行无锡分行")
        chk(r.status_code == 200, f"甲公司第一次: {r.status_code} {r.text[:80]}")
        r = await submit("payment_public", "无锡甲机械", "6222999999999999", "建行无锡分行")
        chk(r.status_code == 200, f"甲公司第二次（换账号）: {r.status_code}")
        # 乙公司只付过一次
        await submit("payment_public", "苏州乙电气", "6228000000000002", "农行苏州分行")
        # 现金付款：没有银行账户
        await submit("payment_cash", "丙五金店")

        r = await c.get("/api/oa/payee-accounts", headers=Hs)
        chk(r.status_code == 200, f"接口通: {r.status_code} {r.text[:80]}")
        rows = r.json()
        by = {x["payee"]: x for x in rows}

        chk("无锡甲机械" in by and "苏州乙电气" in by, f"1) 用过的收款单位都列出来了: {list(by)}")
        chk(by["无锡甲机械"]["account"] == "6222999999999999",
            f"2) 换过账号 → 给最近一次的（给旧账号钱就打飞了）: {by['无锡甲机械']['account']}")
        chk(by["无锡甲机械"]["bank"] == "建行无锡分行",
            f"2) 开户行也跟着最近一次: {by['无锡甲机械']['bank']}")
        chk(by["无锡甲机械"]["used"] == 2, f"3) 用过 2 次: {by['无锡甲机械']['used']}")
        chk(rows[0]["payee"] == "无锡甲机械", f"3) 常用的排前面: {[x['payee'] for x in rows]}")
        chk("丙五金店" not in by, "4) 现金付款没有银行账户，不混进来")

        # 5) 没填账号的不进账户库
        await submit("payment_public", "丁包装", None, None)
        by2 = {x["payee"]: x for x in (await c.get("/api/oa/payee-accounts", headers=Hs)).json()}
        chk("丁包装" not in by2, "5) 没填账号的单据不进账户库（空账号是噪音）")

        # 谁都能读（不是只有财务）——提申请的是各部门的人
        chk((await c.get("/api/oa/payee-accounts", headers=H)).status_code == 200, "管理员也读得到")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
