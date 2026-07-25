"""🆕 反馈#298 财务请款审批列表「项目编号」列回归测试：
1. GET /api/finance/payment-requests 返回 project_codes = 请款单关联采购明细的项目编号（去重、排序）；
2. 多条明细不同编号 → 全部返回；编号为空/重复的明细不产生空值/重复值；
3. 无项目编号的请款单 → project_codes 为空列表（前端显示 —）。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="prcode")
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

        async def mk(u, rc):
            r = await c.post("/api/admin/users", headers=H,
                             json={"username": u, "password": "pass123", "full_name": u, "role_id": rid[rc]})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        async def login(u):
            return {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': u, 'password': 'pass123'})).json()['access_token']}"}

        b1 = await mk("b1", "buyer")
        await mk("f1", "finance")
        Hb1, Hf1 = await login("b1"), await login("f1")

        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb1, json={"name": "请款编号供应商"})
        chk(r.status_code == 200, f"建供应商: {r.text[:120]}")
        sid = r.json()["id"]

        # 三条明细：两个不同项目编号 + 一条无编号（无编号不该出现在 project_codes 里）
        async with SessionLocal() as db:
            i1 = models.PurchaseItem(supplier_id=sid, item_name="件一", buyer_id=b1, project_code="P-102")
            i2 = models.PurchaseItem(supplier_id=sid, item_name="件二", buyer_id=b1, project_code="P-101")
            i3 = models.PurchaseItem(supplier_id=sid, item_name="件三", buyer_id=b1, project_code=None)
            i4 = models.PurchaseItem(supplier_id=sid, item_name="件四", buyer_id=b1, project_code="P-101")  # 重复编号去重
            db.add_all([i1, i2, i3, i4]); await db.commit()
            ids = [i1.id, i2.id, i3.id, i4.id]

        # ===== 1+2. 请款单含多项目编号 → project_codes 去重排序返回 =====
        r = await c.post("/api/purchase-mgmt/payment-requests", headers=Hb1, json={
            "supplier_id": sid, "requested_amount": 100,
            "items": [{"item_id": i, "allocated_amount": 25} for i in ids]})
        chk(r.status_code == 200, f"发起请款: {r.text[:200]}")
        if r.status_code == 200:
            chk(r.json().get("project_codes") == ["P-101", "P-102"],
                f"创建即返回 project_codes 去重排序: {r.json().get('project_codes')}")

        r = await c.get("/api/finance/payment-requests", headers=Hf1, params={"status": "all"})
        chk(r.status_code == 200, f"财务请款列表 200: {r.status_code} {r.text[:150]}")
        rows = r.json()
        chk(len(rows) == 1, f"一条请款单: {len(rows)}")
        if rows:
            chk(rows[0].get("project_codes") == ["P-101", "P-102"],
                f"财务列表 project_codes 去重排序且无空值: {rows[0].get('project_codes')}")

        # ===== 3. 无项目编号的请款单 → 空列表 =====
        r = await c.post("/api/purchase-mgmt/items", headers=Hb1,
                         json={"supplier_id": sid, "item_name": "散件", "qty": 1, "unit_price": 5})
        chk(r.status_code == 200, f"建散件明细: {r.text[:120]}")
        loose_id = r.json()["id"]
        r = await c.post("/api/purchase-mgmt/payment-requests", headers=Hb1, json={
            "supplier_id": sid, "requested_amount": 5,
            "items": [{"item_id": loose_id, "allocated_amount": 5}]})
        chk(r.status_code == 200 and r.json().get("project_codes") == [],
            f"无编号请款单 project_codes=[]: {r.text[:150]}")
        r = await c.get("/api/finance/payment-requests", headers=Hf1, params={"status": "all"})
        by_id = {x["id"]: x for x in r.json()}
        if r.status_code == 200 and len(by_id) == 2:
            codes = sorted(x.get("project_codes") for x in by_id.values())
            chk(codes == [[], ["P-101", "P-102"]], f"两单 project_codes 各自正确: {codes}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
