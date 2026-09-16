"""反馈#433（赵仁辉，2026-09-16）：备机下单「增加下单时间和交付时间」。

口径：两个日期选填；填了回写项目一览「签订日期」「交货日期」（与销售下单同一列，
alias 自动双写详单表头「下单日期」）；格式不对 400；交付早于下单 400；不填不写。
#432（反馈面板四个带数量的页签）是纯前端，数据仍走 /feedbacks?include_done=true，不在这里测。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb433")
os.environ["DATABASE_URL"] = os.environ.get("AUDIT_TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models
from app.routers.projects_router import OVERVIEW_KEY_PREFIX, HEADER_KEY_PREFIX
from app.sheet_templates import OVERVIEW_HEADER_ALIAS

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def extra_of(code):
    async with SessionLocal() as db:
        p = (await db.execute(select(models.Project).where(models.Project.code == code))).scalar_one()
        return p.extra or {}


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}

        async def spare(code, **kw):
            return await c.post("/api/orders/spare", headers=H, json={
                "code": code, "name": "100L真空乳化机(备机)", "depts": ["electric"], **kw})

        r = await spare("SP-433-A", sign_date="2026-09-16", deliver_date="2026/10/20")
        chk(r.status_code == 200, f"带下单/交付时间下单 -> {r.status_code} {r.text[:80]}")
        ex = await extra_of("SP-433-A")
        chk(ex.get(f"{OVERVIEW_KEY_PREFIX}签订日期") == "2026-09-16", f"项目一览·签订日期 -> {ex.get(OVERVIEW_KEY_PREFIX + '签订日期')}")
        chk(ex.get(f"{OVERVIEW_KEY_PREFIX}交货日期") == "2026-10-20", f"项目一览·交货日期（2026/10/20 规范化）-> {ex.get(OVERVIEW_KEY_PREFIX + '交货日期')}")
        alias = OVERVIEW_HEADER_ALIAS.get("签订日期")
        if alias:
            chk(ex.get(f"{HEADER_KEY_PREFIX}{alias}") == "2026-09-16", f"详单表头「{alias}」同步双写")

        r = await spare("SP-433-B")
        ex = await extra_of("SP-433-B")
        chk(r.status_code == 200 and f"{OVERVIEW_KEY_PREFIX}签订日期" not in ex and f"{OVERVIEW_KEY_PREFIX}交货日期" not in ex,
            "不填日期照常下单、不写空值（老客户端兼容）")

        r = await spare("SP-433-C", sign_date="2026-09-16", deliver_date="2026-09-01")
        chk(r.status_code == 400 and "早于" in r.text, f"交付早于下单被拒 -> {r.status_code}")
        r = await spare("SP-433-D", deliver_date="下个月")
        chk(r.status_code == 400 and "格式" in r.text, f"交付时间格式不对被拒 -> {r.status_code}")
        async with SessionLocal() as db:
            n = (await db.execute(select(models.Project).where(models.Project.code.in_(["SP-433-C", "SP-433-D"])))).scalars().all()
        chk(not n, "被拒的单没有建出项目")

    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败")
        sys.exit(1)
    print("✅ 全部通过")


asyncio.run(main())
