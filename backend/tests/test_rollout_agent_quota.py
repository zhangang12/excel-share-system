"""🆕 2026-09-23 智能体用量闸（app/agent/quota.py）。

推广到全部 26 个账号之前加的两道闸：每人每日 LLM 次数上限、全局并发上限。
两道闸都**不报错**，而是降级到规则引擎 —— 规则路径不花钱、查的是同一批真数据。

这个文件要钉死的是「闸不能误伤」：
  · 没到上限的人一次都不该被拦；
  · 规则降级不占额度（否则降级越多越快没额度，闸会自己把自己锁死）；
  · 管理层不受日限；
  · 上限设成 0 = 不限制（管理员要能一键关掉）；
  · 并发位必须还得回来（漏一次就永久少一个，表现是「助手越来越慢」）；
  · 额度按**中国时间**切天，不是 UTC（UTC 切的话每天早上 8 点前算的是昨天）。
"""
import asyncio, os, sys, tempfile
from datetime import datetime, timedelta, timezone

tmp = tempfile.mkdtemp(prefix="rollquota")
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
from app.agent import quota
from app.overdue import _CN_TZ

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def _u(uname):
    async with SessionLocal() as db:
        return (await db.execute(select(models.User)
                                 .where(models.User.username == uname))).scalars().unique().one()


async def _log(uid, uname, via, when=None):
    """塞一条问答日志。用量是从审计日志数出来的，所以造数据就是造日志。"""
    async with SessionLocal() as db:
        row = models.AgentChatLog(
            user_id=uid, username=uname, question="q", answer="a",
            tools_used=[], via=via, model="m", duration_ms=1, outcome="ok")
        if when is not None:
            row.created_at = when
        db.add(row)
        await db.commit()


async def _set(key, val):
    async with SessionLocal() as db:
        import json
        r = (await db.execute(select(models.AppSetting).where(
            models.AppSetting.key == key))).scalar_one_or_none()
        if r:
            r.value = json.dumps(val)
        else:
            db.add(models.AppSetting(key=key, value=json.dumps(val)))
        await db.commit()


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
            "username": "q_asm", "password": "pass123", "full_name": "q_asm",
            "role_ids": [rid["assembler"]]})
        assert r.status_code == 200, r.text
        uid = r.json()["id"]

        u = await _u("q_asm")
        adm = await _u("admin")

        print("\n=== 1. 日限：没到上限一次都不拦 ===")
        await _set("agent_daily_llm_limit", 3)
        async with SessionLocal() as db:
            ok, note = await quota.check_daily(db, u)
        chk(ok and not note, "一次没用过：放行，且不该有任何提示语")

        await _log(uid, "q_asm", "llm")
        await _log(uid, "q_asm", "llm")
        async with SessionLocal() as db:
            ok, _ = await quota.check_daily(db, u)
            used = await quota.used_today(db, u)
        chk(ok and used == 2, f"用了 2/3：还放行 → used={used}")

        await _log(uid, "q_asm", "llm")
        async with SessionLocal() as db:
            ok, note = await quota.check_daily(db, u)
        chk(not ok, "用满 3/3：拦住")
        chk("3/3" in note and "明天" in note,
            f"拦住时给的是**能看懂的中文**，说清用了多少、什么时候恢复 → {note}")

        print("\n=== 2. 规则降级不占额度 ===")
        await _set("agent_daily_llm_limit", 50)
        for _ in range(5):
            await _log(uid, "q_asm", "rule")
        async with SessionLocal() as db:
            used = await quota.used_today(db, u)
        chk(used == 3,
            f"又记了 5 条 rule，用量仍是 3 —— 降级不花钱，占额度的话闸会自己把自己锁死"
            f" → {used}")

        print("\n=== 3. 按中国时间切天，不是 UTC ===")
        # 中国时间今天 00:30（= UTC 昨天 16:30）。按 UTC 切会把它算成昨天，漏计。
        cn_today = datetime.now(_CN_TZ).date()
        just_after_cn_midnight = datetime(cn_today.year, cn_today.month, cn_today.day,
                                          0, 30, tzinfo=_CN_TZ).astimezone(timezone.utc)
        await _log(uid, "q_asm", "llm", when=just_after_cn_midnight)
        async with SessionLocal() as db:
            used = await quota.used_today(db, u)
        chk(used == 4,
            f"中国时间今天凌晨 0:30 那条**要算今天**（UTC 上它属于昨天）→ {used}")
        # 中国时间昨天 23:30 不该算进来
        y = datetime(cn_today.year, cn_today.month, cn_today.day, 23, 30,
                     tzinfo=_CN_TZ) - timedelta(days=1)
        await _log(uid, "q_asm", "llm", when=y.astimezone(timezone.utc))
        async with SessionLocal() as db:
            used = await quota.used_today(db, u)
        chk(used == 4, f"中国时间昨天 23:30 那条不算 → {used}")

        print("\n=== 4. 管理层不受日限 / 上限设 0 = 关闸 ===")
        await _set("agent_daily_llm_limit", 1)
        for _ in range(3):
            await _log(adm.id, "admin", "llm")
        async with SessionLocal() as db:
            ok, _ = await quota.check_daily(db, adm)
        chk(ok, "管理层用超了也放行（这套东西本来就是给他们用、也是他们在付钱）")
        async with SessionLocal() as db:
            ok, _ = await quota.check_daily(db, u)
        chk(not ok, "普通员工照拦（别把闸整个改松了）")
        await _set("agent_daily_llm_limit", 0)
        async with SessionLocal() as db:
            ok, note = await quota.check_daily(db, u)
        chk(ok and not note, "上限设 0：一键关掉日限，管理员不用重启就能救急")

        print("\n=== 5. 配置写坏了不能把功能关掉 ===")
        await _set("agent_daily_llm_limit", "一百")
        async with SessionLocal() as db:
            lim = await quota._setting(db, "agent_daily_llm_limit", 60)
        chk(lim == 60, f"值不是整数时退回默认，不是 0 也不是崩 → {lim}")

        print("\n=== 6. 并发闸：位子要还得回来 ===")
        await _set("agent_max_concurrent_llm", 2)
        async with SessionLocal() as db:
            a1 = await quota.acquire(db)
            a2 = await quota.acquire(db)
        chk(a1 and a2, "上限 2：头两个都占得到")
        quota._WAIT_SECONDS = 0.05      # 第三个不必真等 8 秒
        async with SessionLocal() as db:
            a3 = await quota.acquire(db)
        chk(not a3, "第三个占不到 → 由调用方转规则降级（不是报错）")
        quota.release()
        async with SessionLocal() as db:
            a4 = await quota.acquire(db)
        chk(a4, "还回去一个，下一个就占得到 —— 漏还的话这个位子永久少一个")
        quota.release(); quota.release()
        await _set("agent_max_concurrent_llm", 0)
        async with SessionLocal() as db:
            chk(await quota.acquire(db), "上限设 0：并发闸也能一键关掉")

        print("\n=== 7. 端到端：超日限时 /chat 仍然 200，并把原因说给用户 ===")
        await _set("agent_daily_llm_limit", 1)
        Hu = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': 'q_asm', 'password': 'pass123'})).json()['access_token']}"}
        rr = await c.post("/api/agent/chat", headers=Hu, json={"message": "在建项目有哪些"})
        chk(rr.status_code == 200,
            f"超限**不能是 429/500** —— 一线看到报错只会认为系统坏了 → {rr.status_code}")
        if rr.status_code == 200:
            body = rr.json()
            chk(body.get("fallback") is True, "走的是规则降级")
            # 没配 api_key 的测试环境本来就会降级，这里只断言「答得出来」，
            # 提示语的断言留给上面的 check_daily 单测（那条不依赖 LLM 配置）
            chk(bool(body.get("reply")), "而且真的答出了内容，不是空壳")


asyncio.run(main())
print("\n" + ("全部通过 ✅" if not FAIL else f"{len(FAIL)} 条失败 ❌"))
for m in FAIL:
    print(" -", m)
sys.exit(1 if FAIL else 0)
