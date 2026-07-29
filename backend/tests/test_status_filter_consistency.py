"""🆕 2026-07-30 举一反三：状态筛选口径一致性测试
1. 工作台列表（orders list）：「已完成」筛选按任务单状态（DeptOrder.status）——项目整体仍「进行中」
   但任务单已完成的行应命中；任务单未完成的行不应命中；「进行中」筛选反之。
2. 物流看板：「已完成」筛选只看真实发货（Shipment.status==shipped）——项目手动置已完成但
   未发货的行不再混入；已发货的命中。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="statfilt")
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

        async with SessionLocal() as db:
            p1 = models.Project(code="SF-T01", name="任务单已完成项目", status="进行中")
            p2 = models.Project(code="SF-T02", name="任务单未完成项目", status="进行中")
            p3 = models.Project(code="SF-T03", name="手动完成未发货", status="已完成")
            p4 = models.Project(code="SF-T04", name="已发货项目", status="进行中")
            db.add_all([p1, p2, p3, p4]); await db.flush()
            db.add(models.DeptOrder(project_id=p1.id, dept="design", status="done"))
            db.add(models.DeptOrder(project_id=p2.id, dept="design", status="in_progress"))
            db.add(models.Shipment(project_id=p3.id, status="pending"))
            db.add(models.Shipment(project_id=p4.id, status="shipped"))
            await db.commit()

        # ===== 1. 工作台列表筛选=任务单状态 =====
        r = await c.get("/api/orders", headers=H, params={"dept": "design", "proj_status": "已完成"})
        codes = {x.get("project_code") or x.get("code") for x in r.json()}
        chk("SF-T01" in codes and "SF-T02" not in codes, f"已完成筛选命中任务单done: {codes}")
        r = await c.get("/api/orders", headers=H, params={"dept": "design", "proj_status": "进行中"})
        codes = {x.get("project_code") or x.get("code") for x in r.json()}
        chk("SF-T02" in codes and "SF-T01" not in codes, f"进行中筛选排除任务单done: {codes}")

        # ===== 2. 物流看板已完成=真实发货 =====
        r = await c.get("/api/logistics/board", headers=H, params={"proj_status": "已完成"})
        codes = {x.get("project_code") or x.get("code") for x in r.json()}
        chk("SF-T04" in codes, f"已发货命中: {codes}")
        chk("SF-T03" not in codes, f"手动完成未发货不混入: {codes}")

    await engine.dispose()
    if FAIL:
        print(f"\n{len(FAIL)} 项失败"); sys.exit(1)
    print("PASSED")

asyncio.run(main())
