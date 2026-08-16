"""反馈#363（赵仁辉）/#381/#382 个人待办。

「给我增加个人待办 单独开一列」。业务 2026-08-12 确认：要挂项目、要紧急档、
到期当天推一次企微、右下角角标 = 管理层待办未回复 + 个人待办未完成（合成一个数）。

这个文件锁死的：
  A. **越权**：每个按 id 的接口都得带 user_id 过滤。只滤列表、详情/改/删按 id 直取，
     是最典型的口子——换个 id 就能改别人的待办。这是本文件最重要的一组断言。
  B. 录入成本：只有 title 必填（个人待办的成败全在这）
  C. 与管理层待办**互不干扰**：两张表、两套接口，删个人待办不影响管理层待办的留痕
  D. 到期当天推一次企微，且**只推一次**——不做逾期后天天推（自己设的闹钟天天响，
     人就把整个系统的通知静音了，采购到货提醒 2026-07-26 就栽在这上面）
  E. PUT /reorder 必须排在 PUT /{tid} 之前，否则 "reorder" 被当成 tid 解析成 422
"""
import asyncio, os, sys, tempfile
from datetime import date, timedelta

tmp = tempfile.mkdtemp(prefix="pstodo")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from unittest.mock import patch
from datetime import datetime as _dt
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models, overdue

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
        uid = {}
        for uname, name in [("u1", "甲"), ("u2", "乙")]:
            rr = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "full_name": name,
                "role_id": rid["buyer"]})
            chk(rr.status_code == 200, f"建用户 {name}: {rr.status_code} {rr.text[:70]}")
            uid[name] = rr.json()["id"]
        H1 = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': 'u1', 'password': 'pass123'})).json()['access_token']}"}
        H2 = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': 'u2', 'password': 'pass123'})).json()['access_token']}"}
        pj = (await c.post("/api/projects", headers=H, json={"code": "P-900", "name": "测试项目"})).json()

        # ---------- B. 只要 title ----------
        r = await c.post("/api/personal-todos", headers=H1, json={"title": "给张工回电话"})
        chk(r.status_code == 200, f"B) 只填内容就能建: {r.status_code} {r.text[:90]}")
        t1 = r.json()["id"]
        chk(r.json()["priority"] == "normal" and not r.json()["due_date"] and not r.json()["done"],
            f"B) 默认：普通、无期限、未完成: {r.json()}")
        r = await c.post("/api/personal-todos", headers=H1, json={"title": "   "})
        chk(r.status_code == 400, f"B) 空内容被拒: {r.status_code}")

        # 建完再补日期/项目/紧急
        r = await c.put(f"/api/personal-todos/{t1}", headers=H1, json={
            "due_date": "2026-08-20", "priority": "urgent", "project_id": pj["id"], "note": "催一下"})
        chk(r.status_code == 200 and r.json()["priority"] == "urgent"
            and r.json()["project_code"] == "P-900",
            f"B) 建完能补日期/紧急/项目: {r.text[:130]}")
        r = await c.put(f"/api/personal-todos/{t1}", headers=H1, json={"due_date": "8月20"})
        chk(r.status_code == 400, f"B) 日期格式错被拒: {r.status_code}")
        # 显式传 null 摘掉项目
        r = await c.put(f"/api/personal-todos/{t1}", headers=H1, json={"project_id": None})
        chk(r.status_code == 200 and r.json()["project_id"] is None, "B) 传 null 能摘掉项目")

        # ---------- A. 越权（本文件最重要的一组） ----------
        r = await c.get("/api/personal-todos", headers=H2)
        chk(r.status_code == 200 and len(r.json()) == 0,
            f"A) 乙看不到甲的待办: {r.json()}")
        r = await c.put(f"/api/personal-todos/{t1}", headers=H2, json={"title": "我来改"})
        chk(r.status_code == 404, f"A) 乙改不了甲的待办（按 id 直取也得挡住）: {r.status_code}")
        r = await c.post(f"/api/personal-todos/{t1}/toggle", headers=H2)
        chk(r.status_code == 404, f"A) 乙打不了甲的勾: {r.status_code}")
        r = await c.delete(f"/api/personal-todos/{t1}", headers=H2)
        chk(r.status_code == 404, f"A) 乙删不了甲的待办: {r.status_code}")
        async with SessionLocal() as db:
            still = (await db.execute(select(models.PersonalTodo).where(
                models.PersonalTodo.id == t1))).scalar_one_or_none()
            chk(still is not None and still.user_id == uid["甲"],
                "A) 上面几次越权之后，甲的待办原封不动")
        # admin 也不例外：个人待办就是私人的
        r = await c.put(f"/api/personal-todos/{t1}", headers=H, json={"title": "管理员来改"})
        chk(r.status_code == 404, f"A) 连 admin 也看不到/改不了别人的个人待办: {r.status_code}")

        # ---------- 打勾 / 取消 / 删除 ----------
        r = await c.post(f"/api/personal-todos/{t1}/toggle", headers=H1)
        chk(r.status_code == 200 and r.json()["done"] is True and r.json()["done_at"],
            f"打勾: {r.text[:90]}")
        r = await c.post(f"/api/personal-todos/{t1}/toggle", headers=H1)
        chk(r.status_code == 200 and r.json()["done"] is False and not r.json()["done_at"],
            "再点一下取消打勾，完成时间清掉")
        cnt = (await c.get("/api/personal-todos/count", headers=H1)).json()["count"]
        chk(cnt == 1, f"角标数=未完成条数: {cnt}")

        # ---------- E. reorder 路由顺序 ----------
        t2 = (await c.post("/api/personal-todos", headers=H1, json={"title": "第二件"})).json()["id"]
        t3 = (await c.post("/api/personal-todos", headers=H2, json={"title": "乙的事"})).json()["id"]
        r = await c.put("/api/personal-todos/reorder", headers=H1, json={"ids": [t2, t1]})
        chk(r.status_code == 200,
            f"E) PUT /reorder 没被 /{{tid}} 吃掉（吃掉会是 422）: {r.status_code} {r.text[:90]}")
        rows = (await c.get("/api/personal-todos", headers=H1)).json()
        chk([x["id"] for x in rows] == [t2, t1], f"E) 顺序按传入的来: {[x['id'] for x in rows]}")
        # 混进别人的 id 只忽略不报错，且不能动到别人
        r = await c.put("/api/personal-todos/reorder", headers=H1, json={"ids": [t3, t2, t1]})
        chk(r.status_code == 200 and "2" in r.json()["message"],
            f"E) 混进别人的 id：忽略而不是整批失败，只排到自己的 2 条: {r.text[:80]}")
        async with SessionLocal() as db:
            other = (await db.execute(select(models.PersonalTodo).where(
                models.PersonalTodo.id == t3))).scalar_one()
            chk(other.sort_order == 0, f"E) 别人的 sort_order 没被动: {other.sort_order}")

        # ---------- C. 与管理层待办互不干扰 ----------
        r = await c.post("/api/management-todos", headers=H, json={
            "title": "别人交办的事", "priority": "normal", "recipient_ids": [uid["甲"]]})
        chk(r.status_code == 200, f"C) 下发一条管理层待办: {r.status_code}")
        mine = (await c.get("/api/management-todos/mine", headers=H1)).json()
        chk(len(mine) == 1, f"C) 甲收到 1 条管理层待办: {len(mine)}")
        ps = (await c.get("/api/personal-todos", headers=H1)).json()
        chk(len(ps) == 2 and all("别人交办" not in x["title"] for x in ps),
            f"C) 两边互不串：个人待办列表里没有管理层待办: {[x['title'] for x in ps]}")
        r = await c.delete(f"/api/personal-todos/{t2}", headers=H1)
        chk(r.status_code == 200, f"C) 删个人待办: {r.status_code}")
        chk(len((await c.get("/api/management-todos/mine", headers=H1)).json()) == 1,
            "C) 删个人待办不影响管理层待办（后者要留痕，删不得）")

        # ---------- D. 到期当天推一次企微 ----------
        today = date.today()
        async with SessionLocal() as db:
            t = (await db.execute(select(models.PersonalTodo).where(
                models.PersonalTodo.id == t1))).scalar_one()
            t.due_date = today.isoformat()
            t.priority = "urgent"
            await db.commit()

        class _Now(_dt):
            @classmethod
            def now(cls, tz=None):
                return _dt(today.year, today.month, today.day, 10, 0, tzinfo=tz)
        with patch.object(overdue, "datetime", _Now):
            async with SessionLocal() as db:
                res = await overdue.scan_personal_todos(db)
                await db.commit()
        chk(res["notified"] == 1, f"D) 到期当天推一次: {res}")
        async with SessionLocal() as db:
            ms = list((await db.execute(select(models.Message).where(
                models.Message.biz_type == "personal_todo_due"))).scalars().all())
            chk(len(ms) == 1 and ms[0].to_user_id == uid["甲"], f"D) 只推给本人: {len(ms)}")
            chk("【紧急】" in ms[0].text, f"D) 紧急的标出来: {ms[0].text}")

        # 同一天再扫不重复推
        with patch.object(overdue, "datetime", _Now):
            async with SessionLocal() as db:
                await overdue.scan_personal_todos(db)
                await db.commit()
        async with SessionLocal() as db:
            chk(len(list((await db.execute(select(models.Message).where(
                models.Message.biz_type == "personal_todo_due"))).scalars().all())) == 1,
                "D) 同一天不重复推")

        # 过期第二天**不再推**（自己设的闹钟天天响，人会把整个系统静音）
        tmr = today + timedelta(days=1)

        class _Tmr(_dt):
            @classmethod
            def now(cls, tz=None):
                return _dt(tmr.year, tmr.month, tmr.day, 10, 0, tzinfo=tz)
        with patch.object(overdue, "datetime", _Tmr):
            async with SessionLocal() as db:
                res2 = await overdue.scan_personal_todos(db)
                await db.commit()
        chk(res2["notified"] == 0,
            f"D) 过期后不再天天推（只在列表里标红）: {res2}")
        rows = (await c.get("/api/personal-todos", headers=H1)).json()
        row = next(x for x in rows if x["id"] == t1)
        chk(row["overdue"] is False, "D) 今天到期还不算逾期")

        # 已完成的不推
        async with SessionLocal() as db:
            t = (await db.execute(select(models.PersonalTodo).where(
                models.PersonalTodo.id == t1))).scalar_one()
            t.done = True
            await db.commit()
        with patch.object(overdue, "datetime", _Now):
            async with SessionLocal() as db:
                res3 = await overdue.scan_personal_todos(db)
        chk(res3["scanned"] == 0, f"D) 已打勾的不再提醒: {res3}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
