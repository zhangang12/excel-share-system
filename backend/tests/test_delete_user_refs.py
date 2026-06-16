"""删除用户·外键引用清理回归测试（复现生产 PostgreSQL 的 FK 约束问题）。

dev/测试 SQLite 默认不强制外键，本测试显式 PRAGMA foreign_keys=ON，
使得：被多表引用的用户在删除时，旧实现会 FK 失败，新实现(全量清理引用)通过。
"""
import asyncio, os, sys, tempfile, shutil

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
tmp = tempfile.mkdtemp(prefix="deltest")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import event, select
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)

# 关键：让 SQLite 强制外键约束（默认关闭），以真实复现生产 FK 行为
@event.listens_for(engine.sync_engine, "connect")
def _fk_on(dbapi_conn, _rec):
    cur = dbapi_conn.cursor(); cur.execute("PRAGMA foreign_keys=ON"); cur.close()

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        # 建一个采购部用户(模拟 方步森)
        r = await c.post("/api/admin/users", headers=H, json={
            "username": "panlina", "password": "pass123", "full_name": "方步森", "role_id": rid["buyer"]})
        uid = r.json()["id"]

        # 跨多表插入对该用户的外键引用（含非空 FK：Message/ExportRequest）
        async with SessionLocal() as db:
            p = models.Project(code="2099-001", name="引用测试项目", status="进行中", manager_id=uid)
            db.add(p); await db.flush()
            db.add_all([
                models.Message(to_user_id=uid, kind="info", text="给方步森的消息"),       # 非空 FK
                models.ExportRequest(user_id=uid, scope="台账", status="pending"),         # 非空 FK
                models.ProjectMember(project_id=p.id, user_id=uid, permission="edit"),     # CASCADE
                models.SalesLedger(project_id=p.id, sales_uid=uid, amount=1000),
                models.DeptOrder(project_id=p.id, dept="design", created_by=uid, worker_id=uid),
                models.AfterSales(project_id=p.id, problem="x", cost=1, created_by=uid, appr_by=uid),
                models.Feedback(project_id=p.id, content="fb", created_by=uid, designer_uid=uid, appr_by=uid),
                models.UserFeedback(user_id=uid, kind="bug", content="uf"),
                models.AuditLog(user_id=uid, action="login"),
            ])
            await db.commit()
            pid = p.id

        # 删除用户：旧实现会因 Message/ExportRequest 等非空 FK 约束失败；新实现应 200
        r = await c.delete(f"/api/admin/users/{uid}", headers=H)
        chk(r.status_code == 200, f"删除带多表引用的用户应成功: {r.status_code} {r.text[:120]}")

        # 用户确实没了
        async with SessionLocal() as db:
            gone = (await db.execute(select(models.User).where(models.User.id == uid))).scalar_one_or_none()
            chk(gone is None, "用户已删除")
            # 非空 FK 行被删除
            msgs = (await db.execute(select(models.Message).where(models.Message.to_user_id == uid))).scalars().all()
            chk(len(msgs) == 0, "站内消息已删除")
            ers = (await db.execute(select(models.ExportRequest).where(models.ExportRequest.user_id == uid))).scalars().all()
            chk(len(ers) == 0, "导出申请已删除")
            pms = (await db.execute(select(models.ProjectMember).where(models.ProjectMember.user_id == uid))).scalars().all()
            chk(len(pms) == 0, "项目成员已删除")
            # 可空 FK 置 None，业务行保留
            proj = (await db.execute(select(models.Project).where(models.Project.id == pid))).scalar_one()
            chk(proj.manager_id is None, "项目负责人置空(项目保留)")
            led = (await db.execute(select(models.SalesLedger).where(models.SalesLedger.project_id == pid))).scalar_one()
            chk(led.sales_uid is None, "台账销售员置空(台账保留)")
            o = (await db.execute(select(models.DeptOrder).where(models.DeptOrder.project_id == pid))).scalar_one()
            chk(o.created_by is None and o.worker_id is None, "任务单 created_by/worker_id 置空")
            uf = (await db.execute(select(models.UserFeedback))).scalars().all()
            chk(uf and uf[0].user_id is None, "用户反馈提交人置空(反馈保留)")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
