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


    print("\n===== 5. 默认关思考：请求体里必须带 reasoning_effort=none =====")
    # ⚠️⚠️ 这是本次提速的核心开关。生产实测（2026-08-06）：
    #   pro 开思考 7487ms / 思维链 542 字 / **正文 0 字**（预算全被推理吃光）
    #   pro 关思考 2831ms / 思维链   0 字 /   正文 95 字
    #   带工具时：开 3280ms、关 2290ms，**工具调用两者都正常**。
    chk(ar._thinking_params({}) == {"reasoning_effort": "none"}, "缺省就是关思考")
    chk(ar._thinking_params({"thinking": "off"}) == {"reasoning_effort": "none"}, "off 关")
    chk(ar._thinking_params({"thinking": "on"}) == {}, "显式 on 时不注入（想开回去也能开）")

    seen_payload = {}

    async def spy_stream(messages, model, cfg, tools, max_tokens=700):
        seen_payload.update(ar._thinking_params(cfg))
        yield {"choices": [{"delta": {"content": "好"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

    ar._llm_stream = spy_stream
    try:
        async with SessionLocal() as db:
            u = (await db.execute(select(models.User).where(
                models.User.username == "admin"))).scalars().first()
        async for _ in ar._chat_stream("今日晨报", [], "m",
                                       {"api_key": "k", "base_url": "http://x",
                                        "model": "m", "thinking": "off"}, u):
            pass
        chk(seen_payload.get("reasoning_effort") == "none", "流式路径确实带上了关思考参数")
    finally:
        ar._llm_stream = orig

    print("\n===== 6. 重试要有时间闸：等太久就别再重试（否则拖成 network error）=====")
    chk(ar._RETRY_DEADLINE_S <= 20, f"重试闸门 {ar._RETRY_DEADLINE_S}s，不能太宽松")

    async def slow_thinking(messages, model, cfg, tools, max_tokens=700):
        # 模拟「想了很久还没正文」：超过闸门后不该再重试
        await asyncio.sleep(ar._RETRY_DEADLINE_S + 0.3)
        yield {"choices": [{"delta": {"reasoning_content": "唔" * 100}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "length"}]}

    calls = {"n": 0}

    async def counting(messages, model, cfg, tools, max_tokens=700):
        calls["n"] += 1
        async for x in slow_thinking(messages, model, cfg, tools, max_tokens):
            yield x

    ar._llm_stream = counting
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
        chk(calls["n"] == 1, f"超时后不再重试（实际请求 {calls['n']} 次）")
        chk(err is not None, "带着现状收尾（抛给上层降级），而不是继续拖")
    finally:
        ar._llm_stream = orig


    print("\n===== 7. 模型路由：单点查询走小模型，要分析的仍走大模型 =====")
    # ⚠️ `model_fast` 以前**根本没被读出来**（_effective_llm_config 里没有这个键），
    #    `_route_model` 里 cfg.get("model_fast") 永远 None → 模型路由是死代码，
    #    所有请求都在用大模型。实测 flash 关思考 1691ms vs pro 2318ms，白等 27%。
    cfg = {"model": "big", "model_fast": "small", "models": "big"}
    chk("small" in ar._model_whitelist(cfg),
        "小模型必须进白名单（否则路由过去会被「无效模型」挡下来，配了反而全挂）")
    for q in ("今日晨报", "待填收货人", "这个月销售额"):
        chk(ar._route_model(q, cfg, None) == "small", f"「{q}」→ 小模型")
    for q in ("项目进度跟进", "为什么这批都卡在电工", "把所有项目全部列出来", "交期风险"):
        chk(ar._route_model(q, cfg, None) == "big", f"「{q}」→ 大模型（要分析）")
    chk(ar._route_model("今日晨报", cfg, "big") == "big", "用户显式指定时永远不覆盖他的选择")
    chk(ar._route_model("今日晨报", {"model": "big", "models": "big"}, None) == "big",
        "没配 model_fast 时退回默认模型，不报错")

    await engine.dispose()
    print("\nPASSED" if not FAIL else f"\n{len(FAIL)} FAILURES")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
