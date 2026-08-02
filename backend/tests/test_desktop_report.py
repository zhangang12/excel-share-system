"""🆕 桌面客户端故障自动上报。

背景：old-uninstaller 崩溃让部分机器永远升不了级，排查时手里什么都没有——
没有崩溃转储、没有日志、无法复现，只能去读 electron-builder 的 NSIS 模板反推。

本测试锁三件事：
 1. 上报入口 **不需要认证**（升级失败发生在登录之前，挂鉴权就永远收不到）；
 2. 防滥用三道（kind 白名单 / 64KB 截断 / 每设备每天 20 条）真的生效；
 3. 查询接口仍是 admin/manager 专属，普通用户看不到别人机器的日志。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="dtrep")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.main import app
from app.database import engine, SessionLocal, Base
from app import models
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)
    else: print("  ok:", m)


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
        await c.post("/api/admin/users", headers=H, json={
            "username": "puser", "password": "pass123", "full_name": "普通员工",
            "role_id": rid["designer"]})
        Hp = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'puser','password':'pass123'})).json()['access_token']}"}

        DEV = "dev-aaaa-1111"

        print("===== 1. 不带任何 token 也能上报（核心设计）=====")
        r = await c.post("/api/desktop/report", json={
            "device_id": DEV, "version": "1.0.26", "kind": "update_failed",
            "detail": "[updater] 下载完成 1.0.27\n[update-failed] 重启后仍是 1.0.26",
            "extra": {"target_version": "1.0.27", "current_version": "1.0.26"}})
        chk(r.status_code == 200, f"未认证上报被接受: {r.status_code} {r.text[:100]}")

        async with SessionLocal() as db:
            rows = (await db.execute(select(models.DesktopReport))).scalars().all()
            chk(len(rows) == 1, f"落库 1 条: {len(rows)}")
            chk(rows[0].kind == "update_failed" and rows[0].extra.get("target_version") == "1.0.27",
                f"kind/extra 正确: {rows[0].kind} {rows[0].extra}")
            chk(rows[0].handled is False, "默认未处理")

        print("\n===== 2. 防滥用 =====")
        r = await c.post("/api/desktop/report", json={
            "device_id": DEV, "version": "1.0.26", "kind": "'; DROP TABLE x; --"})
        chk(r.status_code == 400, f"未知 kind 被拒: {r.status_code}")

        big = "x" * (200 * 1024)
        r = await c.post("/api/desktop/report", json={
            "device_id": "dev-big", "version": "1.0.26", "kind": "crash", "detail": big})
        chk(r.status_code == 200, "超大正文仍接受（截断而非报错）")
        async with SessionLocal() as db:
            row = (await db.execute(select(models.DesktopReport)
                   .where(models.DesktopReport.device_id == "dev-big"))).scalar_one()
            chk(len(row.detail) == 64 * 1024, f"正文截断到 64KB: {len(row.detail)}")

        # 限流：同一设备刷到 20 条为止，之后静默丢弃（返回 200，不暴露阈值）
        for i in range(30):
            await c.post("/api/desktop/report", json={
                "device_id": "dev-flood", "version": "1.0.26", "kind": "crash",
                "detail": f"flood {i}"})
        async with SessionLocal() as db:
            n = len((await db.execute(select(models.DesktopReport)
                     .where(models.DesktopReport.device_id == "dev-flood"))).scalars().all())
            chk(n == 20, f"每设备每天封顶 20 条: 实际 {n}")
        r = await c.post("/api/desktop/report", json={
            "device_id": "dev-flood", "version": "1.0.26", "kind": "crash", "detail": "again"})
        chk(r.status_code == 200, "超限返回 200 而非 429（不给探测者反馈阈值）")

        print("\n===== 3. 查询侧仍是 admin/manager 专属 =====")
        r = await c.get("/api/admin/desktop-reports", headers=Hp)
        chk(r.status_code == 403, f"普通用户读不到别人机器的日志: {r.status_code}")

        r = await c.get("/api/admin/desktop-reports", headers=H)
        chk(r.status_code == 200, f"admin 可读: {r.status_code}")
        data = r.json()
        chk(data["open_count"] == 22, f"未处理计数 = 1+1+20 = 22: {data['open_count']}")
        chk(any(x["kind"] == "update_failed" for x in data["items"]), "列表含升级失败记录")

        r = await c.get("/api/admin/desktop-reports", headers=H, params={"kind": "update_failed"})
        chk(len(r.json()["items"]) == 1, f"按 kind 过滤: {len(r.json()['items'])}")

        print("\n===== 4. 标记已处理 =====")
        target = data["items"][-1]["id"]
        r = await c.post(f"/api/admin/desktop-reports/{target}/handled", headers=H)
        chk(r.status_code == 200, f"标记成功: {r.status_code}")
        r = await c.get("/api/admin/desktop-reports", headers=H, params={"only_open": True})
        chk(r.json()["open_count"] == 21, f"未处理减 1: {r.json()['open_count']}")
        chk(all(not x["handled"] for x in r.json()["items"]), "only_open 只返回未处理的")
        r = await c.post("/api/admin/desktop-reports/999999/handled", headers=H)
        chk(r.status_code == 404, f"不存在的记录 404: {r.status_code}")

        print("\n===== 5. 用户名由 device_id 反查台账（上报时可能没登录）=====")
        async with SessionLocal() as db:
            from datetime import datetime, timezone
            db.add(models.DesktopClient(device_id="dev-known", version="1.0.26",
                                        username="lixinxin",
                                        last_seen=datetime.now(timezone.utc)))
            await db.commit()
        await c.post("/api/desktop/report", json={
            "device_id": "dev-known", "version": "1.0.26", "kind": "crash", "detail": "boom"})
        async with SessionLocal() as db:
            row = (await db.execute(select(models.DesktopReport)
                   .where(models.DesktopReport.device_id == "dev-known"))).scalar_one()
            chk(row.username == "lixinxin", f"带出用户名: {row.username}")
        # 台账里没有的设备也能上报（首次启动就崩的情况）
        r = await c.post("/api/desktop/report", json={
            "device_id": "dev-never-seen", "version": "1.0.26", "kind": "crash", "detail": "boom"})
        chk(r.status_code == 200, "台账里没有的新设备也能上报（首启就崩）")

    await engine.dispose()
    print("\nPASSED" if not FAIL else f"\n{len(FAIL)} FAILURES")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
