"""🆕 2026-07-29 反馈回复企微推送测试：
1. 用户提交反馈 → 管理层回复 → 提出人收到站内消息（biz_type=user_feedback，含「反馈回复」，双通道企微可达）；
2. 管理层回复自己的反馈 → 不推（本人不打扰）。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fbpush")
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
        r = await c.post("/api/admin/users", headers=H,
                         json={"username": "w1", "password": "pass123", "full_name": "w1", "role_id": rid["warehouse"]})
        assert r.status_code == 200, r.text
        Hw1 = await login('w1', 'pass123')
        w1id = r.json()["id"]

        # w1 提交反馈
        r = await c.post("/api/user-feedback", headers=Hw1,
                         data={"kind": "问题反馈", "content": "测试反馈内容", "page_url": "/warehouse"})
        chk(r.status_code in (200, 201), f"提交反馈: {r.status_code} {r.text[:120]}")
        fid = r.json()["id"]

        # admin 回复 → w1 收到站内消息
        r = await c.post(f"/api/user-feedback/{fid}/reply", headers=H,
                         json={"reply": "已按你的建议修复并上线，请更新后查看。"})
        chk(r.status_code == 200, f"回复反馈: {r.status_code} {r.text[:120]}")
        async with SessionLocal() as db:
            msgs = list((await db.execute(select(models.Message).where(
                models.Message.to_user_id == w1id,
                models.Message.biz_type == "user_feedback",
                models.Message.biz_id == fid))).scalars().all())
        chk(len(msgs) == 1 and "反馈回复" in msgs[0].text and "已按你的建议修复" in msgs[0].text,
            f"提出人收到回复推送: {len(msgs)}")

        # admin 给自己的反馈回复 → 不推
        r = await c.post("/api/user-feedback", headers=H,
                         data={"kind": "建议", "content": "管理层自己的反馈", "page_url": "/overview"})
        fid2 = r.json()["id"]
        admin_id = (await c.get("/api/auth/me", headers=H)).json()["id"]
        r = await c.post(f"/api/user-feedback/{fid2}/reply", headers=H, json={"reply": "自答自记"})
        chk(r.status_code == 200, f"自回复: {r.status_code}")
        async with SessionLocal() as db:
            msgs2 = list((await db.execute(select(models.Message).where(
                models.Message.to_user_id == admin_id,
                models.Message.biz_type == "user_feedback",
                models.Message.biz_id == fid2))).scalars().all())
        chk(len(msgs2) == 0, f"本人回复自己不推: {len(msgs2)}")

    await engine.dispose()
    if FAIL:
        print(f"\n{len(FAIL)} 项失败"); sys.exit(1)
    print("PASSED")

asyncio.run(main())
