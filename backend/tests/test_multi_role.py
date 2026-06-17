"""多角色 + 销售员名单接口 验证（临时 SQLite）。

覆盖：
1. 新建销售员立刻出现在 /api/sales/salespeople（修复：下拉来自真实用户名单，
   而非已有台账行聚合 —— 未挂台账也能选到）。
2. 多角色取并集：给用户加第二个角色（user_roles 关联），菜单 = 各角色菜单合集，
   且更宽松角色能解锁原角色没有的菜单（sales 无详单 + design_lead 有详单 → 出现 list）。
3. has_role / role_codes 并集语义。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="mroletest")
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
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mk(uname, rc, full):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "full_name": full, "role_id": rid[rc]})
            return r.json()["id"]

        # ---- 1) 新建销售员，未挂任何台账 ----
        sid = await mk("newsales", "sales", "新来的销售")

        sl_id = await mk("slead", "sales_lead", "销售主管")
        Hsl = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'slead','password':'pass123'})).json()['access_token']}"}

        people = (await c.get("/api/sales/salespeople", headers=Hsl)).json()
        ids = {p["id"] for p in people}
        chk(sid in ids, f"新建销售员应出现在 /salespeople（修复下拉 bug）: people={people}")
        chk(sl_id in ids, f"销售主管也应在名单: {people}")
        names = {p["name"] for p in people}
        chk("新来的销售" in names, f"名单含姓名: {names}")

        # ---- 2) 多角色：给 newsales 追加 design_lead 角色（直接写关联表） ----
        async with SessionLocal() as db:
            db.add(models.UserRole(user_id=sid, role_id=rid["design_lead"]))
            await db.commit()

        Hns = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'newsales','password':'pass123'})).json()['access_token']}"}
        mr = (await c.get("/api/auth/menus", headers=Hns)).json()
        keys = {m["key"] for m in mr["menus"]}
        # sales 菜单: catalog, sales（无 list）；design_lead: catalog, list, design
        chk("sales" in keys, f"并集含 sales 菜单: {keys}")
        chk("design" in keys, f"并集含 design 菜单(来自第二角色): {keys}")
        chk("list" in keys, f"并集解锁 list 详单(design_lead 有，sales 没有): {keys}")
        chk(mr["can_view_detail"] is True, f"多角色后可看详单(design_lead 放行): {mr['can_view_detail']}")

        # ---- 3) role_codes / has_role 并集 ----
        async with SessionLocal() as db:
            u = (await db.execute(select(models.User).where(models.User.id == sid))).scalar_one()
            codes = u.role_codes
            chk(codes == {"sales", "design_lead"}, f"role_codes 并集: {codes}")
            chk(u.has_role("design_lead") and u.has_role("sales"), "has_role 命中两个角色")
            chk(u.has_role("admin") is False, "has_role 不误命中未拥有角色")
            chk(set(u.role_ids) == {rid["sales"], rid["design_lead"]}, f"role_ids 并集: {u.role_ids}")

        # ---- 4) 管理端多选 API：建用户时传 role_ids ----
        r = await c.post("/api/admin/users", headers=H, json={
            "username": "multi1", "password": "pass123", "full_name": "多面手",
            "role_ids": [rid["warehouse"], rid["logistics"]]})
        chk(r.status_code == 200, f"role_ids 建用户成功: {r.status_code} {r.text[:120]}")
        body = r.json()
        chk(set(body["role_codes"]) == {"warehouse", "logistics"}, f"返回 role_codes: {body.get('role_codes')}")
        chk(set(body["role_ids"]) == {rid["warehouse"], rid["logistics"]}, f"返回 role_ids: {body.get('role_ids')}")
        muid = body["id"]

        # ---- 5) 更新角色集（整体替换） ----
        r = await c.put(f"/api/admin/users/{muid}", headers=H, json={"role_ids": [rid["finance"]]})
        chk(r.status_code == 200 and set(r.json()["role_codes"]) == {"finance"},
            f"role_ids 替换为单 finance: {r.text[:120]}")
        # 列表里该用户也只剩 finance
        lst = {u["id"]: u for u in (await c.get("/api/admin/users", headers=H)).json()}
        chk(set(lst[muid]["role_codes"]) == {"finance"}, f"列表反映替换: {lst.get(muid, {}).get('role_codes')}")

        # ---- 6) 旧调用兼容：只传单 role_id 仍可建用户 ----
        r = await c.post("/api/admin/users", headers=H, json={
            "username": "legacy1", "password": "pass123", "role_id": rid["designer"]})
        chk(r.status_code == 200 and r.json()["role_codes"] == ["designer"],
            f"兼容单 role_id 建用户: {r.text[:120]}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
