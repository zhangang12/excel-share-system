"""🆕 工作台「分派给」可分派名单的多角色修复：
兼任负责人/管理层的设计师等，也应出现在该部门可分派工人名单(此前因锚点角色非 designer 被漏)。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="optmr")
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
    async with SessionLocal() as db:
        rid = {r.code: r.id for r in (await db.execute(select(models.Role))).scalars().all()}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'admin','password':'admin123'})).json()['access_token']}"}

        async def mk(uname, role_ids, fn):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "full_name": fn, "role_ids": role_ids})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        # 纯设计师 / 设计师+设计部负责人(锚点为负责人) / 管理层+设计师(锚点为管理层)
        a = await mk("d_pure", [rid["designer"]], "纯设计")
        b = await mk("d_lead", [rid["design_lead"], rid["designer"]], "设计兼负责人")
        d = await mk("d_mgr", [rid["manager"], rid["designer"]], "管理兼设计")
        # 对照：完全无设计师角色的人不应出现
        await mk("wh1", [rid["warehouse"]], "仓库")

        opts = (await c.get("/api/orders/options?dept=design", headers=H)).json()
        ids = {w["id"] for w in opts["workers"]}
        chk(a in ids, "纯设计师在名单")
        chk(b in ids, "设计师+设计部负责人(锚点负责人)也在名单")
        chk(d in ids, "管理层+设计师(锚点管理层)也在名单")
        chk(all(w["name"] for w in opts["workers"]), "名单含姓名")
        async with SessionLocal() as db:
            wh_id = (await db.execute(select(models.User.id).where(models.User.username == "wh1"))).scalar_one()
        chk(wh_id not in ids, "无设计师角色者不在名单")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
