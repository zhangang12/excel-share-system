"""销售台账修复验证：
#1 发货前付批注 → 发货款应收清零(后端);预付批注不动金额(对照)
#2 销售员(非主管)也返回合计 totals
#3 电工采购单重复 → dedupe_elec_po_sheets 去重 + 幂等
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="salesfix")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select, func
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns, dedupe_elec_po_sheets
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
            "username": "sf_sales", "password": "pass123", "role_ids": [rid["sales"]]})).json()["id"]
        await c.post("/api/projects", headers=H, json={"code": "SF-1", "name": "修复测试项目"})
        async with SessionLocal() as db:
            pid = (await db.execute(select(models.Project.id).where(models.Project.code == "SF-1"))).scalar_one()
            db.add(models.SalesLedger(project_id=pid, sales_uid=suid, contract="有",
                                      amount=100000, before_ship=60000, ship_receivable=60000))
            await db.commit()
            lid = (await db.execute(select(models.SalesLedger.id).where(models.SalesLedger.project_id == pid))).scalar_one()

        Hs = await login("sf_sales", "pass123")

        # #2 销售员能看到合计
        j = (await c.get("/api/sales/ledger", headers=Hs)).json()
        chk(j.get("totals") is not None, f"#2 销售员应有合计: {j.get('totals')}")
        chk(j["totals"]["count"] == 1 and j["totals"]["amount"] == 100000, f"#2 合计数值: {j.get('totals')}")

        # #1 发货前付批注 → 发货款应收清零
        r = await c.put(f"/api/sales/ledger/{lid}/payment-note", headers=Hs,
                        json={"field": "before_ship", "note": "【6-18】发货前货款收讫"})
        chk(r.status_code == 200, f"#1 批注保存: {r.text[:100]}")
        async with SessionLocal() as db:
            led = (await db.execute(select(models.SalesLedger).where(models.SalesLedger.id == lid))).scalar_one()
            chk(led.ship_receivable == 0, f"#1 发货前付批注后应收清零: {led.ship_receivable}")

        # #1 对照：预付批注不动应收
        async with SessionLocal() as db:
            led = (await db.execute(select(models.SalesLedger).where(models.SalesLedger.id == lid))).scalar_one()
            led.ship_receivable = 50000
            await db.commit()
        await c.put(f"/api/sales/ledger/{lid}/payment-note", headers=Hs, json={"field": "prepay", "note": "预付收讫"})
        async with SessionLocal() as db:
            led = (await db.execute(select(models.SalesLedger).where(models.SalesLedger.id == lid))).scalar_one()
            chk(led.ship_receivable == 50000, f"#1 预付批注不应动应收(对照): {led.ship_receivable}")

        # #B 删除发货前付批注 → 应收恢复 = 发货前付(60000)
        await c.put(f"/api/sales/ledger/{lid}/payment-note", headers=Hs, json={"field": "before_ship", "note": "收讫"})
        await c.put(f"/api/sales/ledger/{lid}/payment-note", headers=Hs, json={"field": "before_ship", "note": ""})
        async with SessionLocal() as db:
            led = (await db.execute(select(models.SalesLedger).where(models.SalesLedger.id == lid))).scalar_one()
            chk(led.ship_receivable == 60000 and led.before_ship_note is None,
                f"#B 删批注→应收恢复为发货前付: ship_receivable={led.ship_receivable} note={led.before_ship_note}")

        # #A next-code 指定年度
        nc = (await c.get("/api/sales/next-code?year=2027", headers=Hs)).json()
        chk(nc["code"].startswith("2027-"), f"#A 指定年度生成编号: {nc}")
        nc0 = (await c.get("/api/sales/next-code", headers=Hs)).json()
        chk(not nc0["code"].startswith("2027-"), f"#A 不传年度=当年(非2027): {nc0}")

        # #C 调货订单：不选任何生产部门 → 可下单，仅建发货待办 + 推送发货部，无部门任务
        logi_uid = (await c.post("/api/admin/users", headers=H, json={
            "username": "dh_logi", "password": "pass123", "role_ids": [rid["logistics"]]})).json()["id"]
        r = await c.post("/api/sales/orders", headers=H, json={
            "code": "DH-1", "name": "调货测试", "customer": "客户A", "cust_type": "经销商",
            "contract": "无", "amount": 0, "tax_rate": "/", "prepay": 0, "prepay_note": "",
            "before_ship": 0, "before_ship_note": "", "ship_receivable": 0, "balance": 0,
            "balance_date": "", "depts": [], "req_text": "", "receiver": {"name": "", "phone": "", "addr": ""}})
        chk(r.status_code == 200, f"#C 调货订单(空部门)可下单: {r.status_code} {r.text[:120]}")
        chk(r.json()["order_ids"] == [], f"#C 调货订单无部门任务: {r.json().get('order_ids')}")
        dh_pid = r.json()["project_id"]
        async with SessionLocal() as db:
            n_orders = (await db.execute(select(func.count()).select_from(models.DeptOrder)
                                         .where(models.DeptOrder.project_id == dh_pid))).scalar()
            n_ship = (await db.execute(select(func.count()).select_from(models.Shipment)
                                       .where(models.Shipment.project_id == dh_pid))).scalar()
            got_logi = (await db.execute(select(func.count()).select_from(models.Message).where(
                models.Message.to_user_id == logi_uid, models.Message.text.like("%发货待办%")))).scalar()
        chk(n_orders == 0, f"#C 调货无 DeptOrder: {n_orders}")
        chk(n_ship == 1, f"#C 调货有发货待办: {n_ship}")
        chk(got_logi >= 1, f"#C 发货部收到推送: {got_logi}")

    # #3 电工采购单去重
    async with SessionLocal() as db:
        pid = (await db.execute(select(models.Project.id).where(models.Project.code == "SF-1"))).scalar_one()
        before = (await db.execute(select(func.count()).select_from(models.Datasheet).where(
            models.Datasheet.project_id == pid, models.Datasheet.name == "电工采购单"))).scalar()
        chk(before == 1, f"#3 新建项目应有 1 张电工采购单: {before}")
        db.add(models.Datasheet(project_id=pid, name="电工采购单", sort_order=101))  # 制造重复
        await db.commit()
        res = await dedupe_elec_po_sheets(db)
        after = (await db.execute(select(func.count()).select_from(models.Datasheet).where(
            models.Datasheet.project_id == pid, models.Datasheet.name == "电工采购单"))).scalar()
        chk(after == 1, f"#3 去重后仅剩 1 张: {after} (removed={res})")
        res2 = await dedupe_elec_po_sheets(db)
        chk(res2["removed"] == 0, f"#3 幂等(再跑不删): {res2}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
