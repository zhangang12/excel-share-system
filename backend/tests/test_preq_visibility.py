"""🆕 反馈#283：采购申请可见权限跟着指定采购员走（王芹澄清：不是推送，是数据可见性）。
1. 指定了采购员的申请：指定人可见；其他普通采购员不可见（含 buyer+finance 多角色账号）；
2. 未指定的申请：全体采购可见；采购主管/管理层/纯财务不受限，全可见；
3. 操作守卫：非指定采购员 handle/reject 指定申请 → 403；指定人 → 200；
4. PDF 守卫：非指定采购员下载指定申请 PDF → 403。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="preqviz")
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
        async def login(u, p):
            r = await c.post('/api/auth/login', json={'username': u, 'password': p})
            assert r.status_code == 200, (u, r.text)
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login('admin', 'admin123')
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(name, *roles):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123", "full_name": name,
                "role_id": rid[roles[0]], "role_ids": [rid[x] for x in roles]})
            assert r.status_code == 200, (name, r.text)
            return await login(name, 'pass123'), r.json()["id"]

        Hwh, _ = await mkuser("wh1", "warehouse")
        Hb1, b1id = await mkuser("b1", "buyer")
        Hb2, _ = await mkuser("b2", "buyer")
        Hb3, _ = await mkuser("b3", "buyer", "finance")   # 王芹同款多角色
        Hlead, _ = await mkuser("lead1", "buyer_lead")
        Hfin, _ = await mkuser("fin1", "finance")
        Hmgr = await login('manager', 'manager123')

        async def mkreq(headers, buyer_id=None):
            body = {"buyer_id": buyer_id, "lines": [{"item_name": "测试物料", "qty": 1}]}
            r = await c.post("/api/purchase-mgmt/purchase-requests", headers=headers, json=body)
            assert r.status_code == 200, r.text
            return r.json()["id"]

        reqA = await mkreq(Hwh, b1id)   # 指定给 b1
        reqB = await mkreq(Hwh)         # 未指定

        async def visible_ids(headers):
            r = await c.get("/api/purchase-mgmt/purchase-requests", headers=headers)
            assert r.status_code == 200, r.text
            return {x["id"] for x in r.json()}

        # ===== 1. 列表可见性 =====
        v = await visible_ids(Hb1);   chk(reqA in v and reqB in v, f"指定人可见指定+未指定: {v}")
        v = await visible_ids(Hb2);   chk(reqA not in v and reqB in v, f"其他采购不见指定单: {v}")
        v = await visible_ids(Hb3);   chk(reqA not in v and reqB in v, f"buyer+finance多角色同样受限: {v}")
        v = await visible_ids(Hlead); chk(reqA in v and reqB in v, f"采购主管全可见: {v}")
        v = await visible_ids(Hfin);  chk(reqA in v and reqB in v, f"纯财务不受限: {v}")
        v = await visible_ids(Hmgr);  chk(reqA in v and reqB in v, f"管理层全可见: {v}")

        # ===== 2. handle 守卫 =====
        r = await c.put(f"/api/purchase-mgmt/purchase-requests/{reqA}/handle", headers=Hb2)
        chk(r.status_code == 403, f"非指定人处理指定单→403: {r.status_code}")
        r = await c.put(f"/api/purchase-mgmt/purchase-requests/{reqA}/handle", headers=Hb1)
        chk(r.status_code == 200, f"指定人处理→200: {r.status_code}")

        # ===== 3. reject 守卫 =====
        reqC = await mkreq(Hwh, b1id)
        r = await c.put(f"/api/purchase-mgmt/purchase-requests/{reqC}/reject", headers=Hb2, json={"reason": "x"})
        chk(r.status_code == 403, f"非指定人驳回→403: {r.status_code}")
        r = await c.put(f"/api/purchase-mgmt/purchase-requests/{reqC}/reject", headers=Hlead, json={"reason": "不要了"})
        chk(r.status_code == 200, f"主管驳回→200: {r.status_code}")

        # ===== 4. PDF 守卫 =====
        r = await c.get(f"/api/purchase-mgmt/purchase-requests/{reqA}/pdf", headers=Hb2)
        chk(r.status_code == 403, f"非指定人下PDF→403: {r.status_code}")
        r = await c.get(f"/api/purchase-mgmt/purchase-requests/{reqA}/pdf", headers=Hb1)
        chk(r.status_code == 200, f"指定人下PDF→200: {r.status_code}")

    await engine.dispose()
    if FAIL:
        print(f"\n{len(FAIL)} 项失败"); sys.exit(1)
    print("PASSED")

asyncio.run(main())
