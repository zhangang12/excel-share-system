"""🆕 #315 收货筛选框：一个框同时模糊匹配 采购单号(po_no)/订单编号(project_code)。

修复方式：过滤从后端 po_no 参数挪到前端（WarehouseView.vue 的 filteredRecv）——
后端 po_no 参数只按采购单号过滤，输项目编号(如 2026-053)会搜不到。
本测试锁定前端所依赖的数据契约：
1. GET /purchase-mgmt/receiving（不带 po_no 参数）返回行带 po_no + project_code；
2. 用与 filteredRecv 相同的谓词（去空格、忽略大小写、po_no/project_code 任一子串命中），
   项目编号「2026-053」与采购单号片段都能命中目标行；
3. 后端旧 po_no 参数行为不变（仍只按采购单号过滤）——老桌面客户端契约不受影响（API 只增不改）。
"""
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


def frontend_hit(row: dict, q: str) -> bool:
    """与 WarehouseView.vue filteredRecv 中 #315 谓词保持一致（忽略大小写子串，po_no/project_code 任一命中）。"""
    q = q.strip().lower()
    return (not q
            or q in (row.get("po_no") or "").lower()
            or q in (row.get("project_code") or "").lower())


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
        by_name = {x["item_name"]: x for x in rows}
        if rows:
            chk("po_no" in rows[0] and "project_code" in rows[0], "行内含 po_no/project_code 字段")

        # ===== 2. 前端谓词：项目编号 / 采购单号都能命中 =====
        hit_053 = sorted(x["item_name"] for x in rows if frontend_hit(x, "2026-053"))
        chk(hit_053 == ["散件", "轴承"], f"输项目编号 2026-053 命中 轴承+散件: {hit_053}")
        hit_po = [x["item_name"] for x in rows if frontend_hit(x, "PO20260728-001")]
        chk(hit_po == ["轴承"], f"输采购单号 PO20260728-001 命中 轴承: {hit_po}")
        hit_low = [x["item_name"] for x in rows if frontend_hit(x, "po20260728-002")]
        chk(hit_low == ["电机"], f"小写采购单号也命中(忽略大小写): {hit_low}")
        hit_none = [x["item_name"] for x in rows if frontend_hit(x, "2026-999")]
        chk(hit_none == [], f"不存在的编号不命中: {hit_none}")

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
