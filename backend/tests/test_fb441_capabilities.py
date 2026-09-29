"""🆕 2026-09-29 反馈#441（赵仁辉）：「管理层待办 和 系统反馈，我可以选择给某些角色增加」。

老板拍板：两样都做成「用户管理」里可勾的功能权限，**默认仍只有管理层**。
  · feedback-entry（反馈入口）：右下角「反馈」按钮
  · todo-send（下发待办）：给别人派待办、看进展、批顺延

本文件钉死：
  · 没勾的人：下发 / 监控 / 选人名单 / 智能体派待办 一律 403 或不给工具；
  · 勾上以后能派，而且**只管得到自己派出去的**：看不到、改不了、撤不了、批不了别人（含管理层）派的；
  · 选人名单只给 id / 姓名 / 岗位，不带账号信息（原来用的 /admin/users 带全套）；
  · 管理层照旧看全部、管全部；
  · 两个新 key 能在「用户管理」里保存（不被当成非法菜单 400）。
"""
import asyncio, os, sys, tempfile
from datetime import date, timedelta

tmp = tempfile.mkdtemp(prefix="fb441")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models
from app.agent import perm
from app.agent.cards import mgmt_extend
from app.routers.agent_router import _allowed_tools

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def _u(name):
    async with SessionLocal() as db:
        return (await db.execute(select(models.User).where(
            models.User.username == name))).scalars().unique().one()


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
        async def login(u, p="pass123"):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(name, codes, menus):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123", "full_name": name,
                "role_ids": [rid[x] for x in codes]})
            assert r.status_code == 200, r.text
            uid = r.json()["id"]
            m = await c.put(f"/api/admin/users/{uid}/menus", headers=H, json={"menus": menus})
            assert m.status_code == 200, m.text
            return uid

        BASE = ["catalog", "list", "produce", "messages", "oa"]
        lead_id = await mkuser("xiakun", ["pm_lead"], BASE)          # 生产主管，先不给权限
        w1 = await mkuser("zhangxiang", ["sheetmetal"], BASE)
        w2 = await mkuser("daikun", ["assembler"], BASE)
        mgr_id = await mkuser("zhaorenhui", ["manager"], BASE)
        Hl, Hw1, Hm = await login("xiakun"), await login("zhangxiang"), await login("zhaorenhui")

        print("\n=== 1. 没勾「下发待办」：一律进不去 ===")
        r = await c.post("/api/management-todos", headers=Hl,
                         json={"title": "上传图纸", "recipient_ids": [w1]})
        chk(r.status_code == 403, f"下发 → {r.status_code}")
        chk((await c.get("/api/management-todos/sent", headers=Hl)).status_code == 403, "监控 → 403")
        chk((await c.get("/api/management-todos/recipients", headers=Hl)).status_code == 403, "选人名单 → 403")
        chk("mgmt_todo_send" not in _allowed_tools(await _u("xiakun")), "智能体也没有派待办的工具")
        m = (await c.get("/api/auth/menus", headers=Hl)).json()["menus"]
        chk("feedback-entry" not in [x["key"] for x in m], "也没有反馈入口")

        print("\n=== 2. 在「用户管理」里勾上两项 ===")
        r = await c.put(f"/api/admin/users/{lead_id}/menus", headers=H,
                        json={"menus": BASE + ["feedback-entry", "todo-send"]})
        chk(r.status_code == 200, f"两个新 key 能保存（不被当成非法菜单）→ {r.status_code} {r.text[:80]}")
        Hl = await login("xiakun")
        m = [x["key"] for x in (await c.get("/api/auth/menus", headers=Hl)).json()["menus"]]
        chk("feedback-entry" in m and "todo-send" in m, f"菜单里带上了这两个开关 → {m}")
        chk(perm.can_send_todo(await _u("xiakun")), "perm.can_send_todo 认账")
        chk("mgmt_todo_send" in _allowed_tools(await _u("xiakun")), "智能体派待办的工具也给了")

        print("\n=== 3. 能派，选人名单只给名字 ===")
        rr = await c.get("/api/management-todos/recipients", headers=Hl)
        chk(rr.status_code == 200, f"选人名单 → {rr.status_code}")
        one = next((x for x in rr.json() if x["id"] == w1), {})
        chk(set(one) == {"id", "username", "full_name", "role_names"},
            f"只给 id/姓名/岗位，不带菜单、企微号等账号信息 → {sorted(one)}")
        r = await c.post("/api/management-todos", headers=Hl, json={
            "title": "钣金图纸上传", "recipient_ids": [w1],
            "due_date": (date.today() + timedelta(days=2)).isoformat()})
        chk(r.status_code == 200, f"主管下发待办 → {r.status_code} {r.text[:80]}")
        lead_todo = r.json()["id"]
        msgs = (await c.get("/api/messages", headers=Hw1)).json()
        items = msgs["items"] if isinstance(msgs, dict) else msgs
        txt = " ".join(x.get("text", "") for x in items)
        chk("【待办】" in txt and "【管理层待办】" not in txt,
            "收件人收到的是「【待办】」，不冒充「【管理层待办】」")

        print("\n=== 4. 只管得到自己派的 ===")
        r = await c.post("/api/management-todos", headers=Hm,
                         json={"title": "管理层派的事", "recipient_ids": [w2]})
        mgr_todo = r.json()["id"]
        sent = [x["id"] for x in (await c.get("/api/management-todos/sent", headers=Hl)).json()]
        chk(sent == [lead_todo], f"主管的监控里只有自己派的 → {sent}")
        allsent = [x["id"] for x in (await c.get("/api/management-todos/sent", headers=Hm)).json()]
        chk(set(allsent) >= {lead_todo, mgr_todo}, "管理层照旧看全部")
        r = await c.put(f"/api/management-todos/{mgr_todo}", headers=Hl, json={"title": "改掉"})
        chk(r.status_code == 403, f"改不了管理层派的 → {r.status_code}")
        r = await c.delete(f"/api/management-todos/{mgr_todo}", headers=Hl)
        chk(r.status_code == 403, f"撤不了管理层派的 → {r.status_code}")
        r = await c.put(f"/api/management-todos/{lead_todo}", headers=Hl, json={"title": "钣金图纸上传（含冷作）"})
        chk(r.status_code == 200, f"改得了自己派的 → {r.status_code}")

        print("\n=== 5. 顺延：批得了自己派的，批不了别人的 ===")
        mine = (await c.get("/api/management-todos/mine", headers=Hw1)).json()
        tgt = next(x for x in mine if x["todo_id"] == lead_todo)["target_id"]
        await c.post(f"/api/management-todos/{tgt}/reply", headers=Hw1,
                     json={"committed_at": (date.today() + timedelta(days=2)).isoformat()})
        r = await c.post(f"/api/management-todos/{tgt}/extend", headers=Hw1, json={
            "extend_to": (date.today() + timedelta(days=5)).isoformat(), "reason": "等激光件"})
        chk(r.status_code == 200, f"工人申请顺延 → {r.status_code} {r.text[:60]}")
        # 管理层派的那条也申请一次顺延
        Hw2 = await login("daikun")
        mine2 = (await c.get("/api/management-todos/mine", headers=Hw2)).json()
        tgt2 = next(x for x in mine2 if x["todo_id"] == mgr_todo)["target_id"]
        await c.post(f"/api/management-todos/{tgt2}/reply", headers=Hw2,
                     json={"committed_at": (date.today() + timedelta(days=1)).isoformat()})
        await c.post(f"/api/management-todos/{tgt2}/extend", headers=Hw2, json={
            "extend_to": (date.today() + timedelta(days=3)).isoformat(), "reason": "缺料"})
        async with SessionLocal() as db:
            ext = await mgmt_extend.pending_extends(db, await _u("xiakun"))
        chk([t.id for t in ext] == [tgt],
            f"主管的待批顺延卡里只有自己派的那条 → {[t.id for t in ext]}")
        r = await c.post(f"/api/management-todos/{tgt2}/extend/decide", headers=Hl, json={"approve": True})
        chk(r.status_code == 403, f"批不了管理层派的那条 → {r.status_code}")
        r = await c.post(f"/api/management-todos/{tgt}/extend/decide", headers=Hl, json={"approve": True})
        chk(r.status_code == 200, f"批得了自己派的 → {r.status_code} {r.text[:60]}")


asyncio.run(main())
print("\n" + ("全部通过 ✅" if not FAIL else f"{len(FAIL)} 条失败 ❌"))
for m in FAIL:
    print(" -", m)
sys.exit(1 if FAIL else 0)
