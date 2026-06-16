"""项目目录行级可见性回归测试：
- 设计/电工/装配 三类岗位仅见"自己接的项目"(被派单 worker_id=本人)
- 采购/仓库等其它角色 + 管理层 + 部门负责人 看全部
- 可逆开关 settings.project_dir_own_only=False 时全部回退为"全部可见"
"""
import asyncio, os, sys, tempfile, shutil

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
tmp = tempfile.mkdtemp(prefix="visibtest")
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
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mk(uname, rc):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "full_name": uname, "role_id": rid[rc]})
            return r.json()["id"]
        d1 = await mk("d1", "designer")
        b1 = await mk("b1", "buyer")
        dl = await mk("dl", "design_lead")
        s1 = await mk("s1", "sales")
        s2 = await mk("s2", "sales")
        pml = await mk("pml", "pm_lead")

        async def login(u):
            r = await c.post("/api/auth/login", json={"username": u, "password": "pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}
        Hd, Hb, Hdl, Hs1, Hs2, Hpm = (await login("d1"), await login("b1"), await login("dl"),
                                      await login("s1"), await login("s2"), await login("pml"))

        # 建两个项目 P1/P2（admin 建，自动把全员加为成员）
        p1 = (await c.post("/api/projects", headers=H, json={"code": "2099-101", "name": "项目甲"})).json()["id"]
        p2 = (await c.post("/api/projects", headers=H, json={"code": "2099-102", "name": "项目乙"})).json()["id"]

        # P1: 派设计任务给 d1 + 台账销售员=s1；P2: 台账销售员=s2
        # P3: 直接 DB 建, 不加任何成员 —— 复现"新用户不是老项目成员"的生产场景
        async with SessionLocal() as db:
            db.add(models.DeptOrder(project_id=p1, dept="design", worker_id=d1, created_by=1))
            db.add(models.SalesLedger(project_id=p1, sales_uid=s1, amount=100))
            db.add(models.SalesLedger(project_id=p2, sales_uid=s2, amount=200))
            p3 = models.Project(code="2099-103", name="无成员老项目", status="进行中")
            db.add(p3); await db.flush()
            p3id = p3.id
            await db.commit()

        def codes(rows): return {x["code"] for x in rows}

        # 🆕 核心修复: P3 无任何成员, 部门负责人(lead)仍应看到(绕过成员资格)——修复"新建主管看不到老项目"
        rows = (await c.get("/api/projects", headers=Hpm)).json()
        chk("2099-103" in codes(rows), f"pm_lead看到非成员项目P3(lead绕过成员): {codes(rows)}")
        # 设计师在 P3 无派单 → 看不到 P3
        rows = (await c.get("/api/projects", headers=Hd)).json()
        chk("2099-103" not in codes(rows), f"设计师无派单不应看到P3: {codes(rows)}")

        # 默认开启：设计师只看自己接的 P1
        rows = (await c.get("/api/projects", headers=Hd)).json()
        chk(codes(rows) == {"2099-101"}, f"设计师仅见自己接的P1: {codes(rows)}")
        # 销售员 s1 只看自己下单的 P1；s2 只看 P2
        rows = (await c.get("/api/projects", headers=Hs1)).json()
        chk(codes(rows) == {"2099-101"}, f"销售s1仅见自己下单的P1: {codes(rows)}")
        rows = (await c.get("/api/projects", headers=Hs2)).json()
        chk(codes(rows) == {"2099-102"}, f"销售s2仅见自己下单的P2: {codes(rows)}")
        # 采购不受限：看全部
        rows = (await c.get("/api/projects", headers=Hb)).json()
        chk({"2099-101", "2099-102"} <= codes(rows), f"采购看全部: {codes(rows)}")
        # 部门负责人看全部
        rows = (await c.get("/api/projects", headers=Hdl)).json()
        chk({"2099-101", "2099-102"} <= codes(rows), f"设计负责人看全部: {codes(rows)}")
        # 生产部主管 pm_lead：项目目录看全部 + 有项目详单权限
        rows = (await c.get("/api/projects", headers=Hpm)).json()
        chk({"2099-101", "2099-102"} <= codes(rows), f"生产部主管看全部项目目录: {codes(rows)}")
        menus = (await c.get("/api/auth/menus", headers=Hpm)).json()["menus"]
        mkeys = {m["key"] for m in menus}
        chk("list" in mkeys and "catalog" in mkeys, f"生产部主管有项目目录+详单菜单: {mkeys}")
        r = await c.get(f"/api/projects/{p2}", headers=Hpm)
        chk(r.status_code == 200, f"生产部主管可进入任意项目详单: {r.status_code}")
        # 管理层看全部
        rows = (await c.get("/api/projects", headers=H)).json()
        chk({"2099-101", "2099-102"} <= codes(rows), f"管理层看全部: {codes(rows)}")

        # 可逆开关关闭 → 设计师也回退看全部
        settings.project_dir_own_only = False
        try:
            rows = (await c.get("/api/projects", headers=Hd)).json()
            chk({"2099-101", "2099-102"} <= codes(rows), f"关开关后设计师回退看全部: {codes(rows)}")
        finally:
            settings.project_dir_own_only = True
        # 再开启 → 又只看 P1
        rows = (await c.get("/api/projects", headers=Hd)).json()
        chk(codes(rows) == {"2099-101"}, f"重开开关设计师又仅见P1: {codes(rows)}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
