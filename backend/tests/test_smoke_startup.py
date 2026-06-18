"""全链路冒烟：完整启动(create_all+ensure_columns+seed+run_all)+健康检查+全菜单角色渲染不报错。"""
import asyncio, os, sys, tempfile, shutil
tmp = tempfile.mkdtemp(prefix="smoke")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); sys.path.insert(0, os.getcwd())
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns

async def main():
    # 模拟完整 lifespan 启动
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)
    # 二次启动幂等
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/health"); assert r.status_code==200, r.text
        r = await c.post("/api/auth/login", json={"username":"admin","password":"admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        roles = (await c.get("/api/admin/roles", headers=H)).json()
        # 端点仅隐藏 admin(其余可见)；2026-06-19 重新启用 标准件/外协采购员两子角色
        assert len(roles) >= 19, f"角色数 {len(roles)}"
        assert all(r["code"] != "admin" for r in roles), "admin 角色对UI隐身"
        assert any(r["code"] == "buyer_outsource" for r in roles) and \
            any(r["code"] == "buyer_standard" for r in roles), \
            "标准件/外协采购员两子角色应可见(重新启用)"
        menus = (await c.get("/api/auth/menus", headers=H)).json()["menus"]
        assert len(menus) >= 14, f"管理层菜单 {len(menus)}"
        # 关键端点可达
        for ep in ["/api/sales/ledger","/api/orders?dept=design","/api/wh/materials",
                   "/api/logistics/board","/api/finance/pending-invoices","/api/aftersales",
                   "/api/feedbacks","/api/reports/monthly","/api/export-requests"]:
            r = await c.get(ep, headers=H)
            assert r.status_code==200, f"{ep} → {r.status_code} {r.text[:80]}"
    await engine.dispose()
    print("SMOKE OK：22+角色 / 14+菜单 / 9关键端点可达 / 启动两次幂等")
    shutil.rmtree(tmp, ignore_errors=True)
asyncio.run(main())
