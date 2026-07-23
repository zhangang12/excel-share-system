"""🆕 反馈#284 回归：尾款「录入到款时间」后金额被清零、尾款日期变「-」

1. 尾款到账批注(插入到款时间, payment-note field=balance)：只写批注，
   尾款金额(balance)与尾款日期(balance_date)必须原样保留；删批注同样不动金额/日期。
2. 台账更新端点只带 balance_date(漏带金额)：金额必须保留（None 字段不覆盖）。
3. 草稿重提(draft-resubmit) payload 漏带金额/批注字段（旧客户端/部分表单）：
   schema 默认值会以 0/"" 到达端点——必须不覆盖台账已有 尾款/金额/批注/尾款日期；
   显式传值时仍正常更新（全表单行为不变）。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="balnote")
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


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)
    async with SessionLocal() as db:
        rid = {r.code: r.id for r in (await db.execute(select(models.Role))).scalars().all()}

    tr = ASGITransport(app=app)
    async with AsyncClient(transport=tr, base_url="http://test") as c:
        async def login(u, p):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")
        suid = (await c.post("/api/admin/users", headers=H, json={
            "username": "bn_sales", "password": "pass123", "role_ids": [rid["sales"]]})).json()["id"]
        await c.post("/api/projects", headers=H, json={"code": "BN-1", "name": "尾款批注项目"})
        async with SessionLocal() as db:
            pid = (await db.execute(select(models.Project.id).where(models.Project.code == "BN-1"))).scalar_one()
            db.add(models.SalesLedger(project_id=pid, sales_uid=suid, contract="有",
                                      amount=51000, prepay=20000, prepay_note="预付已收",
                                      balance=31000, balance_date="2026-08-15"))
            await db.commit()
            lid = (await db.execute(select(models.SalesLedger.id).where(models.SalesLedger.project_id == pid))).scalar_one()

        Hs = await login("bn_sales", "pass123")

        async def row():
            j = (await c.get("/api/sales/ledger", headers=Hs, params={"kw": "BN-1"})).json()
            return j["rows"][0]

        # ===== 1. 尾款插入到款时间(批注) → 金额/日期不动 =====
        r0 = await row()
        chk(r0["balance"] == 31000 and r0["balance_date"] == "2026-08-15",
            f"前置: 尾款31000/日期2026-08-15: balance={r0['balance']} date={r0['balance_date']!r}")
        r = await c.put(f"/api/sales/ledger/{lid}/payment-note", headers=Hs,
                        json={"field": "balance", "note": "【2026-07-23 10:00】尾款到账"})
        chk(r.status_code == 200, f"尾款批注保存: {r.status_code} {r.text[:120]}")
        r1 = await row()
        chk(r1["balance_note"] == "【2026-07-23 10:00】尾款到账", f"批注已落库: {r1['balance_note']!r}")
        chk(r1["balance"] == 31000, f"#284 批注后尾款金额不变: {r1['balance']}")
        chk(r1["balance_date"] == "2026-08-15", f"#284 批注后尾款日期不变: {r1['balance_date']!r}")

        # 删掉批注 → 金额/日期仍不动
        r = await c.put(f"/api/sales/ledger/{lid}/payment-note", headers=Hs,
                        json={"field": "balance", "note": ""})
        chk(r.status_code == 200, f"删批注: {r.status_code}")
        r1 = await row()
        chk(r1["balance_note"] is None and r1["balance"] == 31000 and r1["balance_date"] == "2026-08-15",
            f"删批注后金额/日期不动: balance={r1['balance']} date={r1['balance_date']!r} note={r1['balance_note']!r}")

        # ===== 2. 更新端点只带 balance_date(漏带金额) → 金额保留 =====
        r = await c.put(f"/api/sales/ledger/{lid}", headers=Hs, json={"balance_date": "2026-07-23"})
        chk(r.status_code == 200, f"只改尾款日期: {r.status_code} {r.text[:120]}")
        r2 = await row()
        chk(r2["balance"] == 31000 and r2["balance_date"] == "2026-07-23",
            f"漏带金额的更新不清尾款: balance={r2['balance']} date={r2['balance_date']!r}")

        # ===== 3. 草稿重提漏带金额字段 → 不清尾款/批注(#284 修复点) =====
        async with SessionLocal() as db:
            led = (await db.execute(select(models.SalesLedger).where(models.SalesLedger.id == lid))).scalar_one()
            led.order_state = "draft"
            await db.commit()
        # 部分表单(模拟旧客户端)：只传基础字段，金额/批注/尾款日期全部漏带
        r = await c.put(f"/api/sales/orders/{lid}/draft-resubmit", headers=Hs, json={
            "code": "BN-1", "name": "尾款批注项目", "qty": 1, "unit": "台",
            "customer": "某客户", "cust_type": "终端客户", "contract": "有",
            "depts": [], "req_text": "重提",
            "receiver": {"name": "x", "company": "", "phone": "1", "addr": "y"},
        })
        chk(r.status_code == 200, f"草稿重提(漏带金额): {r.status_code} {r.text[:150]}")
        r3 = await row()
        chk(r3["balance"] == 31000, f"#284 重提漏带balance→尾款不被清零: {r3['balance']}")
        chk(r3["balance_date"] == "2026-07-23", f"#284 重提漏带balance_date→日期保留: {r3['balance_date']!r}")
        chk(r3["prepay"] == 20000 and r3["prepay_note"] == "预付已收",
            f"#284 重提漏带prepay/note→保留: prepay={r3['prepay']} note={r3['prepay_note']!r}")

        # 显式传值 → 仍正常更新（全表单行为不变）
        async with SessionLocal() as db:
            led = (await db.execute(select(models.SalesLedger).where(models.SalesLedger.id == lid))).scalar_one()
            led.order_state = "draft"
            await db.commit()
        r = await c.put(f"/api/sales/orders/{lid}/draft-resubmit", headers=Hs, json={
            "code": "BN-1", "name": "尾款批注项目", "qty": 1, "unit": "台",
            "customer": "某客户", "cust_type": "终端客户", "contract": "有",
            "amount": 51000, "prepay": 20000, "before_ship": 0, "ship_receivable": 0,
            "balance": 5000, "balance_date": "2026-09-01",
            "depts": [], "req_text": "重提2",
            "receiver": {"name": "x", "company": "", "phone": "1", "addr": "y"},
        })
        chk(r.status_code == 200, f"草稿重提(显式金额): {r.status_code} {r.text[:150]}")
        r4 = await row()
        chk(r4["balance"] == 5000 and r4["balance_date"] == "2026-09-01",
            f"显式传值仍正常更新: balance={r4['balance']} date={r4['balance_date']!r}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
