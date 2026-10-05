"""🆕 2026-10-05 外协采购交接（方步森 → 李昌奇）：
1. /api/auth/me 下发 buyer_sheets（= dept_config.BUYER_SHEET_MAP），前端据此决定采购部各清单列的可见性；
   不在分工表里的采购员 buyer_sheets 为 None（看全部）。
2. lichangqi 进了分工表的外协域：跨项目搜 standard 被拒 403，搜 outsource 放行——和方步森同口径。
3. 方步森账号不动：仍是外协域。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="buyersheets")
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

        async def mk(u, rc):
            r = await c.post("/api/admin/users", headers=H,
                             json={"username": u, "password": "pass123", "full_name": u, "role_id": rid[rc]})
            assert r.status_code == 200, r.text
        async def login(u):
            r = await c.post('/api/auth/login', json={'username': u, 'password': 'pass123'})
            return r.json(), {"Authorization": f"Bearer {r.json()['access_token']}"}

        for u in ("fangbusen", "lichangqi", "lixinxin", "b1"):
            await mk(u, "buyer")

        # ===== 1. 登录返回和 /me 都带 buyer_sheets =====
        expect = {"fangbusen": ["outsource"], "lichangqi": ["outsource"],
                  "lixinxin": ["elec_po", "standard"], "b1": None}
        heads = {}
        for u, want in expect.items():
            body, h = await login(u)
            heads[u] = h
            chk(body["user"].get("buyer_sheets") == want, f"{u} 登录返回 buyer_sheets={body['user'].get('buyer_sheets')} 期望 {want}")
            me = (await c.get("/api/auth/me", headers=h)).json()
            chk(me.get("buyer_sheets") == want, f"{u} /me buyer_sheets={me.get('buyer_sheets')} 期望 {want}")
        me = (await c.get("/api/auth/me", headers=H)).json()
        chk(me.get("buyer_sheets") is None, f"admin 不分工: {me.get('buyer_sheets')}")

        # ===== 2. 李昌奇和方步森同口径：只能搜外协 =====
        for u in ("lichangqi", "fangbusen"):
            r = await c.get("/api/purchase-mgmt/purchasable-cross", headers=heads[u], params={"sheet": "standard"})
            chk(r.status_code == 403, f"{u} 搜标准件应 403: {r.status_code}")
            r = await c.get("/api/purchase-mgmt/purchasable-cross", headers=heads[u], params={"sheet": "outsource"})
            chk(r.status_code == 200, f"{u} 搜外协应 200: {r.status_code} {r.text[:120]}")
        r = await c.get("/api/purchase-mgmt/purchasable-cross", headers=heads["b1"], params={"sheet": "standard"})
        chk(r.status_code == 200, f"不在分工表的采购员不限: {r.status_code}")

    print("\n" + ("全部通过" if not FAIL else f"失败 {len(FAIL)} 项"))
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
