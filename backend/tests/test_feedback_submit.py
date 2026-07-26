"""🆕 2026-07-26「各部门的问题反馈不需要主管审批」测试：
1. 电工部（electrician）在手电工任务项目可提交问题反馈 → 建单即 pending_design（直达设计师，无审批环节）；
2. 电工未在手项目提交 → 403；生产三组（assembler 组派单）提交仍 200；
3. /projects 下拉与提交校验同源：电工在下拉里看到且仅看到在手项目；
4. 推送给项目设计师（design 任务 worker），设计师 mine 列表可见待接收。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fbsub")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
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
        async def login(u, p):
            r = await c.post('/api/auth/login', json={'username': u, 'password': p})
            assert r.status_code == 200, (u, r.text)
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login('admin', 'admin123')
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(name, *roles):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123", "full_name": name,
                "role_id": rid[roles[0]], "role_ids": [rid[x] for x in roles]})
            assert r.status_code == 200, (name, r.text)
            return await login(name, 'pass123'), r.json()["id"]

        Helec, elec_id = await mkuser("elec1", "electrician")
        Hasm, asm_id = await mkuser("asm1", "assembler")
        Hdes, des_id = await mkuser("des1", "designer")

        # 直插项目 + 任务：P1=电工在手+设计任务(des1)；P2=装配组在手(asm1)；P3=无人任务（不在任何人手上）
        async with SessionLocal() as db:
            p1 = models.Project(code="FB-T01", name="电工在手项目")
            p2 = models.Project(code="FB-T02", name="装配在手项目")
            p3 = models.Project(code="FB-T03", name="别人项目")
            db.add_all([p1, p2, p3]); await db.flush()
            db.add(models.DeptOrder(project_id=p1.id, dept="electric", status="in_progress", worker_id=elec_id))
            db.add(models.DeptOrder(project_id=p1.id, dept="design", status="in_progress", worker_id=des_id))
            po2 = models.DeptOrder(project_id=p2.id, dept="produce", status="in_progress", worker_id=999)
            db.add(po2); await db.flush()
            db.add(models.ProduceGroupTask(order_id=po2.id, project_id=p2.id, group="assembly", worker_id=asm_id))
            await db.commit()
            P1, P2, P3 = p1.id, p2.id, p3.id

        async def submit(headers, pid, text="电路图有问题"):
            return await c.post("/api/feedbacks", headers=headers,
                                data={"project_id": str(pid), "content": text})

        # ===== 1. 电工在手 → 200 + pending_design（不审批） =====
        r = await submit(Helec, P1)
        chk(r.status_code == 200, f"电工在手提交: {r.status_code} {r.text[:120]}")
        # ===== 2. 电工不在手（P3）→ 403 =====
        r = await submit(Helec, P3)
        chk(r.status_code == 403, f"电工不在手403: {r.status_code}")
        # ===== 3. 装配组在手（P2）仍 200 =====
        r = await submit(Hasm, P2)
        chk(r.status_code == 200, f"装配组在手提交: {r.status_code} {r.text[:120]}")
        # ===== 4. 下拉与校验同源：电工只见 P1 =====
        r = await c.get("/api/feedbacks/projects", headers=Helec)
        ids = {x["id"] for x in r.json()}
        chk(P1 in ids and P2 not in ids and P3 not in ids, f"电工下拉只在手项目: {ids}")
        # ===== 5. 设计师收到（pending_design 直达，无 pending_pm 审批环节） =====
        async with SessionLocal() as db:
            fbs = list((await db.execute(
                __import__("sqlalchemy").select(models.Feedback))).scalars().all())
        fb1 = next((f for f in fbs if f.project_id == P1), None)
        chk(fb1 is not None and fb1.status == "pending_design",
            f"建单即 pending_design: {fb1 and fb1.status}")
        chk(fb1 is not None and fb1.designer_uid == des_id, f"指派给项目设计师: {fb1 and fb1.designer_uid}")
        r = await c.get("/api/feedbacks", headers=Hdes, params={"mine": "true"})
        chk(r.status_code == 200 and any(x["project_id"] == P1 for x in r.json()),
            f"设计师待接收列表可见: {r.status_code}")
        # 确认没有任何 pending_pm 状态（不需要主管审批）
        chk(not any(f.status == "pending_pm" for f in fbs), "无 pending_pm 待审批单")

    await engine.dispose()
    if FAIL:
        print(f"\n{len(FAIL)} 项失败"); sys.exit(1)
    print("PASSED")

asyncio.run(main())
