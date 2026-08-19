"""🆕 智能体下发待办：模型只能**拟草稿**，写库要人点卡片。

🐛 这个文件是一次生产 bug 换来的（2026-08-19）。第一版做成
「工具返回一个 confirm_token → 下一轮模型带回来才发」，上线当场死循环：

    15:11:05  测试   → 出草稿
    15:11:11  今天   → 又出草稿
    15:11:17  确认   → 又出草稿
    15:11:24  确认   → 又出草稿      （一条都没落库）

根因：**工具结果不进跨轮 history**。单轮 ReAct 里工具返回会以 role=tool 回灌，
但下一轮前端只传 user/assistant 的文本，工具返回全丢——模型根本看不见那个码。
任何「这轮给模型凭据、下轮让它带回来」的设计都不成立。

改成：草稿落库（`AgentDraft`）+ 卡片按钮，真正的写只在
`POST /agent/drafts/{id}/send`。所以本测试锁的是——

 1. 工具**没有**任何能直接写库的路径
 2. 草稿只有本人看得见、点得动
 3. 同一张草稿点两次只发一条
 4. 过期/停用/别人的，一律拒
"""
import asyncio, os, sys, tempfile
from datetime import date, datetime, timedelta, timezone

tmp = tempfile.mkdtemp(prefix="mtsend")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

import inspect
from fastapi import HTTPException
from sqlalchemy import select, func
from app.database import engine, SessionLocal, Base
from app import models
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app.agent import tools_entity as te
from app.agent import cards as _cards
from app.routers.agent_router import send_draft

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)
    else: print("  ok:", m)


def d(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


async def _n_todos(db) -> int:
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
            models.User(username="boss2", full_name="另一个管理层",
                        password_hash="x", role_id=rid),
        ])
        await db.commit()

    # ───────── ① 工具签名里根本没有「发」这条路 ─────────
    sig = inspect.signature(te.mgmt_todo_send)
    chk("confirm" not in sig.parameters,
        f"**工具没有 confirm 参数**（有的话又会绕回死循环）：{list(sig.parameters)}")
    src = inspect.getsource(te.mgmt_todo_send)
    chk("ManagementTodo(" not in src,
        "**工具源码里根本不建待办** —— 它只能写 AgentDraft")

    # ───────── ② 拟草稿：不写待办，只写草稿 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        r = await te.mgmt_todo_send(db, boss, title="压料机密封圈", to="夏锟",
                                    due_date=d(1))
        chk(r.get("draft") is True and r.get("draft_id"), f"出了草稿：{r.get('draft_id')}")
        chk(await _n_todos(db) == 0, "**拟草稿阶段一条待办都没有**")
        did = r["draft_id"]

    # ───────── ③ 草稿渲染成卡片，只有一个「发」动作 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        cards = await _cards.assemble_send_cards(db, boss)
        chk(len(cards) == 1 and cards[0]["ref"] == did, f"卡片指向草稿 {did}")
        chk([a["key"] for a in cards[0]["actions"]] == ["send"], "只有「发」一个动作")
        facts = {f["k"]: f["v"] for f in cards[0]["facts"]}
        chk(facts.get("派给") == "夏锟", "收件人在卡上")
        chk(cards[0]["facts"][0]["k"] == "派给" and cards[0]["facts"][0].get("emphasis"),
            "**收件人排第一行且加重** —— 发错人是这里唯一真正的风险")
        chk(_cards.allows("mgmt_todo_send", "send"), "白名单登记了 send")
        chk(not _cards.allows("mgmt_todo_send", "approve"), "白名单外的动作不放行")

        # 别人的草稿看不见
        other = (await db.execute(select(models.User).where(
            models.User.username == "boss2"))).scalars().first()
        chk(await _cards.assemble_send_cards(db, other) == [],
            "**别人的草稿看不见**（里面有收件人和事项）")

    # ───────── ④ 点一下才真发 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        out = await send_draft(did, boss, db)
        chk(out.get("ok") and out.get("todo_id"), f"发出去了：{out}")
        chk(await _n_todos(db) == 1, "落了一条待办")
        todo = (await db.execute(select(models.ManagementTodo))).scalars().first()
        chk(todo.title == "压料机密封圈" and todo.due_date == d(1), "内容对")
        tg = (await db.execute(select(models.ManagementTodoTarget))).scalars().all()
        chk(len(tg) == 1 and tg[0].status == "pending", "收件人一行，待回复")
        msg = (await db.execute(select(models.Message).where(
            models.Message.biz_type == "mgmt_todo"))).scalars().first()
        chk(msg and "压料机密封圈" in msg.text, "推了通知")

    # ───────── ⑤ 同一张草稿点第二次：只发一条 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        try:
            await send_draft(did, boss, db)
            chk(False, "第二次点应该被拒")
        except HTTPException as e:
            chk(e.status_code == 400, f"**点两次只发一条**：{e.detail}")
        chk(await _n_todos(db) == 1, "还是一条")
        chk(await _cards.assemble_send_cards(db, boss) == [], "用过的草稿不再出卡")

    # ───────── ⑥ 别人的草稿点不动 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        other = (await db.execute(select(models.User).where(
            models.User.username == "boss2"))).scalars().first()
        r2 = await te.mgmt_todo_send(db, boss, title="别人的事", to="夏波", due_date=d(2))
        try:
            await send_draft(r2["draft_id"], other, db)
            chk(False, "别人的草稿应该点不动")
        except HTTPException as e:
            chk(e.status_code == 403, f"**别人的草稿 403**：{e.detail}")

    # ───────── ⑦ 过期草稿不认 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        r3 = await te.mgmt_todo_send(db, boss, title="放久了的事", to="夏锟", due_date=d(3))
        old = await db.get(models.AgentDraft, r3["draft_id"])
        old.created_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        await db.commit()
        chk(not any(c["ref"] == r3["draft_id"]
                    for c in await _cards.assemble_send_cards(db, boss)),
            "过期草稿不出卡")
        try:
            await send_draft(r3["draft_id"], boss, db)
            chk(False, "过期草稿应该拒")
        except HTTPException as e:
            chk(e.status_code == 400, f"过期草稿拒绝：{e.detail}")

    # ───────── ⑧ 重复拟同一件事只留最新一张卡 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        a = await te.mgmt_todo_send(db, boss, title="重复的事", to="夏锟", due_date=d(1))
        b = await te.mgmt_todo_send(db, boss, title="重复的事", to="夏锟", due_date=d(2))
        refs = [c["ref"] for c in await _cards.assemble_send_cards(db, boss)]
        chk(a["draft_id"] not in refs and b["draft_id"] in refs,
            f"**同人同事只留最新一张**（否则点两张发两条）：{refs}")

    # ───────── ⑨ 人名与权限 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        amb = await te.mgmt_todo_send(db, boss, title="随便", to="夏")
        chk(amb.get("candidates") and "draft" not in amb,
            f"「夏」有歧义 → 给候选不出草稿：{amb.get('candidates')}")
        chk((await te.mgmt_todo_send(db, boss, title="随便", to="查无此人")).get("error"),
            "查无此人直说")
        chk((await te.mgmt_todo_send(db, boss, title="随便", to="离职的人")).get("error"),
            "停用的人不能当收件人")
        chk((await te.mgmt_todo_send(db, boss, title="", to="夏锟")).get("error"), "空标题拒绝")
        chk((await te.mgmt_todo_send(db, boss, title="x", to="夏锟",
                                     due_date="8月20")).get("error"), "日期格式")
        chk((await te.mgmt_todo_send(db, boss, title="x", to="夏锟",
                                     due_date=d(-1))).get("error"), "截止日在过去要反问")

        # 🐛 回归：头一回发待办的人拿不到快捷人选（生产实测 admin 就是这样），
        #    模型只能退化成「你要派给谁？直接说名字」——引导又变回打字。
        #    ⚠️ 要用**没有自己历史**的那位来验（boss 这时已经发过了）。
        fresh = (await db.execute(select(models.User).where(
            models.User.username == "boss2"))).scalars().first()
        peers = await te.mgmt_todo_peers(db, fresh)
        chk(peers.get("count", 0) > 0,
            f"**没自己的历史也要给候选**（拿全公司派过活的人兜底）：{peers}")
        chk(peers.get("from_my_history") is False, "标明这是兜底来的，不是他自己派过的")
        chk("夏锟" in [x["worker"] for x in peers["recent"]], "候选里有真被派过活的人")

        mine = await te.mgmt_todo_peers(db, boss)
        chk(mine.get("from_my_history") is True, "自己派过的人优先，不走兜底")

        u = (await db.execute(select(models.User).where(
            models.User.username == "xiakun"))).scalars().first()
        role = (await db.execute(select(models.Role).where(
            models.Role.code == "sales"))).scalars().first()
        if role:
            u.role_id = role.id
            await db.commit(); await db.refresh(u)
        chk((await te.mgmt_todo_send(db, u, title="x", to="夏波")).get("error"),
            "**非管理层拟不了**")
        chk(await _cards.assemble_send_cards(db, u) == [], "非管理层看不到草稿卡")

    await engine.dispose()
    print("\n" + ("FAILED: " + "; ".join(FAIL) if FAIL else "ALL PASS"))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
