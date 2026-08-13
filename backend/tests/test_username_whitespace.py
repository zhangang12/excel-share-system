"""用户名首尾空白：建账号时统一 strip，登录侧也容错。

2026-08-13 线上真事：新建账号 `liulonglong` 时用户名尾部粘了个空格，
库里存的是 `'liulonglong '`（12 个字符）。登录走的是**精确匹配**
`User.username == 输入值`，本人按正常拼写输入永远匹配不上，
界面只报「用户名或密码错误」——密码重置多少次都没用，因为压根不是密码的问题。
排查也难：肉眼看列表跟正常的一模一样，得 `length(username)` 才看得出来。

所以两头都要堵：
  1. **建账号时 strip**（源头）——不让脏数据进库
  2. **登录时也 strip**（兜底）——存量脏数据修好之前，人至少能登进来；
     手机键盘长按空格、从表格复制粘贴带尾空格也一并容错
  3. **密码不能 strip** —— 空格可能是密码的一部分，strip 了等于偷偷改人家密码
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="unamews")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        # ---- 1) 建账号时用户名尾部带空格 → 入库前就被 strip ----
        r = await c.post("/api/admin/users", headers=H, json={
            "username": "liulonglong ", "password": "pass123",
            "full_name": "刘龙龙", "role_id": rid["buyer"]})
        chk(r.status_code == 200, f"用尾部带空格的用户名建账号: {r.status_code} {r.text[:90]}")
        uid = r.json()["id"]
        async with SessionLocal() as db:
            u = (await db.execute(select(models.User).where(
                models.User.id == uid))).scalar_one()
            chk(u.username == "liulonglong",
                f"1) 入库时已 strip（线上就是这里漏了，存成 12 个字符）: [{u.username}] 长度 {len(u.username)}")

        # ---- 2) 正常拼写能登进去（线上的症状就是这一步过不了）----
        r = await c.post("/api/auth/login", json={"username": "liulonglong", "password": "pass123"})
        chk(r.status_code == 200,
            f"2) 按正常拼写登录成功: {r.status_code} {r.text[:90]}")

        # ---- 3) 登录侧兜底：输入带空格也认（存量脏数据/手机键盘/复制粘贴）----
        for typed in ("liulonglong ", " liulonglong", "  liulonglong  ", "liulonglong　"):
            r = await c.post("/api/auth/login", json={"username": typed, "password": "pass123"})
            chk(r.status_code == 200,
                f"3) 输入 [{typed}] 也能登进来: {r.status_code}")

        # ---- 4) 密码**不能**被 strip（空格可能是密码的一部分）----
        r = await c.post("/api/admin/users", headers=H, json={
            "username": "spacepwd", "password": " pw123456 ",
            "full_name": "空格密码", "role_id": rid["buyer"]})
        chk(r.status_code == 200, f"建一个密码带空格的账号: {r.status_code} {r.text[:80]}")
        r = await c.post("/api/auth/login", json={"username": "spacepwd", "password": " pw123456 "})
        chk(r.status_code == 200, f"4) 原样带空格的密码能登: {r.status_code}")
        r = await c.post("/api/auth/login", json={"username": "spacepwd", "password": "pw123456"})
        chk(r.status_code == 401,
            f"4) **密码没有被偷偷 strip**（strip 了等于替人改密码）: {r.status_code}")

        # ---- 5) 重名判定要按 strip 后的算，否则能建出两个"同名"账号 ----
        r = await c.post("/api/admin/users", headers=H, json={
            "username": " liulonglong", "password": "pass123",
            "full_name": "冒牌", "role_id": rid["buyer"]})
        chk(r.status_code == 400 and "已存在" in r.text,
            f"5) 带空格的同名被判重名（否则库里会有两个 liulonglong，谁也说不清哪个是真的）: "
            f"{r.status_code} {r.text[:90]}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
