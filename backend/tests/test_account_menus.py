"""🆕 一级菜单按账号配置 测试（临时 SQLite）：

1. PUT /api/admin/users/{uid}/menus 整体替换生效：响应/登录后 /api/auth/menus 均为新值
   （规范序：业务 key 按 MENU_DEFS 序、管理组 key 排尾）；UserOut.grant_menus 为派生值。
2. 非法菜单 key 400；非管理层 403；未登录 401；manager 改 admin 账号 403（保护口径同 update_user）。
3. backfill_user_menus：直接建 User 行（menus=NULL）跑迁移 →
   menus = 角色 ROLE_DEFAULT_MENUS 并集 ∪ (grant_menus ∩ 管理组有效key) ∪ {messages, oa}；
   幂等（二次跑 filled=0）；admin/manager 跳过不填。
4. grant-menus 兼容包装：只对管理组 key 增删，业务 key 不动（自定义业务配置保留）。
5. GET /api/admin/menu-defs 返回 business/admin 两组定义（前端「菜单权限」弹窗数据源）。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="acctmenu")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns, backfill_user_menus
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
        async def login(u, p):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, f"login {u}: {r.text}"
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        # 普通角色用户（warehouse）
        r = await c.post("/api/admin/users", headers=H,
                         json={"username": "w1", "password": "pass123", "full_name": "w1", "role_id": rid["warehouse"]})
        assert r.status_code == 200, r.text
        uid = r.json()["id"]
        # 建号预填：warehouse 模板 + messages/oa；grant_menus 派生空
        chk(r.json().get("menus") == ["catalog", "list", "warehouse", "oa", "messages"],
            f"建号预填 menus=角色模板+messages/oa: {r.json().get('menus')}")
        chk(r.json().get("grant_menus") == [], f"建号 grant_menus 派生空: {r.json().get('grant_menus')}")
        Hw1 = await login("w1", "pass123")

        async def menu_keys(h):
            r = await c.get("/api/auth/menus", headers=h)
            assert r.status_code == 200, r.text
            return [m["key"] for m in r.json()["menus"]]

        # ===== 1. PUT menus 整体替换生效（乱序+重复入参 → 规范序去重落库）=====
        r = await c.put(f"/api/admin/users/{uid}/menus", headers=H,
                        json={"menus": ["messages", "design", "dict-admin", "catalog", "list", "design", "oa"]})
        chk(r.status_code == 200, f"PUT menus: {r.status_code} {r.text[:200]}")
        chk(r.json().get("menus") == ["catalog", "list", "design", "oa", "messages", "dict-admin"],
            f"PUT 响应规范序 menus: {r.json().get('menus')}")
        chk(r.json().get("grant_menus") == ["dict-admin"],
            f"grant_menus 派生=menus∩管理组: {r.json().get('grant_menus')}")
        keys = await menu_keys(Hw1)
        chk(keys == ["catalog", "list", "design", "oa", "messages", "dict-admin"],
            f"PUT 后 /api/auth/menus=新值: {keys}")
        # 用户列表也带 menus
        users = (await c.get("/api/admin/users", headers=H)).json()
        u1 = [u for u in users if u["id"] == uid][0]
        chk(u1.get("menus") == ["catalog", "list", "design", "oa", "messages", "dict-admin"],
            f"用户列表带 menus: {u1.get('menus')}")
        # 清空 → 什么菜单都没有（含 messages/oa，不再无条件追加）
        r = await c.put(f"/api/admin/users/{uid}/menus", headers=H, json={"menus": []})
        chk(r.status_code == 200 and r.json().get("menus") == [], f"PUT 空 menus: {r.text[:200]}")
        keys = await menu_keys(Hw1)
        chk(keys == [], f"menus 清空后 /api/auth/menus 为空: {keys}")
        # 恢复一份业务配置供后续用例
        r = await c.put(f"/api/admin/users/{uid}/menus", headers=H,
                        json={"menus": ["catalog", "design", "messages", "oa"]})
        assert r.status_code == 200, r.text

        # ===== 2. 非法 key 400；非管理层 403；未登录 401；manager 改 admin 403 =====
        r = await c.put(f"/api/admin/users/{uid}/menus", headers=H,
                        json={"menus": ["catalog", "not-a-menu"]})
        chk(r.status_code == 400, f"非法菜单 key 400: {r.status_code} {r.text[:120]}")
        r = await c.put(f"/api/admin/users/{uid}/menus", headers=Hw1, json={"menus": ["catalog"]})
        chk(r.status_code == 403, f"非管理层调 PUT menus 403: {r.status_code}")
        r = await c.put(f"/api/admin/users/{uid}/menus", json={"menus": ["catalog"]})
        chk(r.status_code == 401, f"未登录 401: {r.status_code}")
        r = await c.put("/api/admin/users/99999/menus", headers=H, json={"menus": ["catalog"]})
        chk(r.status_code == 404, f"不存在用户 404: {r.status_code}")
        # manager 改 admin 账号 → 403（保护口径同 update_user）；admin 自己在列表隐身，直接查库拿 id
        Hmgr = await login("manager", "manager123")
        async with SessionLocal() as db:
            admin_id = (await db.execute(select(models.User.id).where(models.User.username == "admin"))).scalar_one()
        r = await c.put(f"/api/admin/users/{admin_id}/menus", headers=Hmgr, json={"menus": ["catalog"]})
        chk(r.status_code == 403, f"manager 改 admin 账号 menus 403: {r.status_code}")

        # ===== 3. backfill_user_menus：存量 NULL 用户回填 =====
        async with SessionLocal() as db:
            # 直接建行（menus=NULL 模拟存量）：sales 用户 + finance_lead 且带历史 grant_menus 的用户
            db.add(models.User(username="legacy_sales", password_hash="x", role_id=rid["sales"]))
            db.add(models.User(username="legacy_fin", password_hash="x", role_id=rid["finance_lead"],
                               grant_menus=["dict-admin", "dirty-key"]))
            await db.commit()
            res = await backfill_user_menus(db)
            chk(res.get("filled") == 2, f"backfill 回填 2 个 NULL 用户: {res}")
            ls = (await db.execute(select(models.User).where(models.User.username == "legacy_sales"))).scalar_one()
            chk(ls.menus == ["catalog", "sales", "leads", "oa", "messages"],
                f"backfill sales=模板+messages/oa: {ls.menus}")
            lf = (await db.execute(select(models.User).where(models.User.username == "legacy_fin"))).scalar_one()
            # finance_lead ⊇ finance（模板相同）∪ (grant_menus∩有效管理组) ∪ {messages,oa}；脏 key 丢弃
            chk(lf.menus == ["catalog", "list", "purchase_mgmt", "finance", "oa", "messages", "dict-admin"],
                f"backfill finance_lead+历史grant: {lf.menus}")
            # admin/manager 跳过不填（保持 NULL，运行时 bypass）
            adm = (await db.execute(select(models.User).where(models.User.username == "admin"))).scalar_one()
            chk(adm.menus is None, f"admin 不回填(保持 NULL): {adm.menus}")
            # 幂等：二次跑 filled=0，已配置的不动
            res2 = await backfill_user_menus(db)
            chk(res2.get("filled") == 0, f"backfill 幂等(二次 filled=0): {res2}")
        # backfill 后 admin 仍全量（bypass 不受 NULL 影响）
        keys = await menu_keys(H)
        chk("dict-admin" in keys and "sales" in keys, f"admin bypass 不受影响: {keys}")

        # ===== 4. grant-menus 兼容包装：只动管理组 key，业务 key 保留 =====
        # 当前 w1 menus = [catalog, design, oa, messages]（上面恢复的值）
        r = await c.put(f"/api/admin/users/{uid}/grant-menus", headers=H,
                        json={"grant_menus": ["dict-admin"]})
        chk(r.status_code == 200, f"包装 grant: {r.status_code} {r.text[:200]}")
        chk(r.json().get("menus") == ["catalog", "design", "oa", "messages", "dict-admin"],
            f"grant 后业务 key 不动、dict-admin 排尾: {r.json().get('menus')}")
        r = await c.put(f"/api/admin/users/{uid}/grant-menus", headers=H, json={"grant_menus": []})
        chk(r.status_code == 200, f"包装 revoke: {r.status_code} {r.text[:200]}")
        chk(r.json().get("menus") == ["catalog", "design", "oa", "messages"],
            f"revoke 后仅移除管理组 key: {r.json().get('menus')}")
        r = await c.put(f"/api/admin/users/{uid}/grant-menus", headers=H,
                        json={"grant_menus": ["not-a-menu"]})
        chk(r.status_code == 400, f"包装非法 key 400: {r.status_code}")

        # ===== 5. menu-defs 接口 =====
        r = await c.get("/api/admin/menu-defs", headers=H)
        chk(r.status_code == 200, f"menu-defs: {r.status_code}")
        biz = [m["key"] for m in r.json()["business"]]
        adm = [m["key"] for m in r.json()["admin"]]
        chk(biz[0] == "catalog" and "messages" in biz and "oa" in biz, f"menu-defs business: {biz}")
        chk("dict-admin" in adm and "admin-users" in adm, f"menu-defs admin: {adm}")
        chk(all(m.get("label") for m in r.json()["business"] + r.json()["admin"]), "menu-defs 带中文 label")
        r = await c.get("/api/admin/menu-defs", headers=Hw1)
        chk(r.status_code == 403, f"非管理层调 menu-defs 403: {r.status_code}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
