"""🆕 推理型模型把 max_tokens 全花在思维链上时，不能整条降级成功能菜单。

生产事故（2026-08-05）：用户问「项目进度跟进」，日志里
    tools_used=[]，duration_ms=15628，via=rule-stream-fallback
    [agent] 流式失败，转规则降级: LLM 返回空内容
最终返回的是那段「我是 ERP 数据助手，目前可以回答…」的功能菜单 ——
用户看到的就是「交期看板直接不能用」。

⚠️ 根因**不在工具、不在提示词，在预算**：模型是 deepseek 推理型，
   `max_tokens` 由**思维链和正文共用**。700 tokens 先被思维链吃光，
   于是正文为空、工具一个没调，被判成「LLM 返回空内容」。

两道防线，本测试各锁一条：
 1. 要动脑子的问法**一开始就给足预算**（_max_tokens_for），别等重试。
 2. 万一仍然空了且 finish_reason=length / 有思维链 → **加码重试一次**，
    而不是直接降级。
"""
import asyncio, json, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="budget")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from app.database import engine, SessionLocal, Base
from app import models
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app.routers import agent_router as ar

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)
    else: print("  ok:", m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    print("===== 1. 要动脑子的问法：一开始就给足预算 =====")
    # 这几条都是生产上真实挂掉或同类的问法
    for q in ("项目进度跟进", "生产进度监控", "哪些项目快到期了",
              "2026-071 卡在哪", "交期盘点", "还剩多少天"):
        got = ar._max_tokens_for(q)
        chk(got > ar._MAX_TOKENS_DEFAULT,
            f"「{q}」预算 {got} > 默认 {ar._MAX_TOKENS_DEFAULT}")
    # 单点查询不该被顺带抬价（抬了就是白烧钱、白等）
    for q in ("今日晨报", "待填收货人", "库存还有多少"):
        chk(ar._max_tokens_for(q) == ar._MAX_TOKENS_DEFAULT,
            f"「{q}」仍走默认预算（单点查询不该抬价）")
    chk(ar._max_tokens_for("把所有项目全部列出来") == ar._MAX_TOKENS_FULL,
        "要全量清单的仍给最大预算")

    print("\n===== 2. 思维链吃光预算 → 加码重试，而不是降级 =====")
    # 假 LLM：第一轮只吐思维链 + finish_reason=length（正文为空，无工具调用），
    # 第二轮（预算被抬高后）正常作答。复刻生产上那次的形状。
    seen_budgets: list[int] = []

    async def fake_stream(messages, model, cfg, tools, max_tokens=700):
        seen_budgets.append(max_tokens)
        if len(seen_budgets) == 1:
            yield {"choices": [{"delta": {"reasoning_content": "唔" * 400}}]}
            yield {"choices": [{"delta": {}, "finish_reason": "length"}]}
            return
        for piece in ("在建项目 46 个，", "已过交货日 19 个。"):
            yield {"choices": [{"delta": {"content": piece}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

    orig = ar._llm_stream
    ar._llm_stream = fake_stream
    try:
        async with SessionLocal() as db:
            u = (await db.execute(select(models.User).where(
                models.User.username == "admin"))).scalars().first()
        out, tools = "", []
        async for kind, payload in ar._chat_stream(
                "项目进度跟进", [], "deepseek-v4-pro",
                {"api_key": "k", "base_url": "http://x", "model": "m"}, u):
            if kind == "delta":
                out += payload
            elif kind == "done":
                out, tools = payload["text"], payload["tools"]
        chk("46" in out and "19" in out, f"重试后拿到了正文（{out[:40]!r}）")
        chk(len(seen_budgets) == 2, f"确实重试了一次（实际请求 {len(seen_budgets)} 次）")
        chk(seen_budgets[1] > seen_budgets[0],
            f"第二次预算被抬高：{seen_budgets[0]} → {seen_budgets[1]}")
    finally:
        ar._llm_stream = orig

    print("\n===== 3. 真的什么都没有（不是预算问题）→ 仍然按失败处理 =====")
    async def empty_stream(messages, model, cfg, tools, max_tokens=700):
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

    ar._llm_stream = empty_stream
    try:
        async with SessionLocal() as db:
            u = (await db.execute(select(models.User).where(
                models.User.username == "admin"))).scalars().first()
        err = None
        try:
            async for _ in ar._chat_stream("今日晨报", [], "m",
                                           {"api_key": "k", "base_url": "http://x",
                                            "model": "m"}, u):
                pass
        except RuntimeError as e:
            err = str(e)
        chk(err is not None and "空内容" in err,
            f"没有思维链也没 length → 照旧报空内容（{err!r}）")
    finally:
        ar._llm_stream = orig

    print("\n===== 4. 流里夹带的错误要带出来，不能报成含糊的「空内容」 =====")
    async def err_stream(messages, model, cfg, tools, max_tokens=700):
        yield {"error": {"message": "context_length_exceeded"}}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

    ar._llm_stream = err_stream
    try:
        async with SessionLocal() as db:
            u = (await db.execute(select(models.User).where(
                models.User.username == "admin"))).scalars().first()
        err = None
        try:
            async for _ in ar._chat_stream("今日晨报", [], "m",
                                           {"api_key": "k", "base_url": "http://x",
                                            "model": "m"}, u):
                pass
        except RuntimeError as e:
            err = str(e)
        chk(err is not None and "context_length_exceeded" in err,
            f"错误原文进了异常（{err!r}）—— 否则排障只能看到「返回空内容」")
    finally:
        ar._llm_stream = orig

    await engine.dispose()
    print("\nPASSED" if not FAIL else f"\n{len(FAIL)} FAILURES")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
