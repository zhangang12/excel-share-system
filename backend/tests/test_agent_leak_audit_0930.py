"""🆕 2026-09-30 智能体越权审计（老板：「审计一下当前AI的回答是否有存在越权的情况」）。

做法：在生产库上以每个在用非管理层账号的身份，把他能用的工具全跑一遍，逐行核对数据归属
（脚本只读，不写库）。24 个账号、295 次调用，审出两类 09-23 收口时漏掉的：

  1. 待填收货人（shipment_receiver）不分人 —— 6 个普通销售每人都能问出全公司 80~90 张
     待填发货单，带别人的客户名，以及该客户历史收货人的姓名和电话。
  2. 模糊搜索（find_entity）的项目命中带客户名 —— 仓库/设计/装配等 15 个没有看钱权限的账号
     搜编号就能顺带看到客户（网页项目目录不显示客户；get_project 09-23 已脱敏，这里漏了）。
  顺带：项目快照里的「收货人」同理只给物流和能看这张台账的人。

第二批（代码审计 + 生产账号配置核查）：
  3. 智能体不认「隐藏页签」—— 王芹（财务）在网页上被藏了资金面板/项目毛利/请款审批，
     09-28 问智能体「九月份销售额」照样拿到 ¥83.5 万，请款审批卡也照样给她亮按钮。
  4. 线索、待审订单对财务全量 —— 网页线索页/待审列表只给销售部，杨倩、王芹能问出全部。
  5. 旧版 project_status 的台账段只看菜单不看归属 —— 方步森（销售兼采购）能看别人项目的合同额。

口径：
  · 待填收货人：物流、管理层/销售主管/财务看全部；普通销售只看自己台账的项目，
    历史收货人候选也只从自己台账的发货单里取。
  · 客户名 / 收货人：与合同额同级，走 perm.can_see_ledger；物流另给收货人（送货要用）。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="leak0930")
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
from app.agent import tools_entity as te, tools_sales as ts
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

        async def login(u, p="pass123"):
            rr = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert rr.status_code == 200, rr.text
            return {"Authorization": f"Bearer {rr.json()['access_token']}"}

        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(name, codes, menus):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123", "full_name": name,
                "role_ids": [rid[x] for x in codes]})
            assert r.status_code == 200, f"{name}: {r.text}"
            m = await c.put(f"/api/admin/users/{r.json()['id']}/menus", headers=H,
                            json={"menus": menus})
            assert m.status_code == 200, m.text

        await mkuser("lk_wh", ["warehouse"], ["catalog", "list", "warehouse", "messages"])
        await mkuser("lk_sales", ["sales"], ["catalog", "list", "sales", "messages"])
        await mkuser("lk_sales2", ["sales"], ["catalog", "list", "sales", "messages"])
        await mkuser("lk_slead", ["sales", "sales_lead"], ["catalog", "list", "sales", "messages"])
        await mkuser("lk_fin", ["finance"], ["catalog", "list", "finance", "messages"])
        await mkuser("lk_log", ["logistics"], ["catalog", "list", "logistics", "messages"])
        u_wh, u_s, u_s2 = await _u("lk_wh"), await _u("lk_sales"), await _u("lk_sales2")
        u_sl, u_fin, u_log = await _u("lk_slead"), await _u("lk_fin"), await _u("lk_log")

        # ── 造数据 ──
        #   2026-951：lk_sales 的，待填收货人，客户「苏州恒达」
        #   2026-952：lk_sales2 的，待填收货人，客户「无锡联科」
        #   2026-953：lk_sales2 的，**已填**收货人，客户也是「苏州恒达」（同一客户换过销售）
        pids = {}
        for code in ("2026-951", "2026-952", "2026-953"):
            pr = await c.post("/api/projects", headers=H, json={"code": code, "name": f"{code} 设备"})
            assert pr.status_code == 200, pr.text
            pids[code] = pr.json()["id"]
        async with SessionLocal() as db:
            for code, cust, owner in (("2026-951", "苏州恒达", u_s), ("2026-952", "无锡联科", u_s2),
                                      ("2026-953", "苏州恒达", u_s2)):
                db.add(models.SalesLedger(project_id=pids[code], customer=cust, amount=100000,
                                          sales_uid=owner.id, order_state="approved"))
            for code, name, phone in (("2026-951", None, None), ("2026-952", None, None),
                                      ("2026-953", "王建国", "13900001111")):
                sh = (await db.execute(select(models.Shipment).where(
                    models.Shipment.project_id == pids[code]))).scalar_one_or_none()
                if sh is None:
                    sh = models.Shipment(project_id=pids[code])
                    db.add(sh)
                sh.status = "shipped" if name else "pending"
                sh.receiver_name, sh.receiver_phone = name, phone
            await db.commit()

        def codes(res):
            return sorted(x["project_code"] for x in res["items"]
                          if x["project_code"] in ("2026-951", "2026-952", "2026-953"))

        print("\n=== 1. 待填收货人：普通销售只看自己的 ===")
        async with SessionLocal() as db:
            r_s = await ts.tool_shipment_receiver(db, u_s)
            r_s2 = await ts.tool_shipment_receiver(db, u_s2)
            chk(codes(r_s) == ["2026-951"],
                f"lk_sales 只看到自己的 951（改之前还能看到 952「无锡联科」）→ {codes(r_s)}")
            chk(codes(r_s2) == ["2026-952"], f"lk_sales2 只看到自己的 952 → {codes(r_s2)}")
            mine = next(x for x in r_s["items"] if x["project_code"] == "2026-951")
            chk(mine["customer"] == "苏州恒达", "自己的项目照常带客户名")
            chk(not mine.get("suggest"),
                f"历史收货人候选不从别人（lk_sales2 的 953）的发货单里带姓名电话 → {mine.get('suggest')}")
            for name, uo in (("物流", u_log), ("销售主管", u_sl), ("财务", u_fin)):
                rr = await ts.tool_shipment_receiver(db, uo)
                chk(codes(rr) == ["2026-951", "2026-952"], f"{name}看全部 → {codes(rr)}")
            rl = await ts.tool_shipment_receiver(db, u_log)
            hint = next(x for x in rl["items"] if x["project_code"] == "2026-951").get("suggest") or {}
            chk(hint.get("name") == "王建国",
                f"物流填 951 时照常能拿到同一客户的历史收货人做候选（功能没被堵死）→ {hint}")
            # 走智能体真实出口（菜单门控 + _cap）也是同一个结果
            via = await ar._run_tool_inner("shipment_receiver", {}, db, u_s)
            chk(codes(via) == ["2026-951"], f"经 _run_tool_inner 调用同口径 → {codes(via)}")

        print("\n=== 2. 模糊搜索的项目命中：没有看钱权限的不带客户名 ===")
        async with SessionLocal() as db:
            def cust_of(res, code):
                return next((x.get("customer") for x in (res["matches"].get("project") or [])
                             if x["code"] == code), None)
            f_wh = await te.find_entity(db, u_wh, "2026-95")
            chk(cust_of(f_wh, "2026-951") == "",
                f"仓库搜「2026-95」：命中项目，但客户名为空（改之前是「苏州恒达」）→ {cust_of(f_wh, '2026-951')!r}")
            f_s = await te.find_entity(db, u_s, "2026-95")
            chk(cust_of(f_s, "2026-951") == "苏州恒达", "负责销售看得到自己项目的客户")
            chk(cust_of(f_s, "2026-952") in (None, ""),
                f"看不到别人项目的客户 → {cust_of(f_s, '2026-952')!r}")
            f_sl = await te.find_entity(db, u_sl, "2026-95")
            chk(cust_of(f_sl, "2026-952") == "无锡联科", "销售主管照常看得到")

        print("\n=== 3. 项目快照里的收货人：物流/能看台账的人才给 ===")
        async with SessionLocal() as db:
            def recv(g):
                return (g.get("projects") or [g])[0].get("shipment_receiver")
            g_wh = await te.get_project(db, u_wh, "2026-953")
            chk(recv(g_wh) == "", f"仓库问 953：收货人不给 → {recv(g_wh)!r}")
            g_log = await te.get_project(db, u_log, "2026-953")
            chk(recv(g_log) == "王建国", f"物流问 953：给（送货要用）→ {recv(g_log)!r}")
            g_s2 = await te.get_project(db, u_s2, "2026-953")
            chk(recv(g_s2) == "王建国", "负责销售问自己的 953：给")

        print("\n=== 4. 隐藏页签：藏了资金面板/毛利的财务，智能体也不给钱 ===")
        await mkuser("lk_fin_inv", ["finance"], ["catalog", "list", "finance", "messages"])
        async with SessionLocal() as db:
            u = (await db.execute(select(models.User).where(
                models.User.username == "lk_fin_inv"))).scalars().unique().one()
            # 与生产王芹同一种配置：只留待开票/已开票/售后费用
            u.hidden_tabs = ["finance:fund", "finance:pnl", "finance:pay_requests", "finance:pay_payment",
                             "finance:payables", "finance:expense", "finance:audit", "finance:inventory"]
            led = (await db.execute(select(models.SalesLedger).where(
                models.SalesLedger.project_id == pids["2026-952"]))).scalar_one()
            led.invoice_state = "pending_invoice"
            await db.commit()
        u_inv = await _u("lk_fin_inv")
        from app.agent import perm
        allowed = ar._allowed_tools(u_inv)
        money_tools = {"balance_due", "receivable_blind", "get_customer", "sales_summary", "ledger_incomplete"}
        chk(not (allowed & money_tools),
            f"应收/尾款/客户全景/销售额这些工具都不给（改之前全给）→ 多出 {sorted(allowed & money_tools)}")
        chk("invoice_pending" in allowed, "「待开票」页签没藏 → 待开票工具照样给")
        chk(perm.money_scope(u_inv) == "none", f"看钱口径降为 none → {perm.money_scope(u_inv)}")
        chk(not perm.can_approve_pay_req(u_inv), "请款审批页签藏了 → 智能体审批卡也不给批")
        async with SessionLocal() as db:
            g = await te.get_project(db, u_inv, "2026-952")
            chk((g.get("projects") or [g])[0].get("ledger") is None, "问项目：台账段（客户/合同额）不给")
            inv = await ts.tool_invoice_pending(db, u_inv)
            ms = await ar._run_tool_inner("sales_summary", {}, db, u_inv)
        web = await c.get("/api/finance/pending-invoices", headers=await login("lk_fin_inv"))
        chk(inv["count"] == len(web.json()) and inv["count"] >= 1,
            f"待开票仍是全量，= 网页那页（别又回「没有 ✅」）→ 工具 {inv['count']} vs 网页 {len(web.json())}")
        chk("error" in ms, f"直接点名调 sales_summary 也被拒 → {str(ms)[:60]}")
        # 反向：没藏页签的财务一切照旧
        chk(perm.money_scope(u_fin) == "all" and perm.can_approve_pay_req(u_fin)
            and money_tools <= ar._allowed_tools(u_fin), "没藏页签的财务：看钱、审批照旧")

        print("\n=== 5. 线索、待审订单：只给销售部 ===")
        async with SessionLocal() as db:
            db.add(models.SalesLead(source="官网", customer="常州新锐", contact="李工", owner_uid=u_s.id))
            db.add(models.SalesLead(source="展会", customer="扬州宏达", contact="赵总", owner_uid=u_s2.id))
            for code in ("2026-951", "2026-952"):
                led = (await db.execute(select(models.SalesLedger).where(
                    models.SalesLedger.project_id == pids[code]))).scalar_one()
                led.order_state = "pending"
            await db.commit()
        for t in ("leads_followup", "order_pending"):
            chk(t not in ar._allowed_tools(u_fin), f"财务没有 {t}（网页上这两页只给销售部）")
        async with SessionLocal() as db:
            r = await ar._run_tool_inner("leads_followup", {}, db, u_fin)
            chk("error" in r, f"财务点名调线索 → 拒 {str(r)[:50]}")
            ls = await ts.tool_leads_followup(db, u_s)
            chk([x["customer"] for x in ls["items"]] == ["常州新锐"], f"销售只看自己的线索 → {ls['items']}")
            lsl = await ts.tool_leads_followup(db, u_sl)
            chk({x["customer"] for x in lsl["items"]} >= {"常州新锐", "扬州宏达"}, "销售主管看全部线索")
            op = await ts.tool_order_pending(db, u_s)
            chk(codes(op) == ["2026-951"], f"销售只看自己提交的待审单 → {codes(op)}")
            opl = await ts.tool_order_pending(db, u_sl)
            chk(codes(opl) == ["2026-951", "2026-952"], f"销售主管看全部待审单 → {codes(opl)}")
            for code in ("2026-951", "2026-952"):
                led = (await db.execute(select(models.SalesLedger).where(
                    models.SalesLedger.project_id == pids[code]))).scalar_one()
                led.order_state = "approved"
            await db.commit()

        print("\n=== 6. 旧版 project_status：台账段要看归属，不只看菜单 ===")
        await mkuser("lk_fbs", ["sales", "buyer"], ["catalog", "list", "sales", "purchase_mgmt", "messages"])
        u_fbs = await _u("lk_fbs")
        async with SessionLocal() as db:
            r = await ar._run_tool_inner("project_status", {"code": "2026-952"}, db, u_fbs)
            chk(r.get("found") and "ledger" not in r,
                f"销售兼采购问别人的 952：进度照给，台账段（客户/合同额/四段款）不给 → ledger={r.get('ledger')}")
            r = await ar._run_tool_inner("project_status", {"code": "2026-952"}, db, u_sl)
            chk((r.get("ledger") or {}).get("customer") == "无锡联科", "销售主管照常有台账段")


asyncio.run(main())
print("\n" + ("全部通过 ✅" if not FAIL else f"{len(FAIL)} 条失败 ❌"))
for m in FAIL:
    print(" -", m)
sys.exit(1 if FAIL else 0)
