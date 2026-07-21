"""一级菜单按账号配置 探针(临时 SQLite)。原「多角色菜单并集」探针按新语义重写
（2026-07-21 角色菜单矩阵 ROLE_MENUS 废除，一级菜单读 User.menus）：

(1) 管理层压制：用户 [manager, sales] 调 GET /api/auth/menus 应拿到
    全部业务菜单 + 管理组菜单(与纯 admin/manager 一致),不被 sales 收窄。
(2) 建号预填：[warehouse, finance, logistics] 建号时 menus 预填=
    三角色 ROLE_DEFAULT_MENUS 并集 + messages/oa，菜单即该预填值,不多不少。
(3) 建号后改角色不影响菜单：sales 建号后把角色改成 design_lead，
    /api/auth/menus 仍是建号预填值（角色不再驱动菜单）。
(4) PUT /api/admin/users/{uid}/menus 直接生效：整体替换后菜单=新值。

角色 id 直接查 DB(SessionLocal + models.Role,因 admin/manager 对
/api/admin/roles 隐藏)。多角色用 POST /api/admin/users role_ids 建。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="mrmenu")
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
from app.menus import MENU_DEFS, ADMIN_MENU_DEFS, ROLE_DEFAULT_MENUS

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    # 全部角色 id（含 admin/manager 等对 /roles 隐藏的）直接查 DB
    async with SessionLocal() as db:
        rid = {r.code: r.id for r in (await db.execute(select(models.Role))).scalars().all()}
    for need in ("manager", "sales", "design_lead", "warehouse", "finance", "logistics", "admin"):
        chk(need in rid, f"seed 中存在角色 {need}: rid={sorted(rid)}")

    ALL_BIZ = {m["key"] for m in MENU_DEFS}
    ALL_ADMIN = {m["key"] for m in ADMIN_MENU_DEFS}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        async def login(u, p="pass123"):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, f"login {u}: {r.text}"
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")

        async def mk(uname, role_ids):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "role_ids": role_ids})
            assert r.status_code == 200, f"mk {uname}: {r.text}"
            return r.json()["id"]

        async def menu_keys(headers):
            r = await c.get("/api/auth/menus", headers=headers)
            assert r.status_code == 200, f"GET /api/auth/menus: {r.text}"
            return {m["key"] for m in r.json()["menus"]}, r.json()

        # 基线：纯 manager 与纯 admin 的菜单(用于「一致」对比)
        await mk("mgr_only", [rid["manager"]])
        H_mgr_only = await login("mgr_only")
        base_mgr, _ = await menu_keys(H_mgr_only)
        base_admin, _ = await menu_keys(H)  # admin 登录态
        expect_full = ALL_BIZ | ALL_ADMIN
        chk(base_mgr == expect_full,
            f"纯 manager 菜单=全业务+管理组: 实际={sorted(base_mgr)} 期望={sorted(expect_full)}")
        chk(base_admin == expect_full,
            f"admin 菜单=全业务+管理组: 实际={sorted(base_admin)}")

        # ================= (1) 管理层压制：[manager, sales] =================
        await mk("mgr_sales", [rid["manager"], rid["sales"]])
        H_ms = await login("mgr_sales")
        ms_keys, ms_raw = await menu_keys(H_ms)
        chk(ms_keys == expect_full,
            f"[manager,sales] 菜单=全业务+管理组(不被 sales 收窄): "
            f"实际={sorted(ms_keys)} 期望={sorted(expect_full)}")
        # 与纯 manager / admin 完全一致
        chk(ms_keys == base_mgr,
            f"[manager,sales] 与纯 manager 一致: 多={sorted(ms_keys-base_mgr)} 少={sorted(base_mgr-ms_keys)}")
        chk(ms_keys == base_admin,
            f"[manager,sales] 与 admin 一致: 多={sorted(ms_keys-base_admin)} 少={sorted(base_admin-ms_keys)}")
        # 含管理组关键菜单(不被 sales 压成业务菜单)
        chk(ALL_ADMIN <= ms_keys,
            f"[manager,sales] 含全部管理组菜单: 缺={sorted(ALL_ADMIN-ms_keys)}")
        chk(ms_raw.get("can_view_detail") is True,
            f"[manager,sales] can_view_detail=True: {ms_raw.get('can_view_detail')}")

        # ================= (2) 建号预填：[warehouse, finance, logistics] =================
        wfl_id = await mk("wfl", [rid["warehouse"], rid["finance"], rid["logistics"]])
        H_wfl = await login("wfl")
        wfl_keys, wfl_raw = await menu_keys(H_wfl)
        expect_wfl = (
            set(ROLE_DEFAULT_MENUS["warehouse"])
            | set(ROLE_DEFAULT_MENUS["finance"])
            | set(ROLE_DEFAULT_MENUS["logistics"])
            | {"messages", "oa"}
        )
        # 显式拼出期望:三部门模板合集 + messages/oa（finance 模板含 purchase_mgmt）
        chk(expect_wfl == {"catalog", "list", "warehouse", "finance", "logistics",
                           "purchase_mgmt", "messages", "oa"},
            f"期望集自检: {sorted(expect_wfl)}")
        chk(wfl_keys == expect_wfl,
            f"[warehouse,finance,logistics] 菜单=建号预填(三角色模板并集+messages/oa): "
            f"实际={sorted(wfl_keys)} 期望={sorted(expect_wfl)} "
            f"多={sorted(wfl_keys-expect_wfl)} 少={sorted(expect_wfl-wfl_keys)}")
        # 不多:不应出现无关部门菜单
        for stray in ("sales", "design", "electric", "produce", "sheet", "purchase", "aftersales", "report"):
            chk(stray not in wfl_keys, f"[warehouse,finance,logistics] 不含无关菜单 {stray}")
        # 不应出现任何管理组菜单
        chk(not (ALL_ADMIN & wfl_keys),
            f"[warehouse,finance,logistics] 不含管理组菜单: 误含={sorted(ALL_ADMIN & wfl_keys)}")
        # 三部门各自部门菜单都在
        for dept in ("warehouse", "finance", "logistics"):
            chk(dept in wfl_keys, f"[warehouse,finance,logistics] 含 {dept} 部门菜单")

        # ================= (3) 建号后改角色不影响菜单 =================
        s_id = await mk("s_role", [rid["sales"]])
        H_s = await login("s_role")
        s_before, _ = await menu_keys(H_s)
        expect_sales = set(ROLE_DEFAULT_MENUS["sales"]) | {"messages", "oa"}
        chk(s_before == expect_sales,
            f"sales 建号预填=模板+messages/oa: 实际={sorted(s_before)} 期望={sorted(expect_sales)}")
        r = await c.put(f"/api/admin/users/{s_id}", headers=H, json={"role_ids": [rid["design_lead"]]})
        chk(r.status_code == 200, f"改角色为 design_lead: {r.status_code} {r.text[:150]}")
        s_after, s_raw = await menu_keys(H_s)
        chk(s_after == s_before,
            f"改角色后菜单不变(角色不再驱动菜单): 改前={sorted(s_before)} 改后={sorted(s_after)}")
        chk("list" not in s_after and s_raw.get("can_view_detail") is False,
            f"改角色后仍无 list 详单(不随 design_lead 解锁): {sorted(s_after)}")

        # ================= (4) PUT menus 直接生效 =================
        r = await c.put(f"/api/admin/users/{wfl_id}/menus", headers=H,
                        json={"menus": ["catalog", "list", "design", "messages", "oa", "dict-admin"]})
        chk(r.status_code == 200, f"PUT menus: {r.status_code} {r.text[:200]}")
        chk(r.json().get("menus") == ["catalog", "list", "design", "oa", "messages", "dict-admin"],
            f"PUT 响应带规范序 menus: {r.json().get('menus')}")
        wfl_keys2, wfl_raw2 = await menu_keys(H_wfl)
        chk(wfl_keys2 == {"catalog", "list", "design", "oa", "messages", "dict-admin"},
            f"PUT menus 后菜单=新值: 实际={sorted(wfl_keys2)}")
        chk(wfl_raw2.get("can_view_detail") is True,
            f"PUT menus 含 list 后 can_view_detail=True: {wfl_raw2.get('can_view_detail')}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
