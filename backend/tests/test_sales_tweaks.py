"""🆕 销售台账三项口径(用户 2026-06-18)：
1. 发货前付插入批注后，发货款应收自动变 0
2. 尾款填 0 时尾款日期自动清空(显示横杠)
3. 税票「不开票」由 "/" 改为 "0"(历史 "/" 兼容，迁移统一为 "0")
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="saletweak")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns, normalize_tax_rate_no_invoice
from app import models

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'admin','password':'admin123'})).json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        await c.post("/api/admin/users", headers=H, json={"username":"s1","password":"pass123","full_name":"赵","role_id":rid["sales"]})
        await c.post("/api/admin/users", headers=H, json={"username":"sl","password":"pass123","full_name":"钱","role_id":rid["sales_lead"]})
        async def login(u,p="pass123"):
            return {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':u,'password':p})).json()['access_token']}"}
        Hs1, Hsl = await login("s1"), await login("sl")

        async def order(**kw):
            body = {"name": "机", "customer": "客户甲", "cust_type": "经销商", "contract": "有",
                    "amount": 100000, "tax_rate": "13%", "prepay": 0, "before_ship": 0,
                    "ship_receivable": 5000, "balance": 0, "balance_date": "",
                    "depts": ["design"], "receiver": {"name":"a","phone":"1","addr":"b"}}
            body.update(kw)
            r = await c.post("/api/sales/orders", headers=Hs1, json=body)
            assert r.status_code == 200, r.text
            return r.json()["project_id"]
        async def row_of(pid):
            rows = (await c.get("/api/sales/ledger", headers=Hsl)).json()["rows"]
            return next(x for x in rows if x["project_id"] == pid)

        # ===== Req1: 发货前付插入批注 → 发货款应收=0 =====
        pid = await order(ship_receivable=5000)
        lid = (await row_of(pid))["id"]
        r = await c.put(f"/api/sales/ledger/{lid}/payment-note", headers=Hs1,
                        json={"field": "before_ship", "note": "2026-06-18 客户已付发货前款"})
        chk(r.status_code == 200, f"发货前付批注: {r.text[:80]}")
        row = await row_of(pid)
        chk(row["ship_receivable"] == 0, f"发货前付批注后发货款应收清零: {row['ship_receivable']}")
        chk(row["before_ship_note"], "批注已保存")
        # 预付批注不应影响发货款应收
        pid2 = await order(ship_receivable=8000)
        lid2 = (await row_of(pid2))["id"]
        await c.put(f"/api/sales/ledger/{lid2}/payment-note", headers=Hs1, json={"field": "prepay", "note": "预付批注"})
        chk((await row_of(pid2))["ship_receivable"] == 8000, "预付批注不影响发货款应收")

        # ===== Req2: 尾款=0 → 尾款日期清空 =====
        # 下单时 balance=0 但传了日期 → 落库为空
        pidA = await order(balance=0, balance_date="2026-08-01")
        chk((await row_of(pidA))["balance_date"] in (None, ""), "下单尾款0→日期清空")
        # balance>0 + 日期 → 保留
        pidB = await order(balance=5000, balance_date="2026-08-01")
        chk((await row_of(pidB))["balance_date"] == "2026-08-01", "尾款>0→日期保留")
        # 编辑把尾款改成0 → 日期被清
        lidB = (await row_of(pidB))["id"]
        r = await c.put(f"/api/sales/ledger/{lidB}", headers=Hsl, json={"balance": 0})
        chk(r.status_code == 200, f"编辑尾款=0: {r.text[:80]}")
        chk((await row_of(pidB))["balance_date"] in (None, ""), "编辑尾款改0→日期清空")

        # ===== Req3: 税票 "0" = 不开票，开票申请被拦；"/" 兼容；迁移 / → 0 =====
        pidC = await order(tax_rate="0")
        lidC = (await row_of(pidC))["id"]
        r = await c.post(f"/api/sales/ledger/{lidC}/invoice-apply", headers=Hs1,
                         files={"file": ("a.pdf", b"x", "application/pdf")})
        chk(r.status_code == 400 and "不开票" in r.text, f"税票0不可开票申请: {r.status_code} {r.text[:80]}")
        # 历史 "/" 兼容：直接写库一条 "/"，迁移后变 "0"
        async with SessionLocal() as db:
            led = (await db.execute(select(models.SalesLedger).where(models.SalesLedger.id == lidC))).scalar_one()
            led.tax_rate = "/"
            await db.commit()
        # "/" 仍被判不开票
        r = await c.post(f"/api/sales/ledger/{lidC}/invoice-apply", headers=Hs1,
                         files={"file": ("a.pdf", b"x", "application/pdf")})
        chk(r.status_code == 400 and "不开票" in r.text, f'历史"/"仍判不开票: {r.status_code}')
        # 迁移幂等：/ → 0
        async with SessionLocal() as db:
            res = await normalize_tax_rate_no_invoice(db)
        chk(res["updated"] >= 1, f"迁移 / → 0: {res}")
        async with SessionLocal() as db:
            led = (await db.execute(select(models.SalesLedger).where(models.SalesLedger.id == lidC))).scalar_one()
            chk(led.tax_rate == "0", f'迁移后税票=0: {led.tax_rate}')
        async with SessionLocal() as db:
            res2 = await normalize_tax_rate_no_invoice(db)
        chk(res2["updated"] == 0, f"迁移幂等(0): {res2}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
