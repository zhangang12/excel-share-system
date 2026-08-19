"""出库下拉必须标出「这是别人项目的料」（2026-08-19，用户确认口径时提出）。

背景：库存总览**不含**项目物料（#373/#374 口径：钱在收货时就算进那个项目了）；
出库下拉**含**——生产实测对项目物料的出库有 249 笔走的就是出库登记这条路（近一半），
去掉的话仓库明天就出不了这些货。既然保留，就必须让人一眼看出来这是谁的料。

原来只有"所有入库都指向同一个项目"的才反显【项目编号】，
生产上 399 个有货的项目料里 **135 个是多项目收过货的，一个标签都没有**，
跟公司备货长得一模一样。本文件锁住接口把**全部**项目编号都带出来。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="whtag")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns, ensure_indexes

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    await ensure_indexes(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        await c.post("/api/admin/users", headers=H, json={
            "username": "wh1", "password": "pass123", "full_name": "仓库王", "role_id": rid["warehouse"]})
        HW = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': 'wh1', 'password': 'pass123'})).json()['access_token']}"}

        pjs = {}
        for code in ("2026-001", "2026-002", "2026-003"):
            pjs[code] = (await c.post("/api/projects", headers=H, json={"code": code, "name": code})).json()

        mid = {}
        for name in ("只挂一个项目的料", "三个项目都收过的料", "纯通用料"):
            mid[name] = (await c.post("/api/wh/materials", headers=HW, json={
                "name": name, "unit": "个", "init_stock": 0})).json()["id"]

        async def recv(name, code, qty=5):
            return await c.post("/api/wh/txns", headers=HW, json={
                "material_id": mid[name], "direction": "in", "qty": qty, "unit_price": 10,
                "biz_date": "2026-08-19", "source": "采购收货", "project_id": pjs[code]["id"]})

        chk((await recv("只挂一个项目的料", "2026-001")).status_code == 200, "挂 001 入库")
        for code in ("2026-001", "2026-002", "2026-003"):
            chk((await recv("三个项目都收过的料", code)).status_code == 200, f"挂 {code} 入库")
        rr = await c.post("/api/wh/txns", headers=HW, json={
            "material_id": mid["纯通用料"], "direction": "in", "qty": 9, "unit_price": 3,
            "biz_date": "2026-08-19", "source": "采购收货"})
        chk(rr.status_code == 200, "无项目入库（通用料）")

        by = {m["name"]: m for m in (await c.get("/api/wh/materials", headers=H)).json()["materials"]}

        a = by["只挂一个项目的料"]
        chk(a["is_project_material"] is True, "单一项目的：是项目物料")
        chk(a["project_code"] == "2026-001", f"单一项目的：反显编号（出库时自动带项目）: {a['project_code']}")
        chk(a["project_codes"] == ["2026-001"], f"单一项目的：全部编号列表: {a['project_codes']}")

        b = by["三个项目都收过的料"]
        chk(b["is_project_material"] is True, "多项目的：是项目物料")
        chk(b["project_code"] is None,
            f"多项目的：**不**反显单一编号（猜一个填进去比不填更糟）: {b['project_code']}")
        chk(b["project_codes"] == ["2026-001", "2026-002", "2026-003"],
            f"多项目的：**全部编号都带出来**——前端据此标「项目料·3 个项目」，"
            f"不然它在下拉里跟公司备货长得一模一样: {b['project_codes']}")

        g = by["纯通用料"]
        chk(g["is_project_material"] is False, "通用料：不是项目物料")
        chk(g["project_codes"] == [], f"通用料：没有项目编号，下拉里不该有任何标: {g['project_codes']}")

        # 口径不能被这次改动带歪：库存总览(general)仍然只有通用料，出库下拉(all)三个都在
        gen = {m["name"] for m in (await c.get("/api/wh/materials", headers=H,
                                               params={"scope": "general"})).json()["materials"]}
        chk(gen == {"纯通用料"}, f"库存总览口径没变：只有通用料: {sorted(gen)}")
        allm = {m["name"] for m in (await c.get("/api/wh/materials", headers=H)).json()["materials"]}
        chk(len(allm) == 3, f"出库下拉口径没变：三个都在（项目料照样能出库）: {len(allm)}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
