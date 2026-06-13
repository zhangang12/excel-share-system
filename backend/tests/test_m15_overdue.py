"""M15 逾期每日提醒：扫描逾期任务推部门主管+管理层，同日幂等不重复。"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="m15test")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app.overdue import scan_overdue

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
        r = await c.post("/api/auth/login", json={"username":"admin","password":"admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        async def mk(u, rc, fn):
            r = await c.post("/api/admin/users", headers=H, json={"username":u,"password":"pass123","full_name":fn,"role_id":rid[rc]})
            return r.json()["id"]
        ids = {}
        for u, rc, fn in [("s1","sales","赵仁辉"),("dl","design_lead","陈工"),("d1","designer","张工"),("lo","logistics","马师傅")]:
            ids[u] = await mk(u, rc, fn)
        async def login(u):
            r = await c.post("/api/auth/login", json={"username":u,"password":"pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}
        Hdl, Hd1 = await login("dl"), await login("d1")

        # 销售下单 → 设计接单，预计完成设为过去日期（逾期）
        r = await c.post("/api/sales/orders", headers=await login("s1"), json={
            "name":"乳化机","customer":"x","cust_type":"经销商","contract":"有","amount":100000,
            "tax_rate":"13%","prepay":0,"before_ship":0,"ship_receivable":0,"balance":0,
            "balance_date":"","depts":["design"],"receiver":{"name":"a","phone":"1","addr":"b"}})
        pid = r.json()["project_id"]
        od = [o for o in (await c.get("/api/orders?dept=design", headers=Hdl)).json() if o["project_id"]==pid][0]["id"]
        await c.post(f"/api/orders/{od}/assign", headers=Hdl, json={"worker_id":ids["d1"]})
        await c.post(f"/api/orders/{od}/start", headers=Hd1, json={"start_date":"2020-01-01","due_date":"2020-01-10"})

        # 另一个未逾期任务（due 未来）不应被提醒
        r = await c.post("/api/sales/orders", headers=await login("s1"), json={
            "name":"机B","customer":"x","cust_type":"经销商","contract":"有","amount":100000,
            "tax_rate":"13%","prepay":0,"before_ship":0,"ship_receivable":0,"balance":0,
            "balance_date":"","depts":["design"],"receiver":{"name":"a","phone":"1","addr":"b"}})
        pid2 = r.json()["project_id"]
        od2 = [o for o in (await c.get("/api/orders?dept=design", headers=Hdl)).json() if o["project_id"]==pid2][0]["id"]
        await c.post(f"/api/orders/{od2}/assign", headers=Hdl, json={"worker_id":ids["d1"]})
        await c.post(f"/api/orders/{od2}/start", headers=Hd1, json={"start_date":"2026-01-01","due_date":"2099-12-31"})

        # 第一次扫描：1 个逾期被提醒
        async with SessionLocal() as db:
            r1 = await scan_overdue(db)
        chk(r1["notified"] == 1, f"首次扫描提醒1个逾期: {r1}")
        # 设计主管 + 管理层(manager) 收到
        msgs = (await c.get("/api/messages", headers=Hdl)).json()
        chk(any("逾期提醒" in m["text"] for m in msgs), "设计主管收逾期提醒")
        rmg = await c.post("/api/auth/login", json={"username":"manager","password":"manager123"})
        Hmg = {"Authorization": f"Bearer {rmg.json()['access_token']}"}
        chk(any("逾期提醒" in m["text"] for m in (await c.get("/api/messages", headers=Hmg)).json()), "管理层收逾期抄送")

        # 同日再扫描：幂等不重复
        async with SessionLocal() as db:
            r2 = await scan_overdue(db)
        chk(r2["notified"] == 0, f"同日再扫描幂等(0): {r2}")
        cnt = len([m for m in (await c.get("/api/messages?limit=100", headers=Hdl)).json() if "逾期提醒" in m["text"]])
        chk(cnt == 1, f"设计主管仅1条逾期(不重复): {cnt}")

        # 内部端点（管理层）
        r = await c.post("/api/internal/overdue-scan", headers=H)
        chk(r.status_code == 200 and r.json()["notified"] == 0, "内部端点可调且幂等")
        # 非管理层不能调
        r = await c.post("/api/internal/overdue-scan", headers=Hd1)
        chk(r.status_code == 403, "非管理层不能触发扫描")

        # 完成逾期任务后不再提醒（次日模拟：清当日消息后完成）
        # 这里验证完成后状态!=in_progress 即不在扫描范围
        from sqlalchemy import update as _upd
        from app import models as M
        from datetime import datetime, timezone
        async with SessionLocal() as db:
            await db.execute(_upd(M.Datasheet).where(M.Datasheet.project_id==pid,
                M.Datasheet.name.in_(['钣金装配','标准件清单','外协外购','原料下料单']))
                .values(imported_at=datetime.now(timezone.utc)))
            await db.commit()
        await c.post(f"/api/orders/{od}/complete", headers=Hd1, json={"notify_user_id":ids["lo"]})
        # 删掉今天的提醒消息模拟次日
        async with SessionLocal() as db:
            from sqlalchemy import delete as _del
            await db.execute(_del(M.Message).where(M.Message.biz_type=="order_overdue"))
            await db.commit()
            r3 = await scan_overdue(db)
        chk(r3["notified"] == 0, f"完成后不再提醒: {r3}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
