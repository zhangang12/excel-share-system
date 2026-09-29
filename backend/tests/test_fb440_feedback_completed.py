"""🆕 2026-09-29 反馈#440（赵仁辉）：「生产部对已完成的项目不能提交问题反馈」。

提交接口本身从来不拦项目状态，拦住人的是「选项目」下拉 —— 原来只列「进行中」的项目，
项目一完成就从下拉里消失。可恰恰是做完、装完、甚至发货以后才发现的设计问题最该反馈回去。

本文件钉死：
  · 生产三组做过的已完成项目出现在下拉里，并带上状态（前端据此标「已完成」）；
  · 进行中的排在前面；
  · 选了已完成的项目真的能提交成功，并推给设计；
  · 没做过的项目照样不能提交（「本人在手」的校验没被放松）。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb440")
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

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        r = await c.post("/api/admin/users", headers=H, json={
            "username": "fb440_sm", "password": "pass123", "full_name": "张翔",
            "role_ids": [rid["sheetmetal"]]})
        uid = r.json()["id"]
        Hs = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': 'fb440_sm', 'password': 'pass123'})).json()['access_token']}"}

        async with SessionLocal() as db:
            ids = {}
            for code, st, mine in (("2026-070", "已完成", True), ("2026-090", "进行中", True),
                                   ("2026-060", "已完成", False)):
                p = models.Project(code=code, name=f"{code} 设备", status=st)
                db.add(p)
                await db.flush()
                ids[code] = p.id
                o = models.DeptOrder(project_id=p.id, dept="produce", status="done" if st == "已完成" else "in_progress")
                db.add(o)
                await db.flush()
                db.add(models.ProduceGroupTask(order_id=o.id, project_id=p.id, group="sheetmetal",
                                               status="done" if st == "已完成" else "dispatched",
                                               worker_id=uid if mine else None))
            await db.commit()

        print("\n=== 1. 下拉里有已完成的项目 ===")
        r = await c.get("/api/feedbacks/projects", headers=Hs)
        chk(r.status_code == 200, f"取下拉 → {r.status_code}")
        opts = r.json()
        codes = [x["code"] for x in opts]
        chk("2026-070" in codes,
            f"做过的已完成项目 2026-070 在下拉里（改之前只列进行中，它不会出现）→ {codes}")
        chk("2026-060" not in codes, "没做过的项目不在下拉里")
        chk(codes == ["2026-090", "2026-070"], f"进行中的排前面 → {codes}")
        st = {x["code"]: x.get("status") for x in opts}
        chk(st.get("2026-070") == "已完成", f"带上状态，前端据此标「已完成」→ {st}")

        print("\n=== 2. 选已完成的项目真能提交 ===")
        r = await c.post("/api/feedbacks", headers=Hs,
                         data={"project_id": str(ids["2026-070"]), "content": "折弯处开裂，下一台加筋"})
        chk(r.status_code == 200, f"已完成项目提交问题反馈 → {r.status_code} {r.text[:80]}")
        async with SessionLocal() as db:
            fb = (await db.execute(select(models.Feedback).where(
                models.Feedback.project_id == ids["2026-070"]))).scalars().first()
        chk(fb is not None and fb.status == "pending_design", "建单并直达设计接收")

        print("\n=== 3. 本人在手的校验没被放松 ===")
        r = await c.post("/api/feedbacks", headers=Hs,
                         data={"project_id": str(ids["2026-060"]), "content": "不是我的项目"})
        chk(r.status_code == 403, f"没做过的项目照样不能提交 → {r.status_code}")


asyncio.run(main())
print("\n" + ("全部通过 ✅" if not FAIL else f"{len(FAIL)} 条失败 ❌"))
for m in FAIL:
    print(" -", m)
sys.exit(1 if FAIL else 0)
