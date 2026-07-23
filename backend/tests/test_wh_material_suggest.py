"""🆕 #278/#289/#290 仓库域反馈测试：
1. GET /api/wh/materials/suggest：未登录 401；关键字命中（含 spec 返回）；
   前缀命中排前面；最多 20 条；q 为空返回 []。
2. #290 下单时间：GET /api/purchase-mgmt/receiving 返回里带 delivery_date（下单日期），
   前端「下单时间」列/筛选直接消费该字段（后端无需改，只验证字段存在且值正确）。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="wh_suggest")
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

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'admin','password':'admin123'})).json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        r = await c.post("/api/admin/users", headers=H,
                         json={"username": "w1", "password": "pass123", "full_name": "w1", "role_id": rid["warehouse"]})
        assert r.status_code == 200, r.text
        Hw1 = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'w1','password':'pass123'})).json()['access_token']}"}

        # ===== 1. suggest 未登录 401 =====
        r = await c.get("/api/wh/materials/suggest", params={"q": "手"})
        chk(r.status_code == 401, f"suggest 未登录 401: {r.status_code}")

        # ===== 2. 建物料：命中 + 排序 + spec 返回 =====
        for name, spec in [("手套", "均码"), ("防滑手套", "L码"), ("螺丝", "M8")]:
            r = await c.post("/api/wh/materials", headers=H, json={"name": name, "spec": spec})
            chk(r.status_code == 200, f"建物料 {name}: {r.status_code} {r.text[:120]}")

        r = await c.get("/api/wh/materials/suggest", headers=H, params={"q": "手"})
        chk(r.status_code == 200, f"suggest 查询 200: {r.status_code} {r.text[:200]}")
        data = r.json()
        names = [x["name"] for x in data]
        chk(names == ["手套", "防滑手套"], f"命中且前缀排前: {names}")
        if data:
            chk(data[0]["spec"] == "均码", f"返回 spec: {data[0]}")
        # 登录的任意角色可读（仓库页可读口径=登录即可）
        r = await c.get("/api/wh/materials/suggest", headers=Hw1, params={"q": "手"})
        chk(r.status_code == 200 and len(r.json()) == 2, f"warehouse 角色可读: {r.status_code}")

        # ===== 3. limit 20 =====
        for i in range(1, 26):
            await c.post("/api/wh/materials", headers=H, json={"name": f"测试料{i:02d}"})
        r = await c.get("/api/wh/materials/suggest", headers=H, params={"q": "测试料"})
        chk(r.status_code == 200 and len(r.json()) == 20, f"limit 20: {len(r.json())}")

        # ===== 4. q 为空 → [] =====
        r = await c.get("/api/wh/materials/suggest", headers=H, params={"q": "  "})
        chk(r.status_code == 200 and r.json() == [], f"空关键字返回空: {r.status_code} {r.text[:80]}")
        r = await c.get("/api/wh/materials/suggest", headers=H)
        chk(r.status_code == 200 and r.json() == [], f"缺省 q 返回空: {r.status_code} {r.text[:80]}")

        # ===== 5. #290 收货列表返回 delivery_date（下单时间） =====
        async with SessionLocal() as db:
            sup = models.Supplier(name="测试供应商S1")
            db.add(sup); await db.flush()
            db.add(models.PurchaseItem(supplier_id=sup.id, item_name="测试手套",
                                       spec="均码", qty=10, delivery_date="2026-07-01"))
            await db.commit()
        r = await c.get("/api/purchase-mgmt/receiving", headers=Hw1, params={"received": False})
        chk(r.status_code == 200, f"receiving 200: {r.status_code} {r.text[:200]}")
        rows = [x for x in r.json() if x["item_name"] == "测试手套"]
        chk(len(rows) == 1, f"待收货含测试明细: {len(rows)}")
        if rows:
            chk(rows[0].get("delivery_date") == "2026-07-01",
                f"下单时间字段存在且正确: {rows[0].get('delivery_date')}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
