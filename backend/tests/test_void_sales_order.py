"""🆕 销售订单作废：销售员申请 → 销售负责人审批 → 软删项目 + 各部门流程作废。

口径(用户 2026-06-18)：销售员申请、负责人审批(负责人可一键直接作废)；
已开票/已发货禁止作废；软删可追溯；必填原因；通过后仅通知管理层。

覆盖：必填原因 / 只能作废自己的 / 申请→主管审批通过→项目软删+详单清空+部门单作废+台账/工作台消失 /
      负责人一键直接作废 / 驳回回退 / 已开票禁止 / 已发货禁止。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="voidord")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select, func, update as _upd
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

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'admin','password':'admin123'})).json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mk(u, rc):
            await c.post("/api/admin/users", headers=H, json={"username": u, "password": "pass123", "full_name": u, "role_id": rid[rc]})
        async def login(u, p="pass123"):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        for u, rc in [("s1", "sales"), ("s2", "sales"), ("sl", "sales_lead")]:
            await mk(u, rc)
        Hs1, Hs2, Hsl = await login("s1"), await login("s2"), await login("sl")

        async def order(name):
            r = await c.post("/api/sales/orders", headers=Hs1, json={
                "name": name, "customer": "客户甲", "cust_type": "经销商", "contract": "有",
                "amount": 50000, "tax_rate": "13%", "prepay": 0, "before_ship": 0,
                "ship_receivable": 0, "balance": 0, "balance_date": "",
                "depts": ["design"], "receiver": {"name": "a", "phone": "1", "addr": "b"}})
            assert r.status_code == 200, r.text
            return r.json()["project_id"]
        async def lid_of(pid):
            rows = (await c.get("/api/sales/ledger", headers=Hs1)).json()["rows"]
            return next(x["id"] for x in rows if x["project_id"] == pid)

        # ===== A: 申请→审批通过→级联软删 =====
        pidA = await order("机A")
        lidA = await lid_of(pidA)
        # 必填原因
        r = await c.post(f"/api/sales/ledger/{lidA}/void-apply", headers=Hs1, json={"reason": "  "})
        chk(r.status_code == 422 or r.status_code == 400, f"空原因应拒绝: {r.status_code}")
        # 只能作废自己的：s2 作废 s1 的单 → 403
        r = await c.post(f"/api/sales/ledger/{lidA}/void-apply", headers=Hs2, json={"reason": "客户取消"})
        chk(r.status_code == 403, f"非本人作废应403: {r.status_code} {r.text[:80]}")
        # s1 申请 → applying
        r = await c.post(f"/api/sales/ledger/{lidA}/void-apply", headers=Hs1, json={"reason": "客户取消订单"})
        chk(r.status_code == 200, f"s1 申请作废: {r.text[:100]}")
        # 主管审批列表含该单
        appr = (await c.get("/api/sales/void-approvals", headers=Hsl)).json()["rows"]
        chk(any(x["id"] == lidA and x["void_reason"] == "客户取消订单" for x in appr), "主管作废审批列表含该单+原因")
        # 设计部任务单此时仍在(admin 视角)
        before = [o for o in (await c.get("/api/orders?dept=design", headers=H)).json() if o["project_id"] == pidA]
        chk(len(before) >= 1, "审批前设计任务单存在")
        # 主管通过 → 级联软删
        r = await c.post(f"/api/sales/ledger/{lidA}/void-approve", headers=Hsl)
        chk(r.status_code == 200, f"主管审批通过: {r.text[:100]}")

        async with SessionLocal() as db:
            p = (await db.execute(select(models.Project).where(models.Project.id == pidA))).scalar_one()
            chk(p.is_deleted is True, "项目已软删 is_deleted")
            chk(p.code.startswith("_deleted_"), f"项目 code 加墓碑前缀: {p.code}")
            led = (await db.execute(select(models.SalesLedger).where(models.SalesLedger.id == lidA))).scalar_one()
            chk(led.void_state == "voided", f"台账 void_state=voided: {led.void_state}")
            ds = (await db.execute(select(func.count()).select_from(models.Datasheet).where(models.Datasheet.project_id == pidA))).scalar()
            chk(ds == 0, f"项目详单已清空: {ds}")
            dord = (await db.execute(select(models.DeptOrder).where(models.DeptOrder.project_id == pidA))).scalars().all()
            chk(dord and all(o.status == "voided" for o in dord), f"各部门任务单已作废: {[o.status for o in dord]}")
        # 台账不再显示 / 设计工作台不再显示
        chk(not any(x["project_id"] == pidA for x in (await c.get("/api/sales/ledger", headers=Hs1)).json()["rows"]), "台账不再显示已作废项目")
        chk(not any(o["project_id"] == pidA for o in (await c.get("/api/orders?dept=design", headers=H)).json()), "设计工作台不再显示已作废项目流程")
        # 管理层收到通知
        Hmg = await login("manager", "manager123")
        chk(any("订单已作废" in m["text"] and "客户取消订单" in m["text"] for m in (await c.get("/api/messages?limit=100", headers=Hmg)).json()), "管理层收到作废通知含原因")

        # ===== B: 已开票禁止作废 =====
        pidB = await order("机B"); lidB = await lid_of(pidB)
        async with SessionLocal() as db:
            await db.execute(_upd(models.SalesLedger).where(models.SalesLedger.id == lidB).values(invoice_state="invoiced"))
            await db.commit()
        r = await c.post(f"/api/sales/ledger/{lidB}/void-apply", headers=Hs1, json={"reason": "x"})
        chk(r.status_code == 400 and "已开票" in r.text, f"已开票禁止作废: {r.status_code} {r.text[:80]}")

        # ===== C: 已发货禁止作废 =====
        pidC = await order("机C"); lidC = await lid_of(pidC)
        async with SessionLocal() as db:
            await db.execute(_upd(models.Shipment).where(models.Shipment.project_id == pidC).values(status="shipped"))
            await db.commit()
        r = await c.post(f"/api/sales/ledger/{lidC}/void-apply", headers=Hs1, json={"reason": "x"})
        chk(r.status_code == 400 and "已发货" in r.text, f"已发货禁止作废: {r.status_code} {r.text[:80]}")

        # ===== D: 驳回回退 =====
        pidD = await order("机D"); lidD = await lid_of(pidD)
        await c.post(f"/api/sales/ledger/{lidD}/void-apply", headers=Hs1, json={"reason": "改主意了"})
        r = await c.post(f"/api/sales/ledger/{lidD}/void-reject", headers=Hsl)
        chk(r.status_code == 200, f"主管驳回: {r.text[:80]}")
        async with SessionLocal() as db:
            led = (await db.execute(select(models.SalesLedger).where(models.SalesLedger.id == lidD))).scalar_one()
            chk(led.void_state is None and led.void_reason is None, "驳回后作废态清空")
            p = (await db.execute(select(models.Project).where(models.Project.id == pidD))).scalar_one()
            chk(p.is_deleted is False, "驳回后项目未删除")
        chk(any(x["project_id"] == pidD for x in (await c.get("/api/sales/ledger", headers=Hs1)).json()["rows"]), "驳回后订单仍在台账")

        # ===== E: 销售负责人一键直接作废 =====
        pidE = await order("机E"); lidE = await lid_of(pidE)
        r = await c.post(f"/api/sales/ledger/{lidE}/void-apply", headers=Hsl, json={"reason": "负责人直接作废"})
        chk(r.status_code == 200 and "已作废" in r.text, f"负责人一键直接作废: {r.text[:100]}")
        async with SessionLocal() as db:
            p = (await db.execute(select(models.Project).where(models.Project.id == pidE))).scalar_one()
            chk(p.is_deleted is True, "负责人直接作废→项目软删")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
