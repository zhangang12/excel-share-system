"""工具截断与 limit（app/routers/agent_router.py 的 _cap）。

起因是用户的三张截图：
  1. 问「待填收货人」→ 助手答「记录均未关联到客户/公司信息（均为空），只有单据编号」。
     **数据其实一直都在**：生产 49 条待填里 49 条有项目编号、44 条有客户名。
     工具读的是 `shipment.receiver_company`——那正是「还没填」的那个字段，当然全空。
     没沿 project_id 去取真客户名，于是让人以为系统里查不到。
  2. 问「采购未到货」→ 清单列到第 2 条就断了（max_tokens=700 写不完）。
  3. 说「全部列举出来，按超期天数排序」→ 助手回了一段功能菜单。
     查日志：**近 3 天一次降级都没有**，全走的 LLM。所以不是兜底，
     是三重上限（工具 `rows[:20]` + 提示词「最多 5 条/250 字」+ max_tokens 700）
     让它结构上就给不出 46 条，而且**从不声明自己截断了**。

这个测试钉住修复后的口径：
  - 工具本身返回全量，截断只在 `_cap` 一处发生（否则 limit 形同虚设）
  - `count` 是总数、`shown` 是给了几条、`truncated` 是没给几条
  - limit 可调、有上限
  - 收货人工具必须带出项目编号与客户名
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="lim")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from app.database import engine, SessionLocal, Base
from app import models
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app.routers.agent_router import _cap, _LIMIT_MAX, _max_tokens_for, _SYSTEM_PROMPT
from app.agent import tools_sales as ts

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

    # ================= _cap 口径 =================
    print("\n===== 截断口径 =====")
    big = {"count": 46, "items": [{"i": i} for i in range(46)]}

    r = _cap(dict(big, items=list(big["items"])), {})
    chk(r["shown"] == 20 and r["truncated"] == 26 and r["count"] == 46,
        f"默认给 20 条，明确记下总数 46 / 没给 26：shown={r['shown']} truncated={r['truncated']}")

    r = _cap(dict(big, items=list(big["items"])), {"limit": 200})
    chk(r["shown"] == 46 and r["truncated"] == 0,
        f"limit=200 → 46 条全给（旧版工具写死 rows[:20]，这里永远只能拿到 20）：shown={r['shown']}")

    r = _cap(dict(big, items=list(big["items"])), {"limit": 5})
    chk(r["shown"] == 5 and r["truncated"] == 41, "limit 可以调小")

    r = _cap(dict(big, items=list(big["items"])), {"limit": 99999})
    chk(r["shown"] == 46 and _LIMIT_MAX == 200, f"limit 有上限 {_LIMIT_MAX}，不让模型把上下文顶爆")

    r = _cap(dict(big, items=list(big["items"])), {"limit": "abc"})
    chk(r["shown"] == 20, "limit 传了非数字 → 退回默认 20，不抛异常")

    r = _cap({"count": 3, "suppliers": [1, 2, 3]}, {})
    chk(r["shown"] == 3 and r["truncated"] == 0,
        "按供应商汇总那类返回的是 suppliers，也要被截断/计数")

    chk(_cap("不是字典", {}) == "不是字典", "非字典结果原样返回，不炸")

    # ================= 收货人工具带客户名 =================
    print("\n===== 待填收货人要带出客户是谁 =====")
    async with SessionLocal() as db:
        me = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalar_one()
        for i in range(3):
            p = models.Project(code=f"SHIP-{i}", name=f"发货测试{i}", is_deleted=False)
            db.add(p); await db.flush()
            db.add(models.SalesLedger(project_id=p.id, sales_uid=me.id,
                                      customer=f"某某客户{i}有限公司", amount=100000,
                                      balance_date="", order_state="approved"))
            # 收货人没填 → 正是「待填收货人」；receiver_company 也空（这才是常态）
            db.add(models.Shipment(project_id=p.id, status="pending",
                                   receiver_name="", receiver_phone="", receiver_addr=""))
        await db.commit()

        out = await ts.tool_shipment_receiver(db, me)
        items = out["items"]
        chk(len(items) == 3, f"查到 3 条待填：{len(items)}")
        chk(all(it.get("project_code", "").startswith("SHIP-") for it in items),
            f"每条都带项目编号：{[i.get('project_code') for i in items]}")
        chk(all(it.get("customer", "—") != "—" for it in items),
            f"**每条都带客户名**（旧版读 receiver_company，全是 —）：{[i.get('customer') for i in items]}")
        chk("company" not in items[0],
            "不再输出误导性的 company 字段（那读的是还没填的 receiver_company）")

    # ================= max_tokens 与提示词 =================
    print("\n===== 要全量时得给够 token =====")
    chk(_max_tokens_for("全部列举出来，按照超期天数做一个排序") > 2000,
        f"「全部列举」→ {_max_tokens_for('全部列举出来，按照超期天数做一个排序')}（700 连 20 条中文都写不完）")
    chk(_max_tokens_for("详细列举") > 2000, "「详细列举」同样放开")
    chk(_max_tokens_for("这月销售额多少？") == 700, "普通问题保持 700，不浪费")

    print("\n===== 提示词必须要求「截断要说出来」=====")
    t = _SYSTEM_PROMPT.format(today="2026-08-04", user_name="杨坛", roles="管理层")
    chk("truncated" in t and "另有" in t, "提示词明确要求声明截断条数")
    chk("limit=200" in t, "提示词告诉模型要全量时重调工具传 limit=200")
    chk("待发货" in t, "提示词带上「发货单大量停在待发货」这个口径坑")
    chk("假亏损" in t, "提示词带上「合同额为 0 → 毛利算成假亏损」这个口径坑")
    chk("不要拿截断后的 shown 去算" in t, "禁止拿截断后的条数算比例")

    # ================= 流式路径不能因作用域错误静默降级 =================
    print("\n===== 流式请求不能悄悄掉进降级 =====")
    import ast, inspect
    from app.routers import agent_router as ar

    # 这条是血的教训：_llm_stream 里引用了调用方的局部变量 max_tokens，
    # 运行时 NameError 被外层 except 吞掉 → 整条流式请求降级成规则应答，
    # 返回「我是 ERP 数据助手」那段功能菜单。现象是**秒回但答非所问**，
    # 极易被误读成「变快了」。所以这里静态扫一遍作用域。
    src = inspect.getsource(ar)
    tree = ast.parse(src)
    bad = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        local = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                local.add(n.id)
        for n in ast.walk(fn):
            if (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                    and n.id == "max_tokens" and "max_tokens" not in local):
                bad.append(f"{fn.name}:{n.lineno}")
    chk(not bad, f"没有函数引用作用域外的 max_tokens：{bad}")

    sig = inspect.signature(ar._llm_stream)
    chk("max_tokens" in sig.parameters,
        f"_llm_stream 必须把 max_tokens 当参数收（不是蹭调用方的局部变量）：{sig}")
    chk("_llm_stream(messages, model, cfg, schemas, max_tokens)" in src,
        "调用点必须真的把 max_tokens 传进去")

    print("\n" + "=" * 56)
    if FAIL:
        print(f"❌ {len(FAIL)} 条失败：")
        for f in FAIL: print("   -", f)
        sys.exit(1)
    print("✅ 全部通过")

asyncio.run(main())
