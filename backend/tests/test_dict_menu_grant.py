"""🆕 反馈#268 「字典设置」按账号开通测试：
1. 普通角色用户默认 /api/auth/menus 不含 dict-admin；admin/manager 天然含；
2. PUT /api/admin/users/{uid}/grant-menus {"grant_menus": ["dict-admin"]} 后，
   该用户 /api/auth/menus 含 dict-admin（追加在业务菜单之后）；
3. 取消 grant（空 list）后 menus 不再含 dict-admin；
4. 权限：非 admin/manager 调 PUT 403，未登录 401；
5. 非法菜单 key 400；用户列表/详情响应带出 grant_menus。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="dictgrant")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns

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
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        # 普通角色用户（warehouse）
        r = await c.post("/api/admin/users", headers=H,
                         json={"username": "w1", "password": "pass123", "full_name": "w1", "role_id": rid["warehouse"]})
        assert r.status_code == 200, r.text
        uid = r.json()["id"]
        chk(r.json().get("grant_menus") == [], f"新建用户 grant_menus 默认空: {r.json().get('grant_menus')}")
        Hw1 = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'w1','password':'pass123'})).json()['access_token']}"}

        async def menu_keys(h):
            r = await c.get("/api/auth/menus", headers=h)
            assert r.status_code == 200, r.text
            return [m["key"] for m in r.json()["menus"]]

        # ===== 1. 默认无 dict-admin；admin 有 =====
        keys = await menu_keys(Hw1)
        chk("dict-admin" not in keys, f"普通角色默认无 dict-admin: {keys}")
        keys_admin = await menu_keys(H)
        chk("dict-admin" in keys_admin, f"admin 天然含 dict-admin: {keys_admin}")

        # ===== 2. 开通后可见（追加在业务菜单后）=====
        r = await c.put(f"/api/admin/users/{uid}/grant-menus", headers=H,
                        json={"grant_menus": ["dict-admin"]})
        chk(r.status_code == 200, f"PUT grant-menus: {r.status_code} {r.text[:200]}")
        chk(r.json().get("grant_menus") == ["dict-admin"],
            f"PUT 响应带 grant_menus: {r.json().get('grant_menus')}")
        keys = await menu_keys(Hw1)
        chk("dict-admin" in keys, f"开通后 menus 含 dict-admin: {keys}")
        if "dict-admin" in keys:
            chk(keys[-1] == "dict-admin", f"dict-admin 追加在业务菜单之后: {keys}")
            item = [m for m in (await c.get('/api/auth/menus', headers=Hw1)).json()["menus"]
                    if m["key"] == "dict-admin"][0]
            chk(item["label"] == "字典设置", f"label 正确: {item}")

        # 用户列表响应带出 grant_menus
        users = (await c.get("/api/admin/users", headers=H)).json()
        u1 = [u for u in users if u["id"] == uid][0]
        chk(u1.get("grant_menus") == ["dict-admin"], f"用户列表带 grant_menus: {u1.get('grant_menus')}")

        # ===== 3. 取消后消失 =====
        r = await c.put(f"/api/admin/users/{uid}/grant-menus", headers=H, json={"grant_menus": []})
        chk(r.status_code == 200, f"取消 grant: {r.status_code} {r.text[:200]}")
        keys = await menu_keys(Hw1)
        chk("dict-admin" not in keys, f"取消后 menus 不含 dict-admin: {keys}")

        # ===== 4. 权限：非 admin/manager 403；未登录 401 =====
        r = await c.put(f"/api/admin/users/{uid}/grant-menus", headers=Hw1,
                        json={"grant_menus": ["dict-admin"]})
        chk(r.status_code == 403, f"普通角色调 PUT 403: {r.status_code}")
        r = await c.put(f"/api/admin/users/{uid}/grant-menus", json={"grant_menus": ["dict-admin"]})
        chk(r.status_code == 401, f"未登录 401: {r.status_code}")

        # ===== 5. 非法 key 400；不存在用户 404 =====
        r = await c.put(f"/api/admin/users/{uid}/grant-menus", headers=H,
                        json={"grant_menus": ["not-a-menu"]})
        chk(r.status_code == 400, f"非法菜单 key 400: {r.status_code}")
        r = await c.put("/api/admin/users/99999/grant-menus", headers=H,
                        json={"grant_menus": ["dict-admin"]})
        chk(r.status_code == 404, f"不存在用户 404: {r.status_code}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
