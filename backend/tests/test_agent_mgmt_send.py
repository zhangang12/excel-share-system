"""🆕 智能体下发待办：**两步确认**才写库。

这是整个智能体里**唯一一条写路径**（其余全部只读）。所以本测试的重点不是
「能不能发出去」，而是「**能不能在没确认的情况下发出去**」——答案必须是不能。

风险不在技术在语义：赵仁辉真实打的字平均 8.1 字、最短 2 字（「采购」），
同样一句「压料机密封圈」既可能是要派给夏锟去办，也可能是问「库存还有没有」。
猜错就是凭空给人塞一条任务，而且收件人当场收到企微通知，撤不回来。
"""
import asyncio, os, sys, tempfile
from datetime import date, timedelta

tmp = tempfile.mkdtemp(prefix="mtsend")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select, func
from app.database import engine, SessionLocal, Base
from app import models
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app.agent import tools_entity as te, confirm as cf

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)
    else: print("  ok:", m)


def d(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


async def _count_todos(db) -> int:
    return (await db.execute(select(func.count(models.ManagementTodo.id)))).scalar_one()


async def main():
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        rid = boss.role_id
        db.add_all([
            models.User(username="xiakun", full_name="夏锟", password_hash="x", role_id=rid),
            models.User(username="xiabo", full_name="夏波", password_hash="x", role_id=rid),
            models.User(username="gone", full_name="离职的人", password_hash="x",
                        role_id=rid, is_active=False),
        ])
        await db.commit()

    # ───────── ① 第一次调用只出草稿，一条都不写库 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        before = await _count_todos(db)
        r = await te.mgmt_todo_send(db, boss, title="压料机密封圈", to="夏锟",
                                    due_date=d(1))
        chk(r.get("draft") is True, "第一次只出草稿")
        chk(r.get("confirm_token"), "给了确认码")
        chk(r["worker"] == "夏锟" and r["title"] == "压料机密封圈", "草稿内容对")
        chk(await _count_todos(db) == before, "**草稿阶段一条都没写库**")
        token = r["confirm_token"]

    # ───────── ② 确认码对了才真发 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        r2 = await te.mgmt_todo_send(db, boss, title="压料机密封圈", to="夏锟",
                                     due_date=d(1), confirm=token)
        chk(r2.get("sent") is True, f"确认后发出：{r2}")
        chk(await _count_todos(db) == 1, "写了一条")
        todo = (await db.execute(select(models.ManagementTodo))).scalars().first()
        chk(todo.title == "压料机密封圈" and todo.due_date == d(1), "落库内容对")
        chk(todo.priority == "normal", "默认普通档")
        tg = (await db.execute(select(models.ManagementTodoTarget))).scalars().all()
        chk(len(tg) == 1 and tg[0].status == "pending", "收件人一行，待回复")
        msg = (await db.execute(select(models.Message).where(
            models.Message.biz_type == "mgmt_todo"))).scalars().first()
        chk(msg is not None and "夏锟" not in msg.text, "推了通知给收件人")
        chk(msg and "压料机密封圈" in msg.text, f"通知带事项名：{msg.text if msg else None}")

    # ───────── ③ 篡改内容后原确认码作废 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        before = await _count_todos(db)
        r3 = await te.mgmt_todo_send(db, boss, title="压料机密封圈", to="夏波",
                                     due_date=d(1), confirm=token)
        chk(r3.get("error"), f"**换了收件人，原确认码不认**：{r3.get('error')}")
        chk(await _count_todos(db) == before, "没写库")

        r4 = await te.mgmt_todo_send(db, boss, title="别的事", to="夏锟",
                                     due_date=d(1), confirm=token)
        chk(r4.get("error"), "换了标题也不认")

        r5 = await te.mgmt_todo_send(db, boss, title="压料机密封圈", to="夏锟",
                                     due_date=d(1), confirm="随便编一个.xxxx")
        chk(r5.get("error"), f"编的确认码不认：{r5.get('error')}")
        chk(await _count_todos(db) == before, "始终没写库")

    # ───────── ④ 防重发：同一个码再来一次 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        r6 = await te.mgmt_todo_send(db, boss, title="压料机密封圈", to="夏锟",
                                     due_date=d(1), confirm=token)
        chk(r6.get("duplicate") is True, f"**认出是重复发**：{r6.get('error')}")
        chk(await _count_todos(db) == 1, "没多出一条")

    # ───────── ⑤ 人名不确定就问，绝不猜 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        r7 = await te.mgmt_todo_send(db, boss, title="随便", to="夏")
        chk(r7.get("candidates") and len(r7["candidates"]) == 2,
            f"「夏」命中两个人 → 给候选让用户挑，不猜：{r7.get('candidates')}")
        chk("draft" not in r7, "有歧义时不出草稿")

        r8 = await te.mgmt_todo_send(db, boss, title="随便", to="查无此人")
        chk(r8.get("error") and not r8.get("candidates"), "查无此人就直说")

        r9 = await te.mgmt_todo_send(db, boss, title="随便", to="离职的人")
        chk(r9.get("error"), "**停用的人不能当收件人**（发了也收不到，比报错还难查）")

    # ───────── ⑥ 日期与标题的基本校验 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        chk((await te.mgmt_todo_send(db, boss, title="", to="夏锟")).get("error"),
            "空标题拒绝")
        chk((await te.mgmt_todo_send(db, boss, title="x", to="夏锟",
                                     due_date="8月20")).get("error"),
            "日期格式不对就说清楚")
        chk((await te.mgmt_todo_send(db, boss, title="x", to="夏锟",
                                     due_date=d(-1))).get("error"),
            "截止日在过去 → 反问，别默默发出去")

    # ───────── ⑦ 非管理层一步都走不了 ─────────
    async with SessionLocal() as db:
        u = (await db.execute(select(models.User).where(
            models.User.username == "xiakun"))).scalars().first()
        role = (await db.execute(select(models.Role).where(
            models.Role.code == "sales"))).scalars().first()
        if role:
            u.role_id = role.id
            await db.commit(); await db.refresh(u)
        chk(not u.has_role("admin", "manager"), "这人不是管理层")
        chk((await te.mgmt_todo_send(db, u, title="x", to="夏波")).get("error"),
            "**非管理层发不了**")
        chk((await te.mgmt_todo_peers(db, u)).get("error"), "非管理层连人选都拿不到")

    # ───────── ⑧ 确认码过期 ─────────
    payload = {"title": "x", "uid": 1, "due": "", "urgent": False, "note": ""}
    old_tok = cf.issue("mgmt_todo_send", 1, payload, now=0)
    ok, why = cf.verify(old_tok, "mgmt_todo_send", 1, payload, now=10 * 60 + 5)
    chk(not ok and "失效" in why, f"超过 10 分钟失效：{why}")
    ok2, _ = cf.verify(old_tok, "mgmt_todo_send", 1, payload, now=60)
    chk(ok2, "十分钟内有效")
    ok3, _ = cf.verify(old_tok, "mgmt_todo_send", 2, payload, now=60)
    chk(not ok3, "**别人的确认码不能用**")
    ok4, _ = cf.verify(old_tok, "别的动作", 1, payload, now=60)
    chk(not ok4, "换个动作也不认（防止拿发待办的码去干别的）")

    await engine.dispose()
    print("\n" + ("FAILED: " + "; ".join(FAIL) if FAIL else "ALL PASS"))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
