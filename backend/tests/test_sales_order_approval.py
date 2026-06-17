"""🆕 销售下单审批(开关 SALES_ORDER_APPROVAL=true)：
仅销售员下单需销售主管审批，通过后才建各部门任务/发货单/成员；驳回=退回草稿可改；主管/管理层下单直接生效。
口径(用户 2026-06-18)：审批通过后才生效 / 驳回退回草稿可改 / 仅销售员下单需审批。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="ordappr")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.environ["SALES_ORDER_APPROVAL"] = "true"   # 开启审批开关
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select, func
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
        for u, rc in [("s1","sales"), ("sl","sales_lead"), ("dl","design_lead")]:
            await c.post("/api/admin/users", headers=H, json={"username":u,"password":"pass123","full_name":u,"role_id":rid[rc]})
        async def login(u,p="pass123"):
            return {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':u,'password':p})).json()['access_token']}"}
        Hs1, Hsl, Hdl = await login("s1"), await login("sl"), await login("dl")

        async def order(hdr, name="机", depts=None):
            r = await c.post("/api/sales/orders", headers=hdr, json={
                "name": name, "customer": "客户甲", "cust_type": "经销商", "contract": "有",
                "amount": 50000, "tax_rate": "13%", "prepay": 0, "before_ship": 0,
                "ship_receivable": 0, "balance": 0, "balance_date": "",
                "depts": depts or ["design"], "receiver": {"name":"a","phone":"1","addr":"b"}})
            assert r.status_code == 200, r.text
            return r.json()
        async def my_row(hdr, pid):
            rows = (await c.get("/api/sales/ledger", headers=hdr)).json()["rows"]
            return next((x for x in rows if x["project_id"] == pid), None)
        async def dept_orders(pid):
            async with SessionLocal() as db:
                return (await db.execute(select(func.count()).select_from(models.DeptOrder)
                        .where(models.DeptOrder.project_id == pid))).scalar()

        # ===== A: 销售员下单 → 待审批，无下游 =====
        res = await order(Hs1, "机A")
        pidA = res["project_id"]
        chk(res["order_ids"] == [], f"销售员下单不立即派单: {res['order_ids']}")
        rowA = await my_row(Hs1, pidA)
        chk(rowA and rowA["order_state"] == "pending", f"台账 order_state=pending: {rowA and rowA['order_state']}")
        chk(await dept_orders(pidA) == 0, "待审批时无部门任务单")
        # 待审批项目不进设计负责人的项目目录
        cat = (await c.get("/api/projects", headers=Hdl)).json()
        chk(not any(p["id"] == pidA for p in cat), "待审批项目对设计负责人不可见(目录)")
        # 但销售本人台账可见
        chk(rowA is not None, "销售本人台账可见待审批单")
        # 主管审批列表含该单
        appr = (await c.get("/api/sales/order-approvals", headers=Hsl)).json()["rows"]
        chk(any(x["project_id"] == pidA for x in appr), "主管下单审批列表含该单")

        # 主管通过 → 派单生效
        r = await c.post(f"/api/sales/ledger/{rowA['id']}/order-approve", headers=Hsl)
        chk(r.status_code == 200, f"主管通过下单: {r.text[:100]}")
        chk(await dept_orders(pidA) >= 1, "通过后创建部门任务单")
        chk((await my_row(Hs1, pidA))["order_state"] in (None, ""), "通过后 order_state 清空(已生效)")
        cat2 = (await c.get("/api/projects", headers=Hdl)).json()
        chk(any(p["id"] == pidA for p in cat2), "通过后项目进入设计负责人目录")
        # 设计工作台出现该项目任务
        chk(any(o["project_id"] == pidA for o in (await c.get("/api/orders?dept=design", headers=H)).json()), "通过后设计工作台可见任务")
        # 销售本人收到通过通知
        chk(any("下单已通过" in m["text"] for m in (await c.get("/api/messages?limit=100", headers=Hs1)).json()), "销售收到下单通过通知")

        # ===== B: 驳回 → 退回草稿，可改重提 =====
        resB = await order(Hs1, "机B")
        pidB = resB["project_id"]; lidB = (await my_row(Hs1, pidB))["id"]
        r = await c.post(f"/api/sales/ledger/{lidB}/order-reject", headers=Hsl, json={"reason": "金额不对"})
        chk(r.status_code == 200, f"主管驳回: {r.text[:80]}")
        rowB = await my_row(Hs1, pidB)
        chk(rowB["order_state"] == "draft", f"驳回→draft: {rowB['order_state']}")
        chk(rowB.get("order_reject_reason") == "金额不对", f"退回原因带出: {rowB.get('order_reject_reason')}")
        chk(any("下单退回" in m["text"] for m in (await c.get("/api/messages?limit=100", headers=Hs1)).json()), "销售收到退回通知")
        chk(await dept_orders(pidB) == 0, "草稿仍无部门任务单")
        # 修改重提
        r = await c.put(f"/api/sales/orders/{lidB}/draft-resubmit", headers=Hs1, json={
            "name": "机B改", "customer": "客户甲", "cust_type": "经销商", "contract": "有",
            "amount": 66000, "tax_rate": "13%", "prepay": 0, "before_ship": 0,
            "ship_receivable": 0, "balance": 0, "balance_date": "",
            "depts": ["design", "electric"], "receiver": {"name":"a","phone":"1","addr":"b"}})
        chk(r.status_code == 200, f"修改重提: {r.text[:100]}")
        rowB2 = await my_row(Hs1, pidB)
        chk(rowB2["order_state"] == "pending" and rowB2["amount"] == 66000, f"重提后 pending+改金额: {rowB2['order_state']}/{rowB2['amount']}")
        # 主管通过 → 按新 depts 派两个部门
        await c.post(f"/api/sales/ledger/{lidB}/order-approve", headers=Hsl)
        chk(await dept_orders(pidB) == 2, f"重提通过后按新派单(2部门): {await dept_orders(pidB)}")

        # ===== C: 放弃草稿 =====
        resC = await order(Hs1, "机C"); pidC = resC["project_id"]; lidC = (await my_row(Hs1, pidC))["id"]
        r = await c.post(f"/api/sales/ledger/{lidC}/order-discard", headers=Hs1)
        chk(r.status_code == 200, f"放弃下单: {r.text[:80]}")
        async with SessionLocal() as db:
            p = (await db.execute(select(models.Project).where(models.Project.id == pidC))).scalar_one()
            chk(p.is_deleted is True, "放弃→项目软删")

        # ===== D: 主管下单直接生效(免审批) =====
        resD = await order(Hsl, "机D")
        chk(resD["order_ids"], f"主管下单立即派单: {resD['order_ids']}")
        chk(await dept_orders(resD["project_id"]) >= 1, "主管下单立即有部门任务单")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
