"""反馈 #334/#335：设计部工作台「我的订单（已完成）」看不到数据

根因（我们自己引入的回归）：
  2026-07-30 `cfcc21a` 把 /api/orders 的 proj_status 从「项目状态」改成「任务单状态」
  （本身是对的，修的是"筛选与状态列对不上"），但工作台**一次取数供多个 tab 用**：
    · 前端 projStatusFilter 默认 '进行中' → 传 proj_status=进行中
    · 后端 → q.where(DeptOrder.status != "done")  ← 已完成的单在服务端就被删光
    · 前端 myDone = orders.filter(o => o.status === 'done')  ← 只能恒为空
  生产实证：陈立新名下 29 条、赵仁辉 5 条已完成设计单全部看不见（设计部共 57 条 done）。

修法：工作台取数不再传 proj_status；顶部状态下拉改为客户端过滤，且只作用于
「任务跟踪 / 外协」这类不按状态分 tab 的列表。后端 proj_status 语义保持 cfcc21a 不回退。

本测试锁住的是**后端契约**：不传 proj_status 时，同一份结果里必须同时含 done 与非 done，
这样前端才有可能分出三个 tab。同时保留 cfcc21a 的语义回归断言，防止有人图省事把它回退掉。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb334")
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

    tr = ASGITransport(app=app)
    async with AsyncClient(transport=tr, base_url="http://test", timeout=60) as c:
        async def login(u, p):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        r = await c.post("/api/admin/users", headers=H, json={
            "username": "wb_d1", "password": "pass123", "full_name": "wb_d1",
            "role_ids": [rid["designer"], rid["design_lead"]]})
        assert r.status_code == 200, r.text
        uid = r.json()["id"]
        await c.put(f"/api/admin/users/{uid}/menus", headers=H,
                    json={"menus": ["catalog", "list", "design", "messages"]})
        Hd = await login("wb_d1", "pass123")

        # 造数：同一个设计师名下 3 条已完成 + 2 条进行中；
        # 关键：项目本身仍是「进行中」——这正是修复前被 proj_status=进行中 剔掉的那批
        await c.post("/api/projects", headers=H, json={"code": "WB-2601", "name": "工作台测试项目"})
        async with SessionLocal() as db:
            p = (await db.execute(select(models.Project)
                                  .where(models.Project.code == "WB-2601"))).scalar_one()
            p.status = "进行中"
            for i in range(3):
                db.add(models.DeptOrder(project_id=p.id, dept="design", worker_id=uid,
                                        status="done", start_date="2026-01-05",
                                        due_date="2026-01-20", done_date="2026-01-18"))
            for i in range(2):
                db.add(models.DeptOrder(project_id=p.id, dept="design", worker_id=uid,
                                        status="in_progress", start_date="2026-01-05",
                                        due_date="2026-01-30"))
            await db.commit()

        async def listing(**params):
            r = await c.get("/api/orders", headers=Hd, params={"dept": "design", **params})
            assert r.status_code == 200, r.text
            return r.json()

        # ===== 1. ★核心：不传 proj_status 时，done 与非 done 必须同时在结果里 =====
        rows = await listing(limit=500)
        done = [o for o in rows if o["status"] == "done"]
        undone = [o for o in rows if o["status"] != "done"]
        chk(len(done) == 3, f"★不传状态时应含 3 条已完成，实得 {len(done)}")
        chk(len(undone) == 2, f"★不传状态时应含 2 条未完成，实得 {len(undone)}")
        chk(len(done) > 0 and len(undone) > 0,
            "★同一份结果必须同时含 done 与非 done，前端才分得出三个 tab（这是 #334 的回归点）")

        # ===== 2. 保留 cfcc21a 的语义：显式传状态时按【任务单状态】筛，不是项目状态 =====
        rows = await listing(proj_status="已完成", limit=500)
        chk(len(rows) == 3 and all(o["status"] == "done" for o in rows),
            f"proj_status=已完成 应按任务单状态筛出 3 条 done，实得 {len(rows)}")
        chk(all(o["status"] == "done" for o in rows),
            "★不得回退成按 Project.status 筛——项目是「进行中」，若按项目状态筛这里会是 0 条")

        rows = await listing(proj_status="进行中", limit=500)
        chk(len(rows) == 2 and all(o["status"] != "done" for o in rows),
            f"proj_status=进行中 应筛出 2 条未完成，实得 {len(rows)}")

        # ===== 3. 复现原始 bug：默认筛选 + 客户端 done 过滤 = 空（修复后前端不再这么用）=====
        rows = await listing(proj_status="进行中", limit=500)
        client_done = [o for o in rows if o["status"] == "done"]
        chk(len(client_done) == 0,
            "（记录用）传 proj_status=进行中 时服务端确实把 done 删光——所以前端不能再传它")

        # ===== 4. limit 参数可用（前端改传 500，防年份放宽后被 200 截断）=====
        r = await c.get("/api/orders", headers=Hd, params={"dept": "design", "limit": 500})
        chk(r.status_code == 200, f"limit=500 应被接受: {r.status_code} {r.text[:120]}")
        r = await c.get("/api/orders", headers=Hd, params={"dept": "design", "limit": 501})
        chk(r.status_code == 422, f"limit 上限 500，501 应 422: {r.status_code}")

    print("PASSED" if not FAIL else f"FAILED {len(FAIL)}")

asyncio.run(main())
