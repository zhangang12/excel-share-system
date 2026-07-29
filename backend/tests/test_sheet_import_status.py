"""🆕 反馈#327：采购部项目一览「预览」列红绿状态（GET /api/purchase-mgmt/sheets/import-status）：
1. imported_at 有值 → imported=true（无行也算已导入）；
2. imported_at 空但行数>0 → imported=true（手工建行也算有内容），record_count 正确；
3. 既无 imported_at 又无行 → imported=false；
4. 不存在的 id → imported=false/record_count=0；空 ids → 空表；
5. 批量混合 id 一次返回；采购角色可读（buyer），无权限角色 403。
"""
import asyncio, os, sys, tempfile, shutil
from datetime import datetime, timezone

tmp = tempfile.mkdtemp(prefix="sheetstatus")
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

URL = "/api/purchase-mgmt/sheets/import-status"


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

        await mk("b1", "buyer")
        await mk("w1", "warehouse")   # 非采购角色 → 403
        Hb1, Hw1 = await login("b1"), await login("w1")

        async with SessionLocal() as db:
            p = models.Project(code="T-ST1", name="状态测试项目", status="进行中")
            db.add(p); await db.flush()
            ds_empty = models.Datasheet(project_id=p.id, name="标准件清单")          # 无 imported_at 无行
            ds_rows = models.Datasheet(project_id=p.id, name="外协加工")             # 无 imported_at 有行
            ds_ts = models.Datasheet(project_id=p.id, name="激光件清单",
                                     imported_at=datetime(2026, 7, 20, tzinfo=timezone.utc))  # 有 imported_at 无行
            db.add_all([ds_empty, ds_rows, ds_ts]); await db.flush()
            f1 = models.Field(datasheet_id=ds_rows.id, name="名称", type="text", sort_order=1)
            db.add(f1); await db.flush()
            db.add(models.Record(datasheet_id=ds_rows.id, values={str(f1.id): "件A"}))
            db.add(models.Record(datasheet_id=ds_rows.id, values={str(f1.id): "件B"}))
            await db.commit()
            ids = (ds_empty.id, ds_rows.id, ds_ts.id)

        # ===== 1-4. 批量混合 + 不存在 id =====
        q = ",".join(str(i) for i in ids) + ",999999"
        r = await c.get(URL, headers=Hb1, params={"ids": q})
        chk(r.status_code == 200, f"状态 200: {r.status_code} {r.text[:200]}")
        st = r.json()
        e, rw, ts, no = st.get(str(ids[0])), st.get(str(ids[1])), st.get(str(ids[2])), st.get("999999")
        chk(e and e["imported"] is False and e["record_count"] == 0, f"空表未导入: {e}")
        chk(rw and rw["imported"] is True and rw["record_count"] == 2 and rw["imported_at"] is None,
            f"有行即已导入: {rw}")
        chk(ts and ts["imported"] is True and ts["record_count"] == 0 and ts["imported_at"],
            f"imported_at 有值即已导入: {ts}")
        chk(no and no["imported"] is False and no["record_count"] == 0, f"不存在id按未导入: {no}")

        # ===== 4b. 空 ids → 空表 =====
        r = await c.get(URL, headers=Hb1, params={"ids": " ,abc,"})
        chk(r.status_code == 200 and r.json() == {}, f"空ids空表: {r.status_code} {r.text[:80]}")

        # ===== 5. 权限：非采购角色 403 =====
        r = await c.get(URL, headers=Hw1, params={"ids": str(ids[0])})
        chk(r.status_code == 403, f"仓库角色 403: {r.status_code}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
