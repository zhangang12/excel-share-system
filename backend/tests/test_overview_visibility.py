"""🆕 项目目录(一览 /api/overview)行级可见性:
纯设计师/电工/装配/销售只看自己经手的项目;部门负责人/管理层看全部。
(修复:此前 /api/overview 仅按项目成员可见,而存量把所有人加为所有项目成员→人人看全部。)
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="ovviz")
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
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'admin','password':'admin123'})).json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mk(u, rc):
            await c.post("/api/admin/users", headers=H, json={"username":u,"password":"pass123","full_name":u,"role_id":rid[rc]})
        async def login(u, p="pass123"):
            return {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':u,'password':p})).json()['access_token']}"}

        for u, rc in [("sl","sales_lead"), ("dl","design_lead"), ("d1","designer"), ("d2","designer")]:
            await mk(u, rc)
        Hsl, Hdl, Hd1, Hd2 = await login("sl"), await login("dl"), await login("d1"), await login("d2")

        async def order(name):
            r = await c.post("/api/sales/orders", headers=Hsl, json={
                "name": name, "customer": "x", "cust_type": "经销商", "contract": "有",
                "amount": 1000, "tax_rate": "13%", "depts": ["design"],
                "receiver": {"name":"a","phone":"1","addr":"b"}})
            assert r.status_code == 200, r.text
            return r.json()["project_id"]
        pidA = await order("机A")
        pidB = await order("机B")
        # 把 A 的设计单派给 d1，B 的派给 d2
        ods = {o["project_id"]: o["id"] for o in (await c.get("/api/orders?dept=design", headers=Hdl)).json()}
        d1id = (await c.get("/api/admin/users", headers=H)).json()
        d1id = next(u["id"] for u in d1id if u["username"] == "d1")
        d2id = next(u["id"] for u in (await c.get("/api/admin/users", headers=H)).json() if u["username"] == "d2")
        await c.post(f"/api/orders/{ods[pidA]}/assign", headers=Hdl, json={"worker_id": d1id})
        await c.post(f"/api/orders/{ods[pidB]}/assign", headers=Hdl, json={"worker_id": d2id})

        async def ov_pids(hdr):
            ov = (await c.get("/api/overview", headers=hdr)).json()
            return {row["id"] for row in ov["rows"]}

        chk(await ov_pids(Hd1) == {pidA}, f"纯设计师 d1 只看自己派的 A: {await ov_pids(Hd1)}")
        chk(await ov_pids(Hd2) == {pidB}, f"纯设计师 d2 只看自己派的 B: {await ov_pids(Hd2)}")
        chk({pidA, pidB} <= await ov_pids(Hdl), f"设计部负责人看到全部")
        Hmg = await login("manager", "manager123")
        chk({pidA, pidB} <= await ov_pids(Hmg), f"管理层看到全部")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
