"""反馈#366（赵仁辉）/#380：已下发的待办要能编辑。

原来只有「撤销 + 重发」一条路。生产现场 2026-08-11：
  02:41:52 下发待办「买一个快装弯头」
  02:42:14 撤销            ← 22 秒后发现写错了
  02:43:45 重发「19的快装弯头  卡盘50.5」
代价：收件人被打扰两次，第一条上的回复/承诺完成时间/进度**全部丢掉**。

要锁死的：
  1. 能改标题/说明/紧急程度/截止日期
  2. **改内容不能碰收件人已有的回复和进度** —— 这是撤销重发最大的代价，
     也是这个接口存在的意义；做成"整批重建 targets"就等于没修
  3. 加收件人：只给**新加的人**推「请回复承诺完成时间」，
     老收件人推「有变更」——他们可能早回过了，再要一次是骚扰
  4. 删收件人：把人移出去
  5. 空标题要拒；日期格式要校验
  6. 只有管理层能改
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb366")
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
        uids = {}
        for uname, name in [("u1", "甲"), ("u2", "乙"), ("u3", "丙")]:
            rr = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "full_name": name,
                "role_id": rid["buyer"]})
            uids[name] = rr.json()["id"]

        # 下发给 甲、乙
        r = await c.post("/api/management-todos", headers=H, json={
            "title": "买一个快装弯头", "content": "尽快", "priority": "normal",
            "recipient_ids": [uids["甲"], uids["乙"]]})
        chk(r.status_code == 200, f"下发待办: {r.status_code} {r.text[:80]}")
        tid = r.json()["id"]

        # 甲先回复承诺完成时间（模拟已经动过的收件人）
        r1 = await c.post("/api/auth/login", json={"username": "u1", "password": "pass123"})
        H1 = {"Authorization": f"Bearer {r1.json()['access_token']}"}
        mine = (await c.get("/api/management-todos/mine", headers=H1)).json()
        target_id = mine[0]["target_id"]
        r = await c.post(f"/api/management-todos/{target_id}/reply", headers=H1,
                         json={"committed_at": "2026-08-20", "progress": "已联系供应商"})
        chk(r.status_code == 200, f"甲回复承诺完成时间: {r.status_code} {r.text[:90]}")

        async def target_of(uid):
            async with SessionLocal() as db:
                return (await db.execute(select(models.ManagementTodoTarget).where(
                    models.ManagementTodoTarget.todo_id == tid,
                    models.ManagementTodoTarget.user_id == uid))).scalars().first()

        t_before = await target_of(uids["甲"])
        chk(t_before is not None and t_before.committed_at == "2026-08-20",
            f"前提：甲的承诺日期已存下: {t_before.committed_at if t_before else None}")

        # ---- 1) 改标题/说明/紧急/截止 ----
        r = await c.put(f"/api/management-todos/{tid}", headers=H, json={
            "title": "19的快装弯头  卡盘50.5", "priority": "urgent", "due_date": "2026-08-25"})
        chk(r.status_code == 200, f"1) 改标题+紧急+截止: {r.status_code} {r.text[:90]}")
        j = r.json()
        chk(j["title"] == "19的快装弯头  卡盘50.5", f"1) 标题改掉了: {j['title']}")
        chk(j["priority"] == "urgent" and j["due_date"] == "2026-08-25",
            f"1) 紧急/截止也改了: {j['priority']} {j['due_date']}")

        # ---- 2) 甲的回复和承诺日期必须还在 ----
        t_after = await target_of(uids["甲"])
        chk(t_after is not None and t_after.committed_at == "2026-08-20",
            f"2) 改内容后，甲的承诺完成时间没丢: {t_after.committed_at if t_after else None}")
        chk(t_after.id == t_before.id,
            f"2) 还是同一行（没有整批重建 targets）: {t_before.id} → {t_after.id}")

        # ---- 3/4) 收件人增删 ----
        r = await c.put(f"/api/management-todos/{tid}", headers=H, json={
            "recipient_ids": [uids["甲"], uids["丙"]]})   # 乙移出、丙加入
        chk(r.status_code == 200, f"改收件人: {r.status_code}")
        chk(await target_of(uids["丙"]) is not None, "3) 丙被加进来了")
        chk(await target_of(uids["乙"]) is None, "4) 乙被移出去了")
        t_keep = await target_of(uids["甲"])
        chk(t_keep is not None and t_keep.committed_at == "2026-08-20",
            "2) 改收件人时，留下来的甲的承诺时间仍然没丢")

        async with SessionLocal() as db:
            msgs = [m.text for m in (await db.execute(select(models.Message).where(
                models.Message.biz_type == "mgmt_todo"))).scalars().all()]
        to_new = [m for m in msgs if "请尽快回复承诺完成时间" in m]
        changed_msgs = [m for m in msgs if "有变更" in m]
        chk(len(to_new) >= 3, f"3) 新加的丙收到「请回复承诺完成时间」: 共 {len(to_new)} 条")
        chk(len(changed_msgs) >= 1, f"3) 老收件人收到的是「有变更」而不是再要一次承诺: {len(changed_msgs)} 条")

        # ---- 5) 校验 ----
        r = await c.put(f"/api/management-todos/{tid}", headers=H, json={"title": "   "})
        chk(r.status_code == 400, f"5) 空标题被拒: {r.status_code}")
        r = await c.put(f"/api/management-todos/{tid}", headers=H, json={"due_date": "8月25"})
        chk(r.status_code == 400, f"5) 日期格式错被拒: {r.status_code}")

        # ---- 6) 非管理层不能改 ----
        r = await c.put(f"/api/management-todos/{tid}", headers=H1, json={"title": "我来改"})
        chk(r.status_code in (401, 403), f"6) 普通用户改不了: {r.status_code}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
