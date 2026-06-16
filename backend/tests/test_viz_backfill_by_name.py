"""存量项目可见性补全回归：按项目目录 销售/电工/设计师 列的人名匹配系统用户 full_name，
补授「可见名单」__viz_uids__，使匹配到的人在项目目录看到自己经手的存量项目。
覆盖：人名匹配授权 / 多名拆分 / 同名歧义跳过 / 匹配不到跳过 / 销售列回填 sales_uid /
      列表可见性按可见名单生效 / 幂等。
"""
import asyncio, os, sys, tempfile, shutil

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
tmp = tempfile.mkdtemp(prefix="vizbf")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns, backfill_project_visibility_from_overview_names
from app import models
from app.routers.projects_router import OVERVIEW_KEY_PREFIX as O

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
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mk(uname, rc, full):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "full_name": full, "role_id": rid[rc]})
            return r.json()["id"]
        # 设计师 陈立新 / 电工 宋朴 / 销售 赵仁辉 ; 同名两个"王伟"(歧义)
        d1 = await mk("d1", "designer", "陈立新")
        e1 = await mk("e1", "electrician", "宋朴")
        s1 = await mk("s1", "sales", "赵仁辉")
        w1 = await mk("w1", "designer", "王伟")
        w2 = await mk("w2", "designer", "王伟")  # 同名歧义

        async def login(u):
            r = await c.post("/api/auth/login", json={"username": u, "password": "pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}
        Hd, He, Hs = await login("d1"), await login("e1"), await login("s1")

        # 直接建存量项目(不走派单), 用 extra __o__列 放人名(模拟导入的项目目录)
        async with SessionLocal() as db:
            p1 = models.Project(code="2026-001", name="存量甲", status="进行中",
                                extra={f"{O}设计师": "陈立新", f"{O}电工": "宋朴", f"{O}销售": "赵仁辉"})
            p2 = models.Project(code="2026-002", name="存量乙", status="进行中",
                                extra={f"{O}设计师": "李四", f"{O}电工": "王伟"})  # 李四=匹配不到, 王伟=歧义
            p3 = models.Project(code="2026-003", name="存量丙", status="进行中",
                                extra={f"{O}设计师": "陈立新、宋朴"})  # 多名拆分(陈立新+宋朴)
            db.add_all([p1, p2, p3]); await db.flush()
            db.add(models.SalesLedger(project_id=p1.id, amount=100))  # sales_uid 空, 待回填
            await db.commit()
            p1id, p2id, p3id = p1.id, p2.id, p3.id

        # 跑补全迁移
        async with SessionLocal() as db:
            r = await backfill_project_visibility_from_overview_names(db)
            chk(r["projects"] >= 2, f"应改若干项目: {r}")
            chk(r["sales_filled"] == 1, f"销售列回填 sales_uid 1 条: {r}")
            # P1 可见名单含 陈立新/宋朴/赵仁辉
            p1x = (await db.execute(select(models.Project).where(models.Project.id == p1id))).scalar_one()
            viz1 = set((p1x.extra or {}).get("__viz_uids__") or [])
            chk({d1, e1, s1} <= viz1, f"P1可见名单含设计/电工/销售: {viz1}")
            # P1 sales_uid 已回填为 赵仁辉
            led = (await db.execute(select(models.SalesLedger).where(models.SalesLedger.project_id == p1id))).scalar_one()
            chk(led.sales_uid == s1, f"P1 sales_uid 回填: {led.sales_uid}")
            # P3 多名拆分: 陈立新+宋朴 都在
            p3x = (await db.execute(select(models.Project).where(models.Project.id == p3id))).scalar_one()
            viz3 = set((p3x.extra or {}).get("__viz_uids__") or [])
            chk({d1, e1} <= viz3, f"P3多名拆分授权: {viz3}")
            # P2: 李四匹配不到 + 王伟歧义 → 可见名单为空
            p2x = (await db.execute(select(models.Project).where(models.Project.id == p2id))).scalar_one()
            viz2 = set((p2x.extra or {}).get("__viz_uids__") or [])
            chk(w1 not in viz2 and w2 not in viz2 and not viz2, f"P2歧义/不匹配不授权: {viz2}")

        def codes(rows): return {x["code"] for x in rows}
        # 列表可见性: 设计师陈立新 看到 P1+P3(可见名单), 看不到 P2
        rows = (await c.get("/api/projects", headers=Hd)).json()
        chk(codes(rows) == {"2026-001", "2026-003"}, f"设计师按可见名单见P1/P3: {codes(rows)}")
        # 电工宋朴 看到 P1+P3
        rows = (await c.get("/api/projects", headers=He)).json()
        chk(codes(rows) == {"2026-001", "2026-003"}, f"电工按可见名单见P1/P3: {codes(rows)}")
        # 销售赵仁辉 看到 P1(sales_uid 回填)
        rows = (await c.get("/api/projects", headers=Hs)).json()
        chk(codes(rows) == {"2026-001"}, f"销售见自己P1: {codes(rows)}")
        # 同名"王伟"w1 看不到任何(歧义未授权)
        rows = (await c.get("/api/projects", headers=await login("w1"))).json()
        chk(codes(rows) == set(), f"歧义同名不授权→看不到: {codes(rows)}")

        # 幂等: 再跑一次, 授权数为 0(并集不重复)
        async with SessionLocal() as db:
            r2 = await backfill_project_visibility_from_overview_names(db)
            chk(r2["granted"] == 0 and r2["projects"] == 0, f"幂等二次无新增: {r2}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
