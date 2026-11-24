"""采购收货的单号、项目号、物料和规格搜索，以及旧 po_no 参数兼容。"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="recvfilter")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

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
        # 直接建行：一个供应商 + 三条采购明细（两条带采购单号分属不同项目，一条无单号散件）
        sup = models.Supplier(name="测试供应商")
        db.add(sup); await db.flush()
        db.add_all([
            models.PurchaseItem(po_no="PO20260728-001", supplier_id=sup.id,
                                project_code="2026-053", item_name="轴承"),
            models.PurchaseItem(po_no="PO20260728-002", supplier_id=sup.id,
                                project_code="2026-077", item_name="电机"),
            models.PurchaseItem(po_no=None, supplier_id=sup.id,
                                project_code="2026-053", item_name="散件"),
            models.PurchaseItem(po_no="PO20260701-001", supplier_id=sup.id,
                                item_name="气控阀", arrival_date="2026-07-02"),
            models.PurchaseItem(po_no="PO20260701-002", supplier_id=sup.id,
                                item_name="外购", spec="304气控阀", arrival_date="2026-07-03"),
            models.PurchaseItem(po_no="PO20260801-001", supplier_id=sup.id,
                                item_name="新的已收货物料", arrival_date="2026-08-02"),
            models.PurchaseItem(po_no="PO20260801-002", supplier_id=sup.id,
                                item_name="新的已收货物料", arrival_date="2026-08-02"),
        ])
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'admin','password':'admin123'})).json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        r = await c.post("/api/admin/users", headers=H,
                         json={"username": "wh1", "password": "pass123", "full_name": "wh1", "role_id": rid["warehouse"]})
        assert r.status_code == 200, r.text
        Hw = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'wh1','password':'pass123'})).json()['access_token']}"}

        # ===== 1. 不带 po_no 参数拉全量待收货（前端现在的拉取方式） =====
        r = await c.get("/api/purchase-mgmt/receiving", params={"received": "false"}, headers=Hw)
        chk(r.status_code == 200, f"收货清单 200: {r.status_code} {r.text[:200]}")
        rows = r.json()
        chk(len(rows) == 3, f"3 条待收货: {len(rows)}")
        if rows:
            chk("po_no" in rows[0] and "project_code" in rows[0], "行内含 po_no/project_code 字段")

        # 项目编号和采购单号都走接口查询。
        for keyword, expected in [(" 2026-053 ", ["散件", "轴承"]), ("PO20260728-001", ["轴承"]),
                                  ("po20260728-002", ["电机"]), ("2026-999", [])]:
            r = await c.get("/api/purchase-mgmt/receiving", params={"keyword": keyword}, headers=Hw)
            chk(r.status_code == 200 and sorted(x["item_name"] for x in r.json()) == expected,
                f"匹配 {keyword!r}: {r.text}")

        # 旧已收货明细位于首批加载范围外，按名称或规格仍能查到。
        path = "/api/purchase-mgmt/receiving"
        r = await c.get(path, params={"received": True, "limit": 2}, headers=Hw)
        chk(r.status_code == 200 and all(x["po_no"].startswith("PO202608") for x in r.json()),
            "初始列表的两条都是较新的明细")
        r = await c.get(path, params={"received": True, "limit": 2, "item_name": " 气控阀 "}, headers=Hw)
        chk(r.status_code == 200 and {x["item_name"] for x in r.json()} == {"气控阀", "外购"},
            "按名称和规格查到两条较旧的明细")

        # ===== 3. 后端旧 po_no 参数行为不变（只按采购单号过滤；项目编号搜不到——正是 #315 根源） =====
        r = await c.get("/api/purchase-mgmt/receiving",
                        params={"received": "false", "po_no": "PO20260728-001"}, headers=Hw)
        chk(r.status_code == 200 and [x["item_name"] for x in r.json()] == ["轴承"],
            f"旧 po_no 参数按采购单号过滤不变: {r.status_code} {[x['item_name'] for x in r.json()]}")
        r = await c.get("/api/purchase-mgmt/receiving",
                        params={"received": "false", "po_no": "2026-053"}, headers=Hw)
        chk(r.status_code == 200 and r.json() == [],
            f"旧 po_no 参数输项目编号仍空(契约未动): {r.status_code} {r.json()}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
