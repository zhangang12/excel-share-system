"""🆕 外网登录两步闸门测试：
1. is_intranet：CIDR 网段 / 单 IP(按/32) / 非法条目跳过 / 空名单；
2. 内网 IP 登录 → 直接发 token（免闸）；
3. 外网浏览器登录 → gate_required + 无 token + manager 收到含 6 位码的站内消息（库中只存哈希）；
4. 正确码 → 发 token；错误码 → 400 且 fail_count+1；连续错 5 次 → 429；过期 → 400；
5. 外网请求带 X-PMS-Client: desktop/... 头 → 直接发 token（客户端免闸）；
6. admin 外网登录 → 直接发 token（admin 除外）；
7. gate_enabled=0 → 直接发 token；
8. 1 分钟内重复发码 → 429（限频）。
"""
import asyncio, hashlib, os, re, shutil, sys, tempfile
from datetime import datetime, timedelta, timezone

tmp = tempfile.mkdtemp(prefix="logingate")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select, update
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models
from app.gate import is_intranet

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)

EXT = {"X-Forwarded-For": "8.8.8.8"}            # 模拟外网来源
LAN = {"X-Forwarded-For": "192.168.1.20"}       # 模拟内网来源
DESKTOP = {**EXT, "X-PMS-Client": "desktop/1.0.0"}  # 外网 + 桌面客户端统计头


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # ===== 0. is_intranet 纯函数 =====
        chk(is_intranet("192.168.1.5", ["192.168.0.0/16"]), "CIDR 网段命中")
        chk(not is_intranet("8.8.8.8", ["192.168.0.0/16"]), "网段外不命中")
        chk(is_intranet("127.0.0.1", ["127.0.0.1"]), "单 IP 按 /32 命中")
        chk(not is_intranet("8.8.8.9", ["8.8.8.8"]), "单 IP 不误伤邻居")
        chk(is_intranet("8.8.8.8", ["bad-cidr", "", "8.8.8.0/24"]), "非法条目跳过后续仍匹配")
        chk(not is_intranet("8.8.8.8", []) and not is_intranet("8.8.8.8", None), "空名单公网 IP 不命中")
        chk(not is_intranet("not-an-ip", ["0.0.0.0/0"]), "非法 IP 不命中")
        # 回环/私网地址恒为内网（本机/开发/测试来源，空名单也免闸）
        chk(is_intranet("127.0.0.1", []) and is_intranet("::1", []), "回环地址恒内网")
        chk(is_intranet("10.1.2.3", []) and is_intranet("172.16.5.5", [])
            and is_intranet("192.168.9.9", []), "私网地址恒内网")

        # ===== 配置：启用闸门，内网=192.168/16 与 10/8 =====
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'admin','password':'admin123'})).json()['access_token']}"}
        r = await c.put("/api/admin/gate-config", headers=H,
                        json={"enabled": True, "cidrs": ["192.168.0.0/16", "10.0.0.0/8"]})
        chk(r.status_code == 200 and r.json()["enabled"] is True
            and r.json()["cidrs"] == ["192.168.0.0/16", "10.0.0.0/8"],
            f"PUT gate-config: {r.status_code} {r.text[:120]}")
        r = await c.get("/api/admin/gate-config", headers=H)
        chk(r.status_code == 200 and r.json()["enabled"] is True, f"GET gate-config: {r.status_code}")

        # ===== 造测试账号：w1(仓库)/w2(销售)/w3(物流)，均非 admin =====
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        for uname, role in (("w1", "warehouse"), ("w2", "sales"), ("w3", "logistics")):
            r = await c.post("/api/admin/users", headers=H,
                             json={"username": uname, "password": "pass123",
                                   "full_name": f"测试{uname}", "role_id": rid[role]})
            assert r.status_code == 200, r.text

        async def gate_rows(uname):
            async with SessionLocal() as db:
                uid = (await db.execute(select(models.User.id).where(
                    models.User.username == uname))).scalar_one()
                rows = list((await db.execute(select(models.LoginGateCode).where(
                    models.LoginGateCode.user_id == uid))).scalars().all())
                return uid, rows

        async def last_manager_msg():
            async with SessionLocal() as db:
                mid = (await db.execute(select(models.User.id).where(
                    models.User.username == "manager"))).scalar_one()
                rows = list((await db.execute(
                    select(models.Message).where(models.Message.to_user_id == mid)
                    .order_by(models.Message.id.desc()).limit(1))).scalars().all())
                return rows[0] if rows else None

        # ===== 1. 内网 IP 登录 → 直接发 token（免闸）=====
        r = await c.post("/api/auth/login", json={"username": "w1", "password": "pass123"}, headers=LAN)
        j = r.json()
        chk(r.status_code == 200 and j.get("access_token") and not j.get("gate_required"),
            f"内网 IP 直接发 token: {r.status_code} {list(j)}")
        _, rows = await gate_rows("w1")
        chk(len(rows) == 0, f"内网登录不发码: {len(rows)}")

        # ===== 2. 外网浏览器登录 → gate_required + 无 token + 管理层收到码 =====
        r = await c.post("/api/auth/login", json={"username": "w1", "password": "pass123"}, headers=EXT)
        j = r.json()
        chk(r.status_code == 200 and j.get("gate_required") is True and j.get("pre_token")
            and "access_token" not in j,
            f"外网浏览器 gate_required 且无 token: {r.status_code} {list(j)}")
        pre1 = j.get("pre_token", "")
        msg = await last_manager_msg()
        chk(msg is not None and msg.kind == "warn" and "【外网登录验证】" in msg.text
            and "测试w1(w1)" in msg.text,
            f"manager 收到发码消息: {msg.text[:80] if msg else None}")
        m = re.search(r"验证码：(\d{6})", msg.text if msg else "")
        code1 = m.group(1) if m else ""
        chk(len(code1) == 6, f"消息含 6 位码: {code1!r}")
        _, rows = await gate_rows("w1")
        chk(len(rows) == 1 and rows[0].pre_token == pre1 and not rows[0].used,
            f"码行落库: {len(rows)}")
        if rows:
            chk(rows[0].code_hash == hashlib.sha256(code1.encode()).hexdigest()
                and rows[0].code_hash != code1,
                "库中只存 sha256 哈希，不存明文")

        # ===== 3. 错误码 → 400 且 fail_count+1；正确码 → token =====
        wrong1 = "000000" if code1 != "000000" else "000001"
        r = await c.post("/api/auth/login/verify-gate", headers=EXT,
                         json={"username": "w1", "pre_token": pre1, "code": wrong1})
        chk(r.status_code == 400 and r.json()["detail"] == "验证码错误",
            f"错误码 400: {r.status_code} {r.text[:80]}")
        _, rows = await gate_rows("w1")
        chk(rows and rows[0].fail_count == 1 and not rows[0].used,
            f"fail_count+1: {rows[0].fail_count if rows else None}")
        r = await c.post("/api/auth/login/verify-gate", headers=EXT,
                         json={"username": "w1", "pre_token": pre1, "code": code1})
        j = r.json()
        chk(r.status_code == 200 and j.get("access_token"),
            f"正确码发 token: {r.status_code}")
        _, rows = await gate_rows("w1")
        chk(rows and rows[0].used, "验码成功后码标记已用")
        # 已用的码再验 → 400
        r = await c.post("/api/auth/login/verify-gate", headers=EXT,
                         json={"username": "w1", "pre_token": pre1, "code": code1})
        chk(r.status_code == 400, f"已用的码再验 400: {r.status_code}")

        # ===== 4. 连续错 5 次 → 429；过期 → 400（用 w2 避免限频串扰）=====
        r = await c.post("/api/auth/login", json={"username": "w2", "password": "pass123"}, headers=EXT)
        pre2 = r.json().get("pre_token", "")
        msg = await last_manager_msg()
        m = re.search(r"验证码：(\d{6})", msg.text if msg else "")
        code2 = m.group(1) if m else ""
        wrong2 = "111111" if code2 != "111111" else "222222"
        for i in range(5):
            r = await c.post("/api/auth/login/verify-gate", headers=EXT,
                             json={"username": "w2", "pre_token": pre2, "code": wrong2})
            chk(r.status_code == 400, f"第{i+1}次错码 400: {r.status_code}")
        _, rows = await gate_rows("w2")
        chk(rows and rows[0].fail_count == 5, f"错 5 次 fail_count=5: {rows[0].fail_count if rows else None}")
        r = await c.post("/api/auth/login/verify-gate", headers=EXT,
                         json={"username": "w2", "pre_token": pre2, "code": code2})
        chk(r.status_code == 429, f"错 5 次后锁定 429: {r.status_code} {r.text[:80]}")
        # 过期 → 400（直接把码行 expires_at 拨到过去）
        async with SessionLocal() as db:
            await db.execute(update(models.LoginGateCode).where(
                models.LoginGateCode.pre_token == pre2).values(
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=1), fail_count=0))
            await db.commit()
        r = await c.post("/api/auth/login/verify-gate", headers=EXT,
                         json={"username": "w2", "pre_token": pre2, "code": code2})
        chk(r.status_code == 400 and "已过期" in r.json()["detail"],
            f"过期码 400: {r.status_code} {r.text[:80]}")

        # ===== 5. 外网 + X-PMS-Client: desktop/... 头 → 直接发 token（客户端免闸）=====
        r = await c.post("/api/auth/login", json={"username": "w1", "password": "pass123"}, headers=DESKTOP)
        j = r.json()
        chk(r.status_code == 200 and j.get("access_token") and not j.get("gate_required"),
            f"桌面客户端免闸: {r.status_code} {list(j)}")

        # ===== 6. admin 外网登录 → 直接发 token（admin 除外）=====
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"}, headers=EXT)
        j = r.json()
        chk(r.status_code == 200 and j.get("access_token") and not j.get("gate_required"),
            f"admin 免闸: {r.status_code} {list(j)}")

        # ===== 7. gate_enabled=0 → 直接发 token =====
        r = await c.put("/api/admin/gate-config", headers=H,
                        json={"enabled": False, "cidrs": ["192.168.0.0/16"]})
        chk(r.status_code == 200 and r.json()["enabled"] is False, f"关闭闸门: {r.status_code}")
        r = await c.post("/api/auth/login", json={"username": "w1", "password": "pass123"}, headers=EXT)
        j = r.json()
        chk(r.status_code == 200 and j.get("access_token") and not j.get("gate_required"),
            f"gate_enabled=0 免闸: {r.status_code} {list(j)}")
        # 恢复启用（后续用例 + 不影响其他测试）
        await c.put("/api/admin/gate-config", headers=H,
                    json={"enabled": True, "cidrs": ["192.168.0.0/16", "10.0.0.0/8"]})

        # ===== 8. 1 分钟内重复发码 → 429（限频；用 w3 全新账号）=====
        r = await c.post("/api/auth/login", json={"username": "w3", "password": "pass123"}, headers=EXT)
        chk(r.status_code == 200 and r.json().get("gate_required") is True,
            f"w3 首次发码: {r.status_code}")
        r = await c.post("/api/auth/login", json={"username": "w3", "password": "pass123"}, headers=EXT)
        chk(r.status_code == 429, f"1 分钟内重复发码 429: {r.status_code} {r.text[:80]}")

        # ===== 9. 权限：普通角色访问 gate-config 403，未登录 401 =====
        Hw1 = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'w1','password':'pass123'}, headers=LAN)).json()['access_token']}"}
        r = await c.get("/api/admin/gate-config", headers=Hw1)
        chk(r.status_code == 403, f"普通角色 GET gate-config 403: {r.status_code}")
        r = await c.get("/api/admin/gate-config")
        chk(r.status_code == 401, f"未登录 GET gate-config 401: {r.status_code}")

        # ===== 10. 管理组菜单包含 gate-config =====
        r = await c.get("/api/auth/menus", headers=H)
        chk("gate-config" in [m["key"] for m in r.json()["menus"]], "管理组菜单含 gate-config")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
