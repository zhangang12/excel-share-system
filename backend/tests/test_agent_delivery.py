"""🆕 项目交期跟进：多项目命中 + 卡点判定 + 交期看板。

本测试锁四件事：

 1. **模糊命中多个项目时一个都不能少**——生产上项目编号大量带字母后缀
    （2026-071A / 071B、043B~043E），用户说「071」时 `%071%` 同时命中两个。
    原实现是 `.order_by(id.desc()).limit(1)`，**悄悄只留了 B**，
    而用户看到的是一份看起来完整的分析。这是本测试最主要的目标。
 2. **精确编号仍然只出一个**——修了①不能反过来把精确查询也变成一堆。
 3. **「生产没有截止日期」必须被报出来**——生产库里 46 个在建项目
    有 39 个生产侧没有任何截止日期。这种情况下正确答案是「算不出来，这是盲区」，
    而不是不报风险（不报=用户以为没问题）。
 4. **已发货待收尾的不算交期风险**——2026-008 这种「货发完了、状态还挂进行中」
    的项目已过期 181 天，混进 overdue 会把真正要盯的挤下去。

5. **交期问题必须走 ReAct，不能被技能劫持**——技能是「命中就不进 ReAct」，
    做成技能等于把 AI 分析绕开，只剩一段套话加一张表。管理层要的是
    「把项目所有数据串起来分析」，判断得归模型。
 6. **明细格式**——days_left 不能把 -55 直接甩给人看；数据字段里不许带 markdown
    （外层再包一层就成了 `卡在**xx（**yy**）**`，markdown 直接裂开）。
"""
import asyncio, os, sys, tempfile
from datetime import date, timedelta

tmp = tempfile.mkdtemp(prefix="delivery")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from app.database import engine, SessionLocal, Base
from app import models
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app.agent import tools_entity as te, skills as sk

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)
    else: print("  ok:", m)


def d(n: int) -> str:
    """今天 +n 天。**不写死日期** —— 写死的话这个测试到了那天就自己变绿/变红。"""
    return (date.today() + timedelta(days=n)).isoformat()


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    async with SessionLocal() as db:
        admin = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()

        # ── 造数：照抄生产上的真实形态 ──────────────────────────────
        # 071A / 071B：同名客户、同一交货日，只有采购数不同（生产上就是这样）
        pa = models.Project(code="2026-071A", name="200L双行星分散混合机",
                            status="进行中", extra={te.DELIVER_KEY: d(25)})
        pb = models.Project(code="2026-071B", name="200L提升式压料机",
                            status="进行中", extra={te.DELIVER_KEY: d(25)})
        # 已发货但状态没收尾，且早就过了交货日
        pc = models.Project(code="2026-008", name="半自动灌装封尾机",
                            status="进行中", extra={te.DELIVER_KEY: d(-181)})
        # 生产填了截止日，而且**晚于交货日** → 按计划就交不了
        pd_ = models.Project(code="2026-090", name="晚于交货日的项目",
                             status="进行中", extra={te.DELIVER_KEY: d(10)})
        # 没填交货日期
        pe = models.Project(code="2026-091", name="没填交货日期的项目", status="进行中")
        db.add_all([pa, pb, pc, pd_, pe]); await db.flush()

        for p, n_po in ((pa, 5), (pb, 16)):
            db.add(models.SalesLedger(project_id=p.id, customer="浙江舒康科技有限公司",
                                      amount=300000 if p is pa else 0))
            db.add(models.DeptOrder(project_id=p.id, dept="design", status="done"))
            db.add(models.DeptOrder(project_id=p.id, dept="electric",
                                    status="in_progress", due_date=d(5)))
            # 生产单没有 due_date —— 生产库里 46 个在建项目全是这样
            po = models.DeptOrder(project_id=p.id, dept="produce", status="in_progress")
            db.add(po); await db.flush()
            # ⚠️ produce_group_tasks 对 (order_id, group) 有唯一约束，
            #    order_id 必须是各自生产单的真实 id，不能图省事都写 0
            for g in ("sheetmetal", "assembly", "sealing"):
                db.add(models.ProduceGroupTask(order_id=po.id, project_id=p.id,
                                               group=g, status="dispatched"))
            for i in range(n_po):
                db.add(models.PurchaseItem(supplier_id=1, project_code=p.code,
                                           item_name=f"件{i}", expected_arrival=d(3)))
            db.add(models.Shipment(project_id=p.id, status="pending"))

        # 2026-008：部门单全作废 + 已发货
        for dept in ("design", "electric", "produce"):
            db.add(models.DeptOrder(project_id=pc.id, dept=dept, status="voided"))
        db.add(models.Shipment(project_id=pc.id, status="shipped"))

        # 2026-092：**还在做**且已过交货日 —— 交期看板最核心的一档。
        # 生产单填了截止日，所以它不进 no_produce_due；也没有采购，不影响卡采购的计数。
        pf = models.Project(code="2026-092", name="已过交货日还在做的项目",
                            status="进行中", extra={te.DELIVER_KEY: d(-12)})
        db.add(pf); await db.flush()
        db.add(models.DeptOrder(project_id=pf.id, dept="design", status="done"))
        db.add(models.DeptOrder(project_id=pf.id, dept="electric",
                                status="in_progress", due_date=d(-3)))
        db.add(models.DeptOrder(project_id=pf.id, dept="produce",
                                status="in_progress", due_date=d(-1)))

        # 2026-090：生产截止晚于交货日
        db.add(models.DeptOrder(project_id=pd_.id, dept="design", status="done"))
        db.add(models.DeptOrder(project_id=pd_.id, dept="electric", status="done"))
        db.add(models.DeptOrder(project_id=pd_.id, dept="produce",
                                status="in_progress", due_date=d(20)))
        await db.commit()

        # ═══════════ 1. 多命中：一个都不能少 ═══════════
        print("===== 1. 模糊命中多个项目 =====")
        r = await te.get_project(db, admin, "071")
        chk(r.get("matched_count") == 2, f"「071」命中 2 个（实际 {r.get('matched_count')}）")
        codes = {p["project"] for p in r.get("projects", [])}
        chk(codes == {"2026-071A", "2026-071B"},
            f"A 和 B 都在结果里（实际 {sorted(codes)}）")
        chk("每个都要讲到" in (r.get("note") or ""),
            "带上「每个都要讲到」的硬约束，防模型只挑一个说")

        print("\n===== 2. 精确编号仍然只出一个 =====")
        r1 = await te.get_project(db, admin, "2026-071A")
        chk(r1.get("project") == "2026-071A" and "projects" not in r1,
            "精确编号 → 单个结果，没被改成一堆")

        print("\n===== 3. 卡点与风险判定 =====")
        a = next(p for p in r["projects"] if p["project"] == "2026-071A")
        b = next(p for p in r["projects"] if p["project"] == "2026-071B")
        chk(a["days_left"] == 25, f"剩余天数按交货日算（{a['days_left']}）")
        chk(a["purchase_pending_count"] == 5 and b["purchase_pending_count"] == 16,
            "两个项目的采购未到货数各算各的，没串")
        chk(any("生产没有截止日期" in x for x in a["risks"]),
            "生产没截止日期 → 报成盲区（不报=用户以为没问题）")
        chk(any("电工未完成" in x for x in a["blockers"]), "电工未完成进卡点")
        chk(any("生产组未完工" in x for x in a["blockers"]), "生产组未完工进卡点")

        print("\n===== 4. 生产截止晚于交货日 =====")
        r90 = await te.get_project(db, admin, "2026-090")
        chk(any("晚于交货日" in x for x in r90["risks"]),
            f"生产截止 {d(20)} > 交货 {d(10)} → 明确报「按计划就交不了」")

        print("\n===== 5. 已发货待收尾：不算交期风险 =====")
        r8 = await te.get_project(db, admin, "2026-008")
        chk(r8["shipped_not_closed"] is True, "识别为「已发货待收尾」")
        chk("已发货" in (r8["blocked_at"] or ""),
            f"不再报成「设计还没下单」（实际：{r8['blocked_at']}）")

        print("\n===== 6. 交期看板 =====")
        board = await te.project_progress(db, admin)
        s = board["summary"]
        chk(s["shipped_not_closed"] == 1, "已发货待收尾单独一档")
        chk(s["overdue"] == 1,
            f"overdue 只数还在做的那 1 个，不含已发货待收尾的（实际 {s['overdue']}）"
            f" —— 虚高的告警没人信")
        chk(s["no_produce_due"] == 2, f"2 个项目生产无截止日期（实际 {s['no_produce_due']}）")
        chk(s["no_deliver_date"] == 1, "没填交货日期的单独计数")
        chk(s["blocked_by_purchase"] == 2, "2 个项目卡在采购")
        chk(board["items"][0]["project"] == "2026-092",
            f"最急的是还在做且已过期的那个（实际 {board['items'][0]['project']}）")
        chk(board["items"][-1]["project"] == "2026-008",
            "已发货待收尾排最后，不占「最急」的位置")
        chk({i["project"] for i in board["items"]} ==
            {"2026-071A", "2026-071B", "2026-008", "2026-090", "2026-091", "2026-092"},
            "**一个都没丢**（含没填交货日期的）")

        print("\n===== 7. within_days 过滤：已过期一律带上 =====")
        b7 = await te.project_progress(db, admin, within_days=7)
        got = {i["project"] for i in b7["items"]}
        chk("2026-091" not in got, "没交货日期的不进「N 天内」")
        chk("2026-071A" not in got, "25 天后交货的不进 7 天内")
        chk("2026-008" in got, "已过期的即使超出 within_days 也要带上（最急的就是它们）")

        print("\n===== 8. 交期问题必须走 ReAct（不能被技能劫持）=====")
        # 技能命中就不进 ReAct。交期这类要的是分析，不是套话模板，
        # 所以这些问法**都必须 match 不到技能**，交给模型带着 project_progress 去答。
        for q in ("项目进度跟进", "哪些项目快到期了", "交期风险",
                  "2026-071 卡在哪", "071 还剩多少天", "生产进度监控"):
            chk(sk.match(q) is None, f"「{q}」不被技能劫持，走 ReAct")
        # 别的技能不能被误伤
        chk((sk.match("库存预警") or {}).get("key") == "stock_alert", "库存预警仍走技能")
        chk(sk.match("查询一下所有的待审批的待办") is None, "老的防劫持用例仍成立")

        print("\n===== 9. 明细格式 =====")
        from app.agent import render as rd
        board = await te.project_progress(db, admin)
        out = rd.table(board, plan={"group": "urgency",
                                    "fields": ["project", "name", "deliver_date",
                                               "days_left", "blocked_at",
                                               "purchase_pending"]})
        chk("已过 " in out and "剩 " in out,
            "days_left 渲染成「已过 N 天」/「剩 N 天」，不是裸的 -55")
        chk("-55" not in out and "· -" not in out, "没有裸负数漏出去")
        chk("**已过交货日**（1）" in out, "按紧急度分组并带条数")
        chk(out.index("已过交货日") < out.index("已发货 · 只差收尾"),
            "已过交货日排在已发货待收尾之前（分组不能打乱工具排好的优先级）")
        chit = [l for l in out.split("\n") if l.startswith("**")]
        chk(len(chit) >= 2, f"至少分出两档（实际 {len(chit)} 档：{chit}）")
        # 数据里带 markdown 会把行拆坏：`卡在**生产未完成（**没有截止日期**）**`
        for it in board["items"]:
            for f in ("blocked_at", "name"):
                chk("**" not in str(it.get(f) or ""), f"{it['project']} 的 {f} 不带 markdown")
            for x in it["blockers"] + it["risks"]:
                chk("**" not in x, f"{it['project']} 的卡点/风险文案不带 markdown")
        chk(all(l.count("·") <= 6 for l in out.split("\n") if l.startswith("- ")),
            "单行字段数受控，手机上一行放得下")

    await engine.dispose()
    print("\nPASSED" if not FAIL else f"\n{len(FAIL)} FAILURES")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
