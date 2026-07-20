"""🆕 桌面客户端在线统计测试：
1. 带 X-PMS-Client: desktop/<version> + X-PMS-Device + X-PMS-User 头的请求 → upsert 落库；
2. 60 秒节流：同 device_id 60 秒内重复请求不重复写库（version/username/last_seen 均不变）；
3. 节流窗口过后（清空进程内节流 dict 模拟）→ 同设备 upsert 更新版本/用户名；
4. GET /api/admin/desktop-clients：distribution 按版本聚合计数、items 按 last_seen 倒序；
5. 权限：非 admin/manager 访问 403，未登录 401；无统计头/非 desktop 头不产生记录；
6. 管理组菜单下发包含 key=desktop。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="desktop")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from httpx import AsyncClient, ASGITransport
from app.main import app, _desktop_last_write
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
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'admin','password':'admin123'})).json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        r = await c.post("/api/admin/users", headers=H,
                         json={"username": "w1", "password": "pass123", "full_name": "w1", "role_id": rid["warehouse"]})
        assert r.status_code == 200, r.text
        Hw1 = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'w1','password':'pass123'})).json()['access_token']}"}

        async def rows():
            async with SessionLocal() as db:
                return list((await db.execute(select(models.DesktopClient))).scalars().all())

        # ===== 1. 带头请求 → 落库 =====
        DH1 = {"X-PMS-Client": "desktop/1.0.0", "X-PMS-Device": "dev-aaa", "X-PMS-User": "zhangsan"}
        r = await c.get("/api/health", headers=DH1)
        chk(r.status_code == 200, f"带头请求业务不受影响: {r.status_code}")
        rs = await rows()
        chk(len(rs) == 1, f"落库1条: {len(rs)}")
        if rs:
            chk(rs[0].device_id == "dev-aaa" and rs[0].version == "1.0.0"
                and rs[0].username == "zhangsan" and rs[0].last_seen is not None,
                f"字段正确: {rs[0].device_id}/{rs[0].version}/{rs[0].username}/{rs[0].last_seen}")
            first_seen = rs[0].last_seen

        # ===== 2. 60 秒内重复 → 不重复写 =====
        r = await c.get("/api/health", headers={"X-PMS-Client": "desktop/9.9.9",
                                                "X-PMS-Device": "dev-aaa", "X-PMS-User": "lisi"})
        chk(r.status_code == 200, f"节流窗口内请求业务不受影响: {r.status_code}")
        rs = await rows()
        chk(len(rs) == 1, f"节流窗口内不新增行: {len(rs)}")
        if rs:
            chk(rs[0].version == "1.0.0" and rs[0].username == "zhangsan"
                and rs[0].last_seen == first_seen,
                f"节流窗口内不更新 version/username/last_seen: {rs[0].version}/{rs[0].username}")

        # ===== 3. 节流窗口过后 → 同设备 upsert 更新 =====
        _desktop_last_write.clear()  # 模拟 60 秒节流窗口已过
        r = await c.get("/api/health", headers={"X-PMS-Client": "desktop/1.0.1",
                                                "X-PMS-Device": "dev-aaa", "X-PMS-User": "lisi"})
        chk(r.status_code == 200, f"窗口过后请求业务不受影响: {r.status_code}")
        rs = await rows()
        chk(len(rs) == 1, f"同设备仍只1行(upsert): {len(rs)}")
        if rs:
            chk(rs[0].version == "1.0.1" and rs[0].username == "lisi",
                f"窗口过后更新 version/username: {rs[0].version}/{rs[0].username}")

        # 第二个设备（不同版本）+ 无 X-PMS-User 头
        r = await c.get("/api/health", headers={"X-PMS-Client": "desktop/1.0.0", "X-PMS-Device": "dev-bbb"})
        chk(r.status_code == 200, f"第二台设备请求: {r.status_code}")
        rs = await rows()
        chk(len(rs) == 2, f"两台设备两行: {len(rs)}")

        # ===== 4. 无统计头 / 非 desktop 头 → 不产生记录 =====
        await c.get("/api/health")
        await c.get("/api/health", headers={"X-PMS-Client": "web/2.0", "X-PMS-Device": "dev-web"})
        await c.get("/api/health", headers={"X-PMS-Client": "desktop/1.0.0"})  # 缺 device_id
        rs = await rows()
        chk(len(rs) == 2, f"无头/非desktop头/缺device头均不落库: {len(rs)}")

        # ===== 5. 接口返回分布正确 =====
        r = await c.get("/api/admin/desktop-clients", headers=H)
        chk(r.status_code == 200, f"admin 查询接口: {r.status_code} {r.text[:150]}")
        data = r.json()
        dist = {d["version"]: d["count"] for d in data["distribution"]}
        chk(dist == {"1.0.1": 1, "1.0.0": 1}, f"distribution 按版本聚合: {data['distribution']}")
        chk(len(data["items"]) == 2, f"items 两台设备: {len(data['items'])}")
        if len(data["items"]) == 2:
            it0 = data["items"][0]
            chk(set(it0.keys()) == {"device_id", "version", "username", "last_seen"},
                f"items 字段契约: {sorted(it0.keys())}")
            chk(it0["device_id"] == "dev-bbb", f"items 按 last_seen 倒序(最新在前): {it0['device_id']}")
            unames = {i["device_id"]: i["username"] for i in data["items"]}
            chk(unames.get("dev-aaa") == "lisi" and unames.get("dev-bbb") is None,
                f"username 正确(无头设备为 None): {unames}")

        # ===== 6. 权限：manager 可查；普通角色 403；未登录 401 =====
        r = await c.post("/api/admin/users", headers=H,
                         json={"username": "m1", "password": "pass123", "full_name": "m1", "role_id": rid["manager"]})
        assert r.status_code == 200, r.text
        Hm1 = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'m1','password':'pass123'})).json()['access_token']}"}
        r = await c.get("/api/admin/desktop-clients", headers=Hm1)
        chk(r.status_code == 200, f"manager 可查: {r.status_code}")
        r = await c.get("/api/admin/desktop-clients", headers=Hw1)
        chk(r.status_code == 403, f"普通角色(warehouse) 403: {r.status_code}")
        r = await c.get("/api/admin/desktop-clients")
        chk(r.status_code == 401, f"未登录 401: {r.status_code}")

        # ===== 7. 管理组菜单包含 desktop =====
        r = await c.get("/api/auth/menus", headers=H)
        keys = [m["key"] for m in r.json()["menus"]]
        chk("desktop" in keys, f"管理组菜单含 desktop: {keys}")
        r = await c.get("/api/auth/menus", headers=Hw1)
        chk("desktop" not in [m["key"] for m in r.json()["menus"]], "普通角色菜单不含 desktop")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
