"""🆕 客户端设备限制（device_gate + device_ids）。

规格：桌面客户端按设备 ID 控制登录；带开关，**默认关**；设备 ID 由管理层手工录入
「外网访问」页；不在名单里的客户端要走验证码才能登录。

本测试锁四件事：
 1. 开关默认关 → 行为与加此功能前**完全一致**（装了客户端就免闸），存量环境零影响；
 2. 开关打开 → 名单内免闸、名单外走验证码，伪造 X-PMS-Client 头也拦得住；
 3. admin 恒免闸——名单填错时的救命通道；
 4. 脏数据（非 JSON / 非数组）当空名单处理，不因存储损坏产生意外放行或误锁。
"""
import asyncio, json, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="gatecidr")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.main import app
from app.database import engine, SessionLocal, Base
from app import models, gate
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)
    else: print("  ok:", m)

DESKTOP = {"X-PMS-Client": "desktop/1.0.27", "X-PMS-Device": "dev-test-1"}


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        # 发码限频是 1 条/分钟/账号，所以每个用例用独立账号，避免撞 429 掩盖真实结果
        users = [f"u{i}" for i in range(8)]
        for name in users:
            await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123",
                "full_name": f"员工{name}", "role_id": rid["designer"]})

        async def login(user, ip, desktop=False, dev="dev-AAA"):
            h = {"X-Real-IP": ip}
            if desktop: h.update({"X-PMS-Client": "desktop/1.0.28", "X-PMS-Device": dev})
            r = await c.post("/api/auth/login", headers=h,
                             json={"username": user, "password": "pass123"})
            return r.json()

        def gated(res):
            """是否被闸门拦下：正常返回 gate_required；撞发码限频(429)也算拦下了"""
            return bool(res.get("gate_required")) or "频繁" in str(res.get("detail", ""))

        async def set_cfg(**kw):
            body = {"enabled": True, "cidrs": [], "device_gate": False, "device_ids": []}
            body.update(kw)
            return await c.put("/api/admin/gate-config", headers=H, json=body)

        WAN = "45.13.99.9"      # 真公网 IP。注意别用 RFC 5737 文档段，见下方断言

        print("===== 1. 纯函数：desktop_exempt 的分支 =====")
        E = gate.desktop_exempt
        chk(E(True, "d1", device_gate=False, device_ids=[]) is True,
            "开关关 → 桌面端免闸（与加此功能前一致）")
        chk(E(True, "d1", device_gate=False, device_ids=["other"]) is True,
            "开关关时名单不生效，填了也不看")
        chk(E(True, "d1", device_gate=True, device_ids=["d1", "d2"]) is True, "开关开 + 在名单 → 免闸")
        chk(E(True, "d9", device_gate=True, device_ids=["d1", "d2"]) is False, "开关开 + 不在名单 → 不免闸")
        chk(E(True, "", device_gate=True, device_ids=["d1"]) is False, "没带设备 ID → 不免闸")
        chk(E(True, "d1", device_gate=True, device_ids=[]) is False,
            "开关开但名单空 → 谁都不免（字面语义，前端会二次确认）")
        chk(E(False, "d1", device_gate=False, device_ids=[]) is False, "浏览器永远不走这条免闸")
        chk(gate.is_intranet("192.168.31.23", []) is True, "192.168.x 仍恒按内网")
        chk(gate.is_intranet("203.0.113.9", []) is True,
            "RFC 5737 文档段被 Python 判为私网（别拿它当外网 IP 写用例）")

        print("\n===== 2. 开关默认关：存量行为零变化 =====")
        async with SessionLocal() as db:
            cfg = await gate.get_gate_config(db)
        chk(cfg["device_gate"] is False, f"未配置时 device_gate 默认关: {cfg['device_gate']}")
        chk(cfg["device_ids"] == [], f"名单默认空: {cfg['device_ids']}")
        res = await login(users[0], WAN, desktop=True, dev="never-registered")
        chk("access_token" in res, f"外网 + 没登记过的客户端 → 照样直接进: {list(res)}")
        res = await login(users[1], WAN)
        chk(gated(res), f"外网 + 浏览器 → 仍要验证码（不受影响）: {list(res)}")

        print("\n===== 3. 开关打开：只认名单 =====")
        await set_cfg(device_gate=True, device_ids=["dev-AAA", "dev-BBB"])
        res = await login(users[2], WAN, desktop=True, dev="dev-AAA")
        chk("access_token" in res, f"名单内设备 → 免闸: {list(res)}")
        res = await login(users[3], WAN, desktop=True, dev="dev-ZZZ")
        chk(gated(res), f"名单外设备 → 要验证码: {list(res)}")
        res = await login(users[4], WAN, desktop=True, dev="")
        chk(gated(res), f"不带设备 ID 的客户端 → 要验证码: {list(res)}")

        print("\n===== 4. 伪造 X-PMS-Client 头绕闸，现在拦得住 =====")
        r = await c.post("/api/auth/login",
                         headers={"X-Real-IP": WAN, "X-PMS-Client": "desktop/1.0.28"},
                         json={"username": users[5], "password": "pass123"})
        chk(gated(r.json()), f"只伪造头、没有在册设备 ID → 被拦: {list(r.json())}")

        print("\n===== 5. 救命通道：admin 恒免闸 =====")
        r = await c.post("/api/auth/login", headers={"X-Real-IP": WAN},
                         json={"username": "admin", "password": "admin123"})
        chk("access_token" in r.json(), f"名单填错时 admin 仍能进来改回: {r.status_code}")

        print("\n===== 6. 内网不受设备闸影响 =====")
        res = await login(users[6], "192.168.31.23", desktop=True, dev="dev-ZZZ")
        chk("access_token" in res, f"内网 + 名单外设备 → 仍免闸（走 is_intranet）: {list(res)}")

        print("\n===== 7. 配置读写往返 =====")
        r = await set_cfg(cidrs=["10.0.0.0/8"], device_gate=True,
                          device_ids=["  dev-1 ", "", "dev-2", "dev-1"])
        chk(r.status_code == 200, f"保存成功: {r.status_code}")
        chk(r.json()["device_ids"] == ["dev-1", "dev-2"],
            f"去空白 + 去重且保序: {r.json()['device_ids']}")
        got = (await c.get("/api/admin/gate-config", headers=H)).json()
        chk(got["device_gate"] is True and got["device_ids"] == ["dev-1", "dev-2"],
            f"重新读回一致: {got}")
        chk(got["cidrs"] == ["10.0.0.0/8"], "内网名单没被新字段冲掉")

        print("\n===== 8. 存储损坏不产生意外放行/误锁 =====")
        for bad in ("{不是JSON", '{"a":1}', "null"):
            async with SessionLocal() as db:
                row = await db.get(models.AppSetting, "device_ids")
                row.value = bad
                await db.commit()
            async with SessionLocal() as db:
                cfg = await gate.get_gate_config(db)
            chk(cfg["device_ids"] == [], f"脏数据 {bad!r} → 当空名单: {cfg['device_ids']}")
        # 开关仍是开的 + 名单被当成空 → 该拦就拦，不能因为读坏了就放行
        res = await login(users[7], WAN, desktop=True, dev="dev-1")
        chk(gated(res), "名单读坏时按空名单执行（拦住），不会误放行")

        print("\n===== 9. 旧格式请求体兼容 =====")
        r = await c.put("/api/admin/gate-config", headers=H,
                        json={"enabled": True, "cidrs": ["10.0.0.0/8"]})
        chk(r.status_code == 200, f"不带新字段仍接受: {r.status_code}")
        chk(r.json()["device_gate"] is False and r.json()["device_ids"] == [],
            f"缺省为关 + 空名单: device_gate={r.json()['device_gate']}")

    await engine.dispose()
    print("\nPASSED" if not FAIL else f"\n{len(FAIL)} FAILURES")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
