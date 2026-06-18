"""M14 报表：月度/部门/销售；重点验证效率口径 C2(done==due按时)/C3(预计0天按1天) + 权限。"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="m14test")
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
        for u, rc, fn in [("s1","sales","赵仁辉"),("sl","sales_lead","高总"),("dl","design_lead","陈工"),
                          ("d1","designer","张工"),("lo","logistics","马师傅")]:
            ids[u] = await mk(u, rc, fn)
        async def login(u):
            r = await c.post("/api/auth/login", json={"username":u,"password":"pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}
        Hsl, Hdl, Hd1 = await login("sl"), await login("dl"), await login("d1")

        # 销售下单建项目 + 设计任务
        async def place(name, amount):
            r = await c.post("/api/sales/orders", headers=await login("s1"), json={
                "name":name,"customer":"x","cust_type":"经销商","contract":"有","amount":amount,
                "tax_rate":"13%","prepay":1000,"before_ship":0,"ship_receivable":0,"balance":500,
                "balance_date":"","depts":["design"],"receiver":{"name":"a","phone":"1","addr":"b"}})
            return r.json()["project_id"]
        p1 = await place("机A", 100000)
        p2 = await place("机B", 200000)

        from sqlalchemy import update as _upd
        from app import models as M
        from datetime import datetime, timezone
        # 设计任务：p1 当天完成(按时,C2)；p2 预计0天(start==due)当天完成(C3 效率=100)
        async def design_done(pid, start, due, done):
            od = [o for o in (await c.get("/api/orders?dept=design", headers=Hdl)).json() if o["project_id"]==pid][0]["id"]
            await c.post(f"/api/orders/{od}/assign", headers=Hdl, json={"worker_id":ids["d1"]})
            await c.post(f"/api/orders/{od}/start", headers=Hd1, json={"start_date":start,"due_date":due})
            async with SessionLocal() as db:
                await db.execute(_upd(M.Datasheet).where(M.Datasheet.project_id==pid,
                    M.Datasheet.name.in_(['钣金装配','标准件清单','外协加工','不锈钢原料下料单']))
                    .values(imported_at=datetime.now(timezone.utc)))
                await db.commit()
            # 直接置 done_date 模拟指定完成日
            r = await c.post(f"/api/orders/{od}/complete", headers=Hd1, json={"notify_user_id":ids["lo"]})
            async with SessionLocal() as db:
                await db.execute(_upd(M.DeptOrder).where(M.DeptOrder.id==od).values(done_date=done))
                await db.commit()
            return od

        await design_done(p1, "2026-06-01", "2026-06-10", "2026-06-10")  # 按时, eff=round(9/9*100)=100
        await design_done(p2, "2026-06-01", "2026-06-01", "2026-06-01")  # 预计0天→按1天, eff=round(0/1)=0→实际0天 round(0/1*100)=0

        # ===== 部门报表 =====
        r = await c.get("/api/reports/dept/design", headers=Hdl)
        chk(r.status_code==200, f"设计负责人看部门报表: {r.status_code}")
        rep = r.json()
        chk(rep["done"]==2 and rep["ontime_rate"]==100, f"两单都按时(C2 done==due): rate={rep['ontime_rate']}")
        # 销售主管不能看设计部报表
        r = await c.get("/api/reports/dept/design", headers=Hsl)
        chk(r.status_code==403, "销售主管不能看设计部报表")

        # ===== 月度报表（仅管理层）=====
        r = await c.get("/api/reports/monthly", headers=H)
        chk(r.status_code==200, "管理层看月度报表")
        mr = r.json()
        chk(mr["sales_order_count"]==2, f"当月销售下单数: {mr['sales_order_count']}")
        chk(mr["done"]==2 and mr["overdue"]==0, "月度完成2逾期0")
        design_card = [d for d in mr["dept_cards"] if d["dept"]=="design"][0]
        chk(design_card["done"]==2, "月度设计部完成2")
        # 部门主管/销售主管不能看月度报表(§十二.17)
        r = await c.get("/api/reports/monthly", headers=Hdl)
        chk(r.status_code==403, "设计负责人不能看月度报表")
        r = await c.get("/api/reports/monthly", headers=Hsl)
        chk(r.status_code==403, "销售主管不能看月度报表")

        # ===== 销售报表 =====
        r = await c.get("/api/reports/sales", headers=Hsl)
        chk(r.status_code==200, "销售主管看销售报表")
        sr = r.json()
        chk(sr["project_count"]==2 and sr["total_amount"]==300000, f"销售报表总额: {sr['total_amount']}")
        chk(sr["contract_count"]==2 and sr["contract_rate"]==100, "合同覆盖率100%")
        chk(sr["uninvoiced_amount"]==300000, "未开票=全部(未开票)")
        chk(len(sr["by_salesperson"])==1 and sr["by_salesperson"][0]["count"]==2, "按销售员1人2单")
        chk(sr["receivables"]["prepay"]==2000, f"预付合计: {sr['receivables']['prepay']}")
        # 设计负责人不能看销售报表
        r = await c.get("/api/reports/sales", headers=Hdl)
        chk(r.status_code==403, "设计负责人不能看销售报表")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
