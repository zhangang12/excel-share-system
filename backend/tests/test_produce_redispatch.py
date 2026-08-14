"""生产分组「补派」：派过之后还能把漏掉的组补上（反馈#399 的续，2026-08-14）。

线上真事：2026-067 的生产单只派了钣金组和封板组，**装配组漏了**，
装配组页签里当然找不到这个项目。杨坛来问「怎么派？没有入口」——他是对的：
派发按钮**只挂在「待分派」页签**，单子一派出去就离开那个列表，
漏派的组此后再也没有入口补。

后端 `dispatch_produce` 本来就是幂等的（已有的组改人、缺的组新建），
所以只是前端缺入口。本文件锁住后端这条路确实通，别哪天被"优化"成一次性的：
  1. 派过之后还能再调，把缺的组补上（不报错、不重复建）
  2. 已有的组再调一次 = 改派（改人，不新建第二条）
  3. 补派后 due_date / 完成状态等既有数据不能被冲掉
  4. 已完成/已作废的单子不许再派
  5. 接口要带出每组当前是谁（前端预填要用，缺了就没法区分"没派"和"派了但不知道给谁"）
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="redisp")
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
        uid = {}
        for uname, name, rc in [("sm1", "钣金张", "sheetmetal"), ("as1", "装配高", "assembler"),
                                ("as2", "装配李", "assembler"), ("sl1", "封板宝", "sealing"),
                                ("pm1", "生产主管", "pm_lead")]:
            rr = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "full_name": name, "role_id": rid[rc]})
            chk(rr.status_code == 200, f"建 {name}: {rr.status_code} {rr.text[:70]}")
            uid[name] = rr.json()["id"]
        HP = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': 'pm1', 'password': 'pass123'})).json()['access_token']}"}

        pj = (await c.post("/api/projects", headers=H, json={"code": "2026-067", "name": "补派测试"})).json()
        async with SessionLocal() as db:
            o = models.DeptOrder(project_id=pj["id"], dept="produce", status="pending_assign")
            db.add(o)
            await db.commit()
            oid = o.id

        async def groups():
            async with SessionLocal() as db:
                rows = list((await db.execute(select(models.ProduceGroupTask).where(
                    models.ProduceGroupTask.order_id == oid))).scalars().all())
            return {g.group: g for g in rows}

        # ---- 复刻线上：只派钣金 + 封板，装配组漏了 ----
        r = await c.post(f"/api/produce/dispatch/{oid}", headers=HP, json={
            "sheetmetal_worker_id": uid["钣金张"], "sealing_worker_id": uid["封板宝"]})
        chk(r.status_code == 200, f"首次派发（只派钣金+封板）: {r.status_code} {r.text[:90]}")
        g = await groups()
        chk(set(g) == {"sheetmetal", "sealing"},
            f"复刻线上状态：装配组确实没有 → 装配组页签找不到这个项目: {sorted(g)}")

        # 给钣金组填个预计完成 + 让封板组完成，验证补派不能冲掉这些
        r = await c.post(f"/api/produce/group/{g['sheetmetal'].id}/due", headers=HP,
                         json={"due_date": "2026-09-01"})
        chk(r.status_code == 200, f"钣金组填预计完成: {r.status_code} {r.text[:80]}")
        HS = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': 'sl1', 'password': 'pass123'})).json()['access_token']}"}
        r = await c.post(f"/api/produce/group/{g['sealing'].id}/done", headers=HS, json={"done": True})
        chk(r.status_code == 200, f"封板组标记完成: {r.status_code} {r.text[:80]}")

        # ---- 1) 补派装配组 ----
        r = await c.post(f"/api/produce/dispatch/{oid}", headers=HP, json={
            "assembly_worker_id": uid["装配高"]})
        chk(r.status_code == 200,
            f"1) **派过之后还能补派漏掉的组**（杨坛问的就是这个）: {r.status_code} {r.text[:90]}")
        g = await groups()
        chk(set(g) == {"sheetmetal", "assembly", "sealing"}, f"1) 三组齐了: {sorted(g)}")
        chk(g["assembly"].worker_id == uid["装配高"], "1) 装配组派给了装配高")

        # ---- 3) 补派不能冲掉已有数据 ----
        chk(g["sheetmetal"].due_date == "2026-09-01",
            f"3) 钣金组的预计完成没被冲掉: {g['sheetmetal'].due_date}")
        chk(g["sealing"].status == "done",
            f"3) 封板组的完成状态没被冲掉: {g['sealing'].status}")
        chk(g["sheetmetal"].worker_id == uid["钣金张"], "3) 钣金组的人没被动")

        # ---- 2) 再调一次 = 改派，不新建第二条 ----
        r = await c.post(f"/api/produce/dispatch/{oid}", headers=HP, json={
            "assembly_worker_id": uid["装配李"]})
        chk(r.status_code == 200, f"2) 改派装配组: {r.status_code}")
        g = await groups()
        chk(len(g) == 3 and g["assembly"].worker_id == uid["装配李"],
            f"2) 改人而不是多出一条装配组任务: 组数 {len(g)}，装配={g['assembly'].worker_id}")

        # ---- 5) 接口要带出每组当前是谁（前端预填用）----
        rows = (await c.get("/api/orders", headers=H, params={"dept": "produce"})).json()
        row = next((x for x in (rows if isinstance(rows, list) else rows.get("rows", []))
                    if x["id"] == oid), None)
        pg = {x["group"]: x for x in (row or {}).get("produce_groups") or []}
        chk(len(pg) == 3 and pg["assembly"]["worker_name"] == "装配李",
            f"5) 接口带出每组当前的人（前端靠它预填，缺了就分不清「没派」和「派了不知给谁」）: "
            f"{[(k, v.get('worker_name')) for k, v in pg.items()]}")

        # ---- 4) 已完成/已作废不许再派 ----
        async with SessionLocal() as db:
            oo = (await db.execute(select(models.DeptOrder).where(
                models.DeptOrder.id == oid))).scalar_one()
            oo.status = "voided"
            await db.commit()
        r = await c.post(f"/api/produce/dispatch/{oid}", headers=HP, json={
            "assembly_worker_id": uid["装配高"]})
        chk(r.status_code == 400, f"4) 已作废的单子不许再派: {r.status_code} {r.text[:80]}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
