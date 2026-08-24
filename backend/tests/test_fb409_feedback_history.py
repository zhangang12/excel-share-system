"""问题反馈要能翻历史（反馈#409 赵仁辉，2026-08-24）。

原话：「之前反馈的问题都看不到了」。他没说错——工作台那块只取「**待**我处理」的，
设计师一接收或驳回，那条就从面板上彻底消失，**界面上再没有任何入口**。
线上 11 条反馈里 10 条已是 archived / rejected_by_design，他只看得见 1 条。

加 `include_done=true` 看历史。**这里最容易做错的是顺手把可见范围也放宽了**：
看历史只该放宽**状态**，不该放宽**能看谁的**——否则设计师一勾就能翻到同事名下的全部反馈。
本文件主要就是焊死这条边界。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb409")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, update
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

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        async def login(u, p):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(name, codes):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123", "full_name": name,
                "role_ids": [rid[x] for x in codes]})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        d1 = await mkuser("设计甲", ["designer"])
        d2 = await mkuser("设计乙", ["designer"])
        mgr = await mkuser("管理层", ["manager"])
        Hd1, Hd2, Hm = await login("设计甲", "pass123"), await login("设计乙", "pass123"), await login("管理层", "pass123")

        pj = (await c.post("/api/projects", headers=H, json={"code": "2026-071B", "name": "提升式压料机"})).json()

        # 直接造反馈：一条待接收给甲、两条已办结给甲、一条已办结给乙
        async with SessionLocal() as db:
            rows = [
                models.Feedback(project_id=pj["id"], content="连接板放宽，焊接变形余量放大",
                                status="pending_design", designer_uid=d1, created_by=mgr),
                models.Feedback(project_id=pj["id"], content="导套卡簧槽太浅，卡簧突出",
                                status="archived", designer_uid=d1, created_by=mgr),
                models.Feedback(project_id=pj["id"], content="孔径小了20多丝",
                                status="rejected_by_design", designer_uid=d1, created_by=mgr),
                models.Feedback(project_id=pj["id"], content="乙名下的那条，甲不该看到",
                                status="archived", designer_uid=d2, created_by=mgr),
            ]
            db.add_all(rows)
            await db.commit()

        async def mine(hdr, done=False):
            p = {"mine": "true"}
            if done:
                p["include_done"] = "true"
            r = await c.get("/api/feedbacks", headers=hdr, params=p)
            assert r.status_code == 200, r.text
            return r.json()

        # ===== 1) 复刻他的处境：默认只看得到待办的那条 =====
        d1_default = await mine(Hd1)
        chk(len(d1_default) == 1 and d1_default[0]["status"] == "pending_design",
            f"1) 默认只有「待接收」那 1 条——办完的直接消失，这就是他说的「都看不到了」: "
            f"{[(x['status']) for x in d1_default]}")

        m_default = await mine(Hm)
        chk(len(m_default) == 1,
            f"1) 管理层同样只看得到 1 条（他正是管理层）: {len(m_default)}")

        # ===== 2) 勾上「看已处理的」能翻到历史 =====
        d1_all = await mine(Hd1, True)
        chk(len(d1_all) == 3,
            f"2) 设计甲看历史 → 自己名下 3 条全出来（1 待接收 + 1 已归档 + 1 已驳回）: {len(d1_all)}")
        sts = sorted(x["status"] for x in d1_all)
        chk(sts == ["archived", "pending_design", "rejected_by_design"],
            f"2) 三种状态都在: {sts}")

        m_all = await mine(Hm, True)
        chk(len(m_all) == 4, f"2) 管理层看历史 → 全部 4 条（他本来就是全局视角）: {len(m_all)}")

        # ===== 3) ⚠️ 关键边界：放宽状态 ≠ 放宽范围 =====
        contents = [x["content"] for x in d1_all]
        chk(not any("乙名下" in x for x in contents),
            f"3) **设计甲看历史时看不到乙名下的反馈** —— 只放宽状态，不放宽「能看谁的」。"
            f"写成无条件放行的话，一勾就能翻同事全部反馈: {contents}")

        d2_all = await mine(Hd2, True)
        chk(len(d2_all) == 1 and "乙名下" in d2_all[0]["content"],
            f"3) 反过来设计乙也只看得到自己那条: {[x['content'][:10] for x in d2_all]}")

        # ===== 4) 不带 include_done 时行为一个字都没变（别把老口径改坏）=====
        chk(len(await mine(Hd1)) == 1 and len(await mine(Hd2)) == 0,
            "4) 不带参数时还是老样子：甲 1 条、乙 0 条（乙名下那条已办结）")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
