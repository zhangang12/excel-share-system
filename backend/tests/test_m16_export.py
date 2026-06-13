"""M16 导出审批：开关默认关(老导出不变)；开关开(非管理层拦截→申请→审批→永久放行)。"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="m16test")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app.config import settings

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
        r = await c.post("/api/auth/login", json={"username":"admin","password":"admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        async def mk(u, rc, fn):
            r = await c.post("/api/admin/users", headers=H, json={"username":u,"password":"pass123","full_name":fn,"role_id":rid[rc]})
            return r.json()["id"]
        d1 = await mk("d1", "designer", "张工")
        async def login(u):
            r = await c.post("/api/auth/login", json={"username":u,"password":"pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}
        Hd = await login("d1")

        # 建项目让设计师是成员（自动全员成员）
        await c.post("/api/projects", headers=H, json={"code":"E-1","name":"导出测试"})

        # ===== 开关默认关：老导出行为不变（设计师可导出一览）=====
        chk(settings.export_approval_enabled is False, "开关默认关闭")
        r = await c.get("/api/export-requests/config", headers=Hd)
        chk(r.json()["enabled"] is False and r.json()["can_export"] is True, f"关闭时config: {r.json()}")
        r = await c.get("/api/overview/export", headers=Hd)
        chk(r.status_code == 200, f"关闭时设计师可导出(老行为不变): {r.status_code}")

        # ===== 打开开关 =====
        settings.export_approval_enabled = True
        r = await c.get("/api/export-requests/config", headers=Hd)
        chk(r.json()["enabled"] is True and r.json()["can_export"] is False, f"开启时设计师无导出权: {r.json()}")
        # 设计师导出被拦截
        r = await c.get("/api/overview/export", headers=Hd)
        chk(r.status_code == 403, f"开启时设计师导出403: {r.status_code}")
        # 管理层免审
        r = await c.get("/api/overview/export", headers=H)
        chk(r.status_code == 200, "管理层导出免审")

        # 申请 → 查重 → 审批 → 放行
        r = await c.post("/api/export-requests", headers=Hd, json={"scope":"项目一览导出"})
        chk(r.status_code == 200, "提交导出申请")
        r = await c.post("/api/export-requests", headers=Hd, json={"scope":"再次"})
        chk(r.status_code == 400, "重复申请被拒")
        # 管理层(manager角色)收推送 + 列表（admin为系统账号,推送目标是manager角色池）
        Hm = await login("manager") if False else None
        rmg = await c.post("/api/auth/login", json={"username":"manager","password":"manager123"})
        Hmg = {"Authorization": f"Bearer {rmg.json()['access_token']}"}
        msgs = (await c.get("/api/messages", headers=Hmg)).json()
        chk(any("导出申请" in m["text"] for m in msgs), "管理层(manager)收导出申请推送")
        reqs = (await c.get("/api/export-requests", headers=H)).json()
        chk(len(reqs) == 1 and reqs[0]["status"] == "pending", "审批列表1条待审批")
        rqid = reqs[0]["id"]
        # 设计师不能审批
        r = await c.post(f"/api/export-requests/{rqid}/approve", headers=Hd)
        chk(r.status_code == 403, "设计师不能审批导出")
        # 管理层批准 → 永久放行
        r = await c.post(f"/api/export-requests/{rqid}/approve", headers=H)
        chk(r.status_code == 200, "管理层批准")
        msgs = (await c.get("/api/messages", headers=Hd)).json()
        chk(any("导出已批准" in m["text"] for m in msgs), "申请人收批准推送")
        r = await c.get("/api/export-requests/config", headers=Hd)
        chk(r.json()["can_export"] is True, "批准后获导出权")
        r = await c.get("/api/overview/export", headers=Hd)
        chk(r.status_code == 200, "批准后设计师可导出")

        # 关回开关，恢复
        settings.export_approval_enabled = False

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
