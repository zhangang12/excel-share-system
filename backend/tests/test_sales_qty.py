"""🆕 销售下单填数量+单位(台/套)，同步到项目一览「数量」单元格(__o__数量)。"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="salesqty")
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
from app.routers.projects_router import OVERVIEW_KEY_PREFIX

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
        await c.post("/api/admin/users", headers=H, json={"username":"sl","password":"pass123","full_name":"钱","role_id":rid["sales_lead"]})
        Hsl = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'sl','password':'pass123'})).json()['access_token']}"}

        async def order(**kw):
            body = {"name":"乳化机","customer":"客户甲","cust_type":"经销商","contract":"有",
                    "amount":1000,"tax_rate":"13%","depts":["design"],
                    "receiver":{"name":"a","phone":"1","addr":"b"}}
            body.update(kw)
            r = await c.post("/api/sales/orders", headers=Hsl, json=body)
            assert r.status_code == 200, r.text
            return r.json()["project_id"]
        async def row(pid):
            rows = (await c.get("/api/sales/ledger", headers=Hsl)).json()["rows"]
            return next(x for x in rows if x["project_id"] == pid)
        async def ov_qty(pid):
            async with SessionLocal() as db:
                p = (await db.execute(select(models.Project).where(models.Project.id == pid))).scalar_one()
                return (p.extra or {}).get(f"{OVERVIEW_KEY_PREFIX}数量")

        # 指定数量+单位 → 一览数量单元格 "3套"
        pidA = await order(qty=3, unit="套")
        chk(await ov_qty(pidA) == "3套", f"一览数量单元格=3套: {await ov_qty(pidA)}")
        rA = await row(pidA)
        chk(rA["qty"] == 3 and rA["unit"] == "套", f"台账行回带 数量/单位: {rA['qty']}/{rA['unit']}")

        # 缺省 → 1台
        pidB = await order(name="搅拌机")
        chk(await ov_qty(pidB) == "1台", f"缺省数量=1台: {await ov_qty(pidB)}")

        # 非法单位回退台；数量<1 回退1
        pidC = await order(qty=0, unit="个")
        chk(await ov_qty(pidC) == "1台", f"非法单位/0数量回退1台: {await ov_qty(pidC)}")

        # 一览接口也能取到该单元格
        ov = (await c.get("/api/overview", headers=H)).json()
        chk(any("数量" == f["name"] for f in ov["fields"]), "一览含「数量」列")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
