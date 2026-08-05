"""🆕 问答审计日志的**覆盖面**：所有路径都要留痕，且留下的东西能拿来做优化。

原来的实现能查责（谁问了什么、答了什么），但分析不动，缺口有三个：

 1. ⚠️⚠️ **流式被中途放弃时一条都不留**。`_log_chat` 写在生成器末尾，
    用户划走/关页面/断网 → 生成器被取消 → 后面的代码永不执行。
    而生产上 llm 路径平均 14 秒、降级路径 20.8 秒，这个时长下「等不及走人」很常见，
    **丢掉的恰恰是最该拿来优化的那批样本**。
 2. **没有会话 id**，多轮串不起来，看不出「同一件事问了 3 遍才问明白」。
 3. **没有质量维度**：跑了几轮工具、查出多少条只给了几条、明细到底有没有渲染出来。

本测试对这三条各下一道断言。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="chatlog")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.main import app
from app.database import engine, SessionLocal, Base
from app import models
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)
    else: print("  ok:", m)


async def logs():
    async with SessionLocal() as db:
        return list((await db.execute(select(models.AgentChatLog)
                                      .order_by(models.AgentChatLog.id))).scalars().all())


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}

        print("===== 1. 非流式问答留痕（无 LLM key → 规则降级路径）=====")
        n0 = len(await logs())
        await c.post("/api/agent/chat", headers=H,
                     json={"message": "今日晨报", "history": [], "session_id": "sess-A"})
        rows = await logs()
        chk(len(rows) == n0 + 1, "非流式问答写了一条")
        last = rows[-1]
        chk(last.session_id == "sess-A", "带上了会话 id")
        chk(last.turn == 1, f"第 1 轮（实际 {last.turn}）")
        chk(last.outcome == "ok", f"outcome=ok（实际 {last.outcome}）")
        chk(last.answer_chars and last.answer_chars > 0, "记了回答字数")

        print("\n===== 2. 多轮 turn 递增（这样才看得出问了几遍）=====")
        await c.post("/api/agent/chat", headers=H, json={
            "message": "再说详细点", "session_id": "sess-A",
            "history": [{"role": "user", "content": "今日晨报"},
                        {"role": "assistant", "content": "…"}]})
        rows = await logs()
        same = [x for x in rows if x.session_id == "sess-A"]
        chk(len(same) == 2 and [x.turn for x in same] == [1, 2],
            f"同一会话两轮，turn 依次为 1/2（实际 {[x.turn for x in same]}）")

        print("\n===== 3. 直答路径也留痕 =====")
        n1 = len(await logs())
        await c.post("/api/agent/tool", headers=H,
                     json={"tool": "morning_report", "args": {}})
        rows = await logs()
        chk(len(rows) == n1 + 1, "直答写了一条")
        chk(rows[-1].via == "direct" and rows[-1].outcome == "ok", "标成 direct/ok")

        print("\n===== 4. ⚠️ 流式被中途放弃：必须仍然留一条 aborted =====")
        # ⚠️ 不能用 httpx 的 ASGI transport「读一片就 break」来模拟：
        #    那一层会把响应整个缓冲完，生成器**不会**被取消，测出来 outcome=ok，
        #    等于这条断言什么都没验到（第一版就是这么假绿的）。
        #    真实的客户端断开 = Starlette 对生成器调用 aclose()，这里直接照做。
        n2 = len(await logs())
        from app.routers import agent_router as ar

        class _Body:
            message = "今日晨报"
            history: list = []
            model = None
            session_id = "sess-B"

        async with SessionLocal() as db:
            admin_u = (await db.execute(select(models.User).where(
                models.User.username == "admin"))).scalars().first()
        resp = await ar.chat_stream(_Body(), admin_u)      # type: ignore[arg-type]
        agen = resp.body_iterator
        await agen.__anext__()                            # 拿到第一片
        await agen.aclose()                               # 用户划走 —— 生成器被取消
        for _ in range(40):
            await asyncio.sleep(0.05)
            rows = await logs()
            if len(rows) > n2:
                break
        chk(len(rows) == n2 + 1, f"中途放弃仍写了一条（实际多了 {len(rows) - n2} 条）")
        ab = rows[-1]
        chk(ab.outcome == "aborted", f"标成 aborted（实际 {ab.outcome}）")
        chk(ab.session_id == "sess-B", "aborted 记录也带会话 id")
        chk(ab.question == "今日晨报", "问题原样留下了 —— 这才知道用户当时在问什么")
        chk(ab.duration_ms is not None, "记了放弃前已经等了多久（这就是要优化的那个数）")

        print("\n===== 5. 完整流式：质量维度要有值 =====")
        n3 = len(await logs())
        async with c.stream("POST", "/api/agent/chat/stream", headers=H, json={
                "message": "采购未到货", "history": [], "session_id": "sess-C"}) as resp:
            async for _ in resp.aiter_bytes():
                pass                                    # 读到底
        rows = await logs()
        chk(len(rows) == n3 + 1, "完整读完只写一条，没有重复写")
        fin = rows[-1]
        chk(fin.outcome == "ok", f"读完标 ok（实际 {fin.outcome}）")
        chk(fin.duration_ms is not None and fin.duration_ms >= 0, "记了耗时")

        print("\n===== 6. 查询接口把新维度吐出来，且能按 outcome 筛 =====")
        d = (await c.get("/api/agent/chat-logs?size=5", headers=H)).json()
        chk(d["total"] >= 5, f"能查到日志（total={d['total']}）")
        it = d["items"][0]
        for f in ("session_id", "turn", "outcome", "tool_rounds",
                  "result_count", "result_shown", "rendered", "answer_chars"):
            chk(f in it, f"返回里有 {f}")
        d2 = (await c.get("/api/agent/chat-logs?outcome=ok&size=50", headers=H)).json()
        chk(all(x["outcome"] == "ok" for x in d2["items"]), "按 outcome 过滤生效")

        print("\n===== 7. 审计是旁路：写日志失败不能炸掉聊天 =====")
        from app.routers import agent_router as ar
        async with SessionLocal() as db:
            u = (await db.execute(select(models.User).where(
                models.User.username == "admin"))).scalars().first()
            # 传一个超长 via（列宽 8）验证截断兜底还在
            await ar._log_chat(db, u, "q", "a", [], via="rule-stream-fallback",
                               model="m", duration_ms=1, outcome="ok")
        rows = await logs()
        chk(rows[-1].via == "rule-str", f"via 截到 8 字符（实际 {rows[-1].via!r}）")

    await engine.dispose()
    print("\nPASSED" if not FAIL else f"\n{len(FAIL)} FAILURES")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
