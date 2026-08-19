"""反馈#404（王利利）：「收货完了，都不会及时更新，出库的时候啥也找不到，
都要从新点一下项目目录，在点出库，才有东西」。

根因不在后端，在前端**一个数组喂了三个地方**：
物料主数据的表格、库存总览的表格+KPI、出入库登记的物料下拉，全读同一个 `materials`。
而物料主数据的搜索是走服务端的（kw 传给后端），一搜就把这个共享数组换成命中的那几条——
出库下拉里当然找不到刚收的货；切到别的菜单再回来组件重挂载、kw 归空，就又好了
（这正是她说的"点项目目录再点出库"）。修法：拆成两个数组，全量的那份永不带筛选。

后端这边要锁住的是**这个拆分依赖的接口契约**：
  1. 不带任何参数时返回全量（前端全量那份就靠它）
  2. 带 kw/location/low_only 时确实会缩小——所以它**不能**用来喂出库下拉
  3. 刚收货生成的物料，立刻出现在不带参数的结果里（收货→出库不用等）
  4. scope 默认 all：项目物料也要能出库，不能因为挂了项目就从下拉里消失
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb404")
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

        for name, spec in [("深沟球轴承", "6205"), ("角接触轴承", "7205"), ("圆头螺丝", "M5*50"),
                           ("密封圈", "Φ30"), ("四氟垫片", "DN50")]:
            rr = await c.post("/api/wh/materials", headers=HW, json={
                "name": name, "spec": spec, "unit": "个", "init_stock": 10, "safety_stock": 0})
            chk(rr.status_code == 200, f"建物料 {name}: {rr.status_code} {rr.text[:60]}")

        async def mats(**kw):
            return (await c.get("/api/wh/materials", headers=H, params=kw)).json()

        full = await mats()
        chk(full["total"] == 5, f"1) 不带参数 = 全量: {full['total']}")

        hit = await mats(kw="轴承")
        chk(hit["total"] == 2, f"2) 带 kw 会缩小（所以这个结果**不能**拿去喂出库下拉）: {hit['total']}")
        names = {m["name"] for m in hit["materials"]}
        chk("圆头螺丝" not in names,
            f"2) 搜「轴承」时螺丝确实不在结果里——出库下拉若共用它，就是 #404 的现场: {sorted(names)}")

        low = await mats(low_only="true")
        chk(low["total"] <= full["total"],
            f"2) 「只看缺料」同理会缩小（比 kw 更狠，下拉只剩缺料的）: {low['total']}")

        # 3) 收货 → 立刻能在全量里选到（不用切菜单再回来）
        sup = (await c.post("/api/purchase-mgmt/suppliers", headers=H, json={"name": "验证供应商"})).json()
        item = (await c.post("/api/purchase-mgmt/items", headers=H, json={
            "po_no": "PO-404", "supplier_id": sup["id"], "item_name": "新到货垫片",
            "spec": "DN80", "qty": 88})).json()
        loc = (await c.post("/api/wh/locations", headers=HW, json={"name": "A-01"})).json()
        rr = await c.put(f"/api/purchase-mgmt/items/{item['id']}/receive", headers=HW, json={
            "arrival_date": "2026-08-19", "stock_location": loc["name"], "unit_price": 12})
        chk(rr.status_code == 200, f"3) 收货: {rr.status_code} {rr.text[:80]}")

        after = await mats()
        got = next((m for m in after["materials"] if m["name"] == "新到货垫片"), None)
        chk(got is not None and got["stock"] == 88,
            f"3) **刚收的货立刻出现在全量结果里**（出库下拉据此就能选到）: "
            f"{got and got['stock']}")

        # 同一时刻带着旧搜索词去查 —— 它就是找不到，正是 #404 的机制
        stale = await mats(kw="轴承")
        chk(not any(m["name"] == "新到货垫片" for m in stale["materials"]),
            "3) 而带着旧搜索词「轴承」去查，刚收的货不在里面——共用这份结果就会「啥也找不到」")

        # 4) scope 默认 all：挂了项目的料也要能出库
        pj = (await c.post("/api/projects", headers=H, json={"code": "2026-404", "name": "口径验证"})).json()
        m0 = next(m for m in after["materials"] if m["name"] == "密封圈")
        rr = await c.post("/api/wh/txns", headers=HW, json={
            "material_id": m0["id"], "direction": "in", "qty": 5, "unit_price": 3,
            "biz_date": "2026-08-19", "source": "采购收货", "project_id": pj["id"]})
        chk(rr.status_code == 200, f"4) 挂项目入库: {rr.status_code} {rr.text[:60]}")
        allm = await mats()
        pm = next(m for m in allm["materials"] if m["name"] == "密封圈")
        chk(pm["is_project_material"] is True, "4) 它成了项目物料")
        chk(any(m["name"] == "密封圈" for m in allm["materials"]),
            "4) **项目物料仍在默认(all)结果里**——不然挂了项目的料就没法出库了")
        gen = await mats(scope="general")
        chk(not any(m["name"] == "密封圈" for m in gen["materials"]),
            "4) 而 scope=general 会滤掉它（库存总览用这个口径，出库下拉不能用）")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
