"""🆕 2026-10-01 老板：「AI助手这一块，ReAct兜底没有生效。你看看最近大家问的问题」。

翻生产近 3 天的问答，长尾问题几乎都是**一两轮就放弃**：
  · 赵仁辉「方步森销售额」「李昌奇销售额」→ find_entity 只认项目/客户/供应商/物料 → 「系统里查不到这个人」
  · 杨坛「销售员业绩排名」「今年业务额销售排名」→ sales_summary 只按月 → 「系统里没有销售员字段」
    （台账上明明有 sales_uid）
  · 「1 个项目没填签订日期」（两次）→ sales_summary 只给个数 → 「查不到是哪个」
  · 王芹「王易 未到货」→ get_supplier `.limit(1)` 取到了两条同名档案里错的那条 → 「查不到任何采购记录」
  · 李新新（采购兼仓库）「9月份销售额」→ 「没有销售接口，请到 ERP 的销售模块看」——是他没权限，而这就是 ERP
  · 王芹 09-30 07:40「我手上的活」→ 模型自己指出「工具返回的日期是 09-29」：容器 UTC，早上 8 点前差一天

根因两层：① 提示词铁律写着「工具没返回的就说查不到」，没有任何「换路」要求；
         ② 工具本身缺维度（人、按销售员、没签订日期的清单、同名供应商）。
另外 4 轮跑满会抛「轮次超限」整条降级，换路越多越容易撞上 —— 改成 5 轮、最后一轮只许作答。
"""
import asyncio, json, os, sys, tempfile
from datetime import datetime, timedelta

tmp = tempfile.mkdtemp(prefix="react")
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
from app.agent import tools_entity as te, clock
from app.routers import agent_router as ar

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def _u(uname: str) -> models.User:
    async with SessionLocal() as db:
        return (await db.execute(select(models.User)
                                 .where(models.User.username == uname))).scalars().unique().one()


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

        async def mkuser(uname, full, codes, menus):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "full_name": full,
                "role_ids": [rid[x] for x in codes]})
            assert r.status_code == 200, r.text
            m = await c.put(f"/api/admin/users/{r.json()['id']}/menus", headers=H, json={"menus": menus})
            assert m.status_code == 200, m.text

        await mkuser("rf_li", "李昌奇", ["sales"], ["catalog", "list", "sales", "messages"])
        await mkuser("rf_fang", "方步森", ["sales"], ["catalog", "list", "sales", "messages"])
        await mkuser("rf_wq", "王芹", ["buyer"], ["catalog", "list", "purchase_mgmt", "messages"])
        await mkuser("rf_lxx", "李新新", ["buyer", "warehouse"],
                     ["catalog", "list", "purchase_mgmt", "warehouse", "messages"])
        await mkuser("rf_lead", "杨坛", ["sales", "sales_lead"], ["catalog", "list", "sales", "messages"])
        u_li, u_fang, u_wq = await _u("rf_li"), await _u("rf_fang"), await _u("rf_wq")
        u_lxx, u_lead, u_admin = await _u("rf_lxx"), await _u("rf_lead"), await _u("admin")

        # ── 台账：李昌奇 2 单（一单没填签订日期）、方步森 1 单 ──
        yr = str(clock.today().year)
        async with SessionLocal() as db:
            for code, owner, amt, sign in (("2026-971", u_li, 300000, f"{yr}-03-05"),
                                           ("2026-972", u_li, 50000, ""),
                                           ("2026-973", u_fang, 120000, f"{yr}-04-10")):
                p = models.Project(code=code, name=f"{code} 设备", status="进行中",
                                   extra={te.SIGN_KEY: sign} if sign else {})
                db.add(p)
                await db.flush()
                db.add(models.SalesLedger(project_id=p.id, customer=f"{code}客户", amount=amt,
                                          sales_uid=owner.id, order_state="approved"))
            # ── 两条同名供应商档案（复刻生产「无锡王易不锈钢」315/340）──
            s1 = models.Supplier(name="无锡王易不锈钢有限公司")
            s2 = models.Supplier(name="无锡王易不锈钢有限公司")
            s3 = models.Supplier(name="王易五金")
            db.add_all([s1, s2, s3])
            await db.flush()
            tmr = (clock.today() + timedelta(days=1)).isoformat()
            ago = (clock.today() - timedelta(days=3)).isoformat()
            db.add(models.PurchaseItem(supplier_id=s1.id, item_name="不锈钢板", buyer_id=u_lxx.id,
                                       expected_arrival=ago))
            for k in range(3):
                db.add(models.PurchaseItem(supplier_id=s2.id, item_name=f"方管{k}", buyer_id=u_wq.id,
                                           expected_arrival=tmr, po_no="TH-W-1"))
            await db.commit()

        print("\n=== 1. find_entity 认得出人 ===")
        async with SessionLocal() as db:
            f = await te.find_entity(db, u_admin, "方步森")
            ppl = f["matches"].get("person") or []
            chk([x["person"] for x in ppl] == ["方步森"], f"「方步森」认成同事（改之前四类全 0）→ {ppl}")
            chk(ppl and "销售" in ppl[0]["roles"], f"带上岗位，模型才知道去查销售额 → {ppl[:1]}")
            chk(ppl and set(ppl[0]) == {"id", "person", "roles"}, f"只给姓名和岗位，不带账号信息 → {ppl[:1]}")

        print("\n=== 2. sales_summary：按销售员查 / 排名 / 没签订日期的清单 ===")
        async with SessionLocal() as db:
            r = await te.sales_summary(db, u_admin, by="sales")
            rank = [(x["salesperson"], x["amount"]) for x in r["items"] if x["salesperson"] in ("李昌奇", "方步森")]
            chk(rank == [("李昌奇", 300000.0), ("方步森", 120000.0)],
                f"按销售员排名（今年、按签订日期），没签订日期的不进 → {rank}")
            nsi = [x["project"] for x in r["no_sign_date_items"]]
            chk("2026-972" in nsi, f"没填签订日期的项目列出来了（改之前只有个数）→ {nsi}")
            r = await te.sales_summary(db, u_admin, sales="李昌奇")
            codes = [p["project"] for m in r["months_detail"] for p in m["projects"]]
            chk(codes == ["2026-971"] and r["sales_filter"] == "李昌奇",
                f"sales=李昌奇 只看他的 → {codes} / {r.get('sales_filter')}")
            r = await te.sales_summary(db, u_admin, sales="李昌奇", months=1)
            chk(r["this_year_amount"] == 300000.0 and r["this_year_count"] == 1,
                f"全年合计单独给，不受 months 截断（改之前模型拿 6 个月加总当「今年」）→ "
                f"{r['this_year_amount']} / {r['this_year_count']}")
            from app.agent import render as _rd
            t = _rd.table(await te.sales_summary(db, u_admin), plan={"list": "no_sign_date_items"})
            chk("2026-972" in t and "月份" not in t,
                f"编排块 list=no_sign_date_items 渲染的是那组清单，不是月度表 → {t[:80]!r}")
            r = await te.sales_summary(db, u_fang, sales="李昌奇")
            chk("error" in r and "只能看自己" in r["error"], f"销售查别人的销售额 → 拒 {r}")
            r = await te.sales_summary(db, u_fang, by="sales")
            chk([x["salesperson"] for x in r["items"]] == ["方步森"], f"销售看排名只有自己那一行 → {r['items']}")
            r = await te.sales_summary(db, u_lead, by="sales")
            chk({x["salesperson"] for x in r["items"]} >= {"李昌奇", "方步森"}, "销售主管看全部排名")
            r = await te.sales_summary(db, u_admin, sales="不存在的人")
            chk("error" in r and "find_entity" in r["error"], f"人名对不上时引导去 find_entity → {r}")
            via = await ar._run_tool_inner("sales_summary", {"by": "sales"}, db, u_admin)
            chk(via.get("by") == "sales", "经 _run_tool_inner 能把 by/sales 参数传进去")

        print("\n=== 3. get_supplier：同名档案合并、在途也算、多家不替人挑 ===")
        async with SessionLocal() as db:
            g = await te.get_supplier(db, u_wq, "无锡王易不锈钢有限公司")
            chk(g["found"] and g["purchase_items"] == 3,
                f"王芹（受限采购员）看到自己在第二条档案下的 3 条（改之前取到第一条 → 0）→ {g['purchase_items']}")
            chk(g["in_transit_count"] == 3 and g["count"] == 0,
                f"明天到的算「在途」，不是「没有未到货」→ 在途 {g['in_transit_count']} / 超期 {g['count']}")
            chk(g["supplier_records"] == 2, "提示同名档案有 2 条")
            ga = await te.get_supplier(db, u_admin, "无锡王易不锈钢有限公司")
            chk(ga["purchase_items"] == 4 and ga["count"] == 1,
                f"管理员看两条档案合计 4 条，其中 1 条已超期 → {ga['purchase_items']} / {ga['count']}")
            gm = await te.get_supplier(db, u_admin, "王易")
            chk(not gm["found"] and len(gm.get("candidates") or []) == 2,
                f"「王易」匹配两家不同名的 → 给候选，不替人挑 → {gm.get('candidates')}")
            gl = await te.get_supplier(db, u_lxx, "王易五金")
            chk(gl["found"] and gl["purchase_items"] == 0 and "你名下没有" in (gl.get("hint") or ""),
                f"受限采购员名下没有时明说「你名下没有」，不说「系统里查不到」→ {gl.get('hint')}")

        print("\n=== 4. 北京时间 ===")
        chk(clock.cn_date(datetime(2026, 9, 29, 23, 40)).isoformat() == "2026-09-30",
            "UTC 09-29 23:40 = 北京 09-30（早上 7:40 那条问答的情形）")
        import pathlib
        left = [str(p) for p in pathlib.Path("app/agent").rglob("*.py")
                if p.name != "clock.py" and "date.today()" in p.read_text(encoding="utf-8")]
        chk(not left, f"智能体代码里不再有 date.today()（容器是 UTC）→ {left}")

        print("\n=== 5. 提示词：换路 + 权限说法 ===")
        sp = ar._SYSTEM_PROMPT
        chk("先换路再下结论" in sp and "至少换一次路" in sp, "提示词有「换路」要求")
        chk("工具没返回的就说\"系统里查不到\"" not in sp, "旧铁律「工具没返回就说查不到」已改")
        chk("这个系统就是公司的 ERP" in sp, "不许把人支到「ERP / 别的系统」")
        hint = ar._scope_hint(ar._allowed_tools(u_lxx))
        chk("销售额" in hint and "没有这部分权限" in hint,
            f"李新新（采购兼仓库）的提示里写明他看不到销售额 → {hint[:60]}")
        chk(ar._scope_hint(ar._allowed_tools(u_admin)) == "", "管理员什么都看得到，不加这段")

        print("\n=== 6. 轮次：跑满不再整条降级，最后一轮只许作答 ===")
        choices: list[str] = []

        async def always_tools(messages, model, cfg, tools, max_tokens=700, tool_choice="auto"):
            choices.append(tool_choice)
            if tool_choice == "none":
                yield {"choices": [{"delta": {"content": "**换了几条路，能查到的最接近的是按月销售额。**"}}]}
                yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                return
            n = len(choices)
            yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": f"c{n}", "function": {
                "name": "find_entity", "arguments": json.dumps({"q": f"词{n}"})}}]}}]}
            yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}

        orig = ar._llm_stream
        ar._llm_stream = always_tools
        try:
            out, err = "", None
            try:
                async for kind, payload in ar._chat_stream(
                        "方步森今年的业绩", [], "m", {"api_key": "k", "base_url": "http://x", "model": "m"},
                        u_admin):
                    if kind == "done":
                        out = payload["text"]
            except RuntimeError as e:
                err = str(e)
            chk(err is None and "最接近" in out,
                f"模型一直想调工具也能收尾作答，不抛「轮次超限」→ err={err} out={out[:30]!r}")
            chk(choices[-1] == "none" and choices.count("none") == 1 and len(choices) == ar._MAX_ROUNDS,
                f"前 {ar._MAX_ROUNDS - 1} 轮可调工具，最后一轮 tool_choice=none → {choices}")
        finally:
            ar._llm_stream = orig


asyncio.run(main())
print("\n" + ("全部通过 ✅" if not FAIL else f"{len(FAIL)} 条失败 ❌"))
for m in FAIL:
    print(" -", m)
sys.exit(1 if FAIL else 0)
