"""智能体 v2 阶段三~七：Skills / 记忆 / RAG / 模型路由 / ReAct 硬约束。

设计见 docs/agent-architecture-v2.md。

技能选型依据是两位管理层近 30 天真实操作：
  杨坛   188 次：销售台账+订单 32%  → 客户回款画像、项目体检
  赵仁辉 635 次：**仓库 36%**、物料字典 170 → 库存预警、供应商拖期复盘

⚠️ 全篇最要紧的一条：**技能命中判定必须严**。v1 那个贪婪正则
   `/待我审批|待审|请款单|审批/` 把用户打的「查询一下所有的待审批的待办?」
   劫持成查请款单 —— 宁可漏判走 ReAct，也不能改写用户的问题。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="v2sk")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from app.database import engine, SessionLocal, Base
from app import models
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app.agent import skills as sk, memory as mem
from app.routers.agent_router import _route_model, _run_tool, _FULL_LIST_HINT

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

    # ================= 技能命中 =================
    print("\n===== 技能命中：宁可漏判，不可劫持 =====")
    chk(sk.match("迈克斯 回款画像")["key"] == "customer_profile", "「回款画像」命中")
    chk(sk.match("库存预警")["key"] == "stock_alert", "「库存预警」命中")
    chk(sk.match("哪家供应商拖期")["key"] == "supplier_review", "「哪家供应商拖期」命中")
    chk(sk.match("2026-063 项目体检")["key"] == "project_check", "「项目体检」命中")

    # 这几条**绝不能**被劫持
    for q in ["查询一下所有的待审批的待办?", "采购未到货", "这月销售额多少",
              "帮我看看库存", "客户是谁", "供应商列表"]:
        chk(sk.match(q) is None, f"不劫持普通提问：{q}")

    print("\n===== 技能可见性按菜单 =====")
    av = {s["key"] for s in sk.available({"warehouse"})}
    chk("stock_alert" in av and "customer_profile" not in av,
        f"只有仓库权限时只看得到库存预警：{sorted(av)}")
    av = {s["key"] for s in sk.available({"finance", "sales", "purchase_mgmt", "warehouse", "list"})}
    chk(len(av) == 4, f"全权限看得到 4 个技能：{sorted(av)}")

    # ================= 记忆：别名 =================
    print("\n===== 别名：整词替换，绝不改写用户原意 =====")
    async with SessionLocal() as db:
        chk(await mem.expand(db, "南京那个项目怎么样") == "南京那个项目怎么样",
            "没配别名时原样返回")
        await mem.set_alias(db, "南京那个", "2026-063")
        chk(await mem.expand(db, "南京那个项目怎么样") == "2026-063项目怎么样",
            f"配了别名才替换：{await mem.expand(db, '南京那个项目怎么样')}")
        chk(await mem.expand(db, "北京那个项目") == "北京那个项目",
            "没命中的词一个字都不改")

    # ================= 记忆：会话焦点 =================
    print("\n===== 会话焦点：只认明确出现过的编号，不猜 =====")
    f = mem.focus_from_history([{"role": "user", "content": "2026-063 进度怎么样"},
                                {"role": "assistant", "content": "进行中"}])
    chk(f.get("project") == "2026-063", f"抓到当前话题：{f}")
    chk(mem.focus_from_history([{"role": "user", "content": "有多少待办"}]) == {},
        "抓不到就返回空——塞一个猜错的焦点比没有更糟")
    chk("2026-063" in mem.focus_hint(f), "焦点会作为提示注入")
    chk(mem.focus_hint({}) == "", "没焦点就不注入")

    # ================= RAG：口径召回 =================
    print("\n===== 口径召回：不索引业务数据，只索引规则 =====")
    async with SessionLocal() as db:
        r = await mem.recall(db, "发货款应收是什么意思")
        chk(r and "待发货" in r[0], f"问发货款 → 召回「先确认货发没发」：{r[0][:40] if r else '无'}")
        r = await mem.recall(db, "合同额为 0 会怎样")
        chk(r and "假亏损" in r[0], f"问合同额 0 → 召回假亏损：{r[0][:30] if r else '无'}")
        r = await mem.recall(db, "尾款催办扫不到")
        chk(r and "balance_date" in r[0], "问尾款催办 → 召回到期日口径")
        chk(await mem.recall(db, "今天天气怎么样") == [],
            "无关问题不硬塞口径")

        # 业务可自己维护，不用改代码
        await mem.save_knowledge(db, [{"q": "测试词", "a": "这是业务自己加的口径"}])
        r = await mem.recall(db, "测试词是什么")
        chk(r == ["这是业务自己加的口径"], f"业务加的口径能被召回：{r}")
        await mem.save_knowledge(db, [])
        chk(len(await mem.knowledge(db)) == len(mem.DEFAULT_KNOWLEDGE),
            "清空后退回出厂口径，不会变成没有口径")

    # ================= 模型路由 =================
    print("\n===== 模型路由：分析用大模型，单点查询用小模型 =====")
    cfg = {"model": "big", "model_fast": "small"}
    for q in ["诺朋这家供应商靠谱吗", "为什么这个月毛利低", "采购未到货全部列举", "对比一下"]:
        chk(_route_model(q, cfg, None) == "big", f"「{q}」→ 大模型")
    for q in ["待开票", "不锈钢焊丝还有多少库存", "尾款到期"]:
        chk(_route_model(q, cfg, None) == "small", f"「{q}」→ 小模型")
    chk(_route_model("待开票", cfg, "big") == "big", "用户显式指定时永远不覆盖他的选择")
    chk(_route_model("待开票", {"model": "only"}, None) == "only", "没配小模型就退回默认")

    # ================= ReAct：重复调用硬拦 =================
    print("\n===== 同工具同参数不得重复调用（硬拦，不靠提示词）=====")
    async with SessionLocal() as db:
        me = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalar_one()
        seen: set = set()
        r1 = await _run_tool("po_arrival_overdue", {}, db, me, seen)
        chk("error" not in r1, "第一次正常返回")
        r2 = await _run_tool("po_arrival_overdue", {}, db, me, seen)
        chk("error" in r2 and "已经做过" in r2["error"],
            f"同参数第二次被拦：{r2.get('error')}")
        r3 = await _run_tool("po_arrival_overdue", {"limit": 200}, db, me, seen)
        chk("error" not in r3, "换了参数不算重复，照常执行")
        r4 = await _run_tool("po_arrival_overdue", {}, db, me, None)
        chk("error" not in r4, "不传 seen 时不拦（兼容旧调用）")

    # ================= 技能执行 =================
    print("\n===== 技能真的能跑出结果 =====")
    async with SessionLocal() as db:
        me = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalar_one()
        p = models.Project(code="SK-001", name="技能测试机", is_deleted=False)
        db.add(p); await db.flush()
        db.add(models.SalesLedger(project_id=p.id, sales_uid=me.id, customer="技能客户有限公司",
                                  amount=200000, ship_receivable=80000, balance=0,
                                  balance_date="", order_state="approved"))
        db.add(models.Shipment(project_id=p.id, status="pending",
                               receiver_name="", receiver_phone="", receiver_addr=""))
        db.add(models.WhMaterial(code="SK-M1", name="技能物料", unit="个",
                                 safety_stock=100, init_stock=10))
        await db.commit()

        t = await sk.run(db, me, sk._SKILLS["customer_profile"], "技能客户 回款画像")
        chk("技能客户有限公司" in t, "客户画像出得来")
        chk("待发货" in t, f"**点出「货没发出去，这钱还不到该收的时候」**：{t[:0] or ''}")
        chk("¥200,000" in t or "¥80,000" in t, "金额写原始数值")

        t = await sk.run(db, me, sk._SKILLS["stock_alert"], "库存预警")
        chk("技能物料" in t and "90" in t, f"库存预警算出缺口 100-10=90：{t[:60]}")

        t = await sk.run(db, me, sk._SKILLS["project_check"], "SK-001 项目体检")
        chk("SK-001" in t and "技能客户" in t, "项目体检出得来")

        # 找不到时要给下一步，不是空手而归
        t = await sk.run(db, me, sk._SKILLS["customer_profile"], "查无此客户 回款画像")
        chk("没有" in t or "确认" in t, f"查不到时给提示：{t[:40]}")
        t = await sk.run(db, me, sk._SKILLS["customer_profile"], "回款画像")
        chk("要看哪个客户" in t, "没说客户名时反问，不瞎猜")

    print("\n" + "=" * 58)
    if FAIL:
        print(f"❌ {len(FAIL)} 条失败：")
        for f in FAIL: print("   -", f)
        sys.exit(1)
    print("✅ 全部通过")

asyncio.run(main())
