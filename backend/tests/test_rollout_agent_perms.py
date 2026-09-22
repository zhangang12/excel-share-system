"""🆕 2026-09-23 手机 APP 智能体**推广到全公司**之前的权限收口验证。

背景：智能体此前实际只有管理层和少数几个人在用，权限上的窟窿一直没人踩到。
要发给全部 26 个在用账号（装配、钣金、封板、仓库、人事、设计、电工都在内）之前
做了一遍排查，审出三处「智能体比网页版看得多」：

  1. 待审请款卡对**所有登录用户**可见 —— 供应商、金额、收款账号后 4 位、采购内容。
     网页上这张列表只给采购和财务（purchase_mgmt_router.list_payment_requests）。
  2. 项目类工具把**合同额、客户、四段款**给了所有有「项目详单」菜单的人。
     网页上这些只在销售台账页，仓库/装配/设计根本打不开那一页。
  3. find_entity / get_supplier / project_progress **一条行级隔离都没有**：
     受限岗位在网页项目目录里只看得到自己经手的几台，问智能体能拿到全部。

外加一条反向的（看得**太少**）：财务问「待开票有哪些」被过滤成空，回「没有 ✅」。
根因是智能体套用了 `sales_router._all_view`（只认管理层+销售主管），
而网页 finance_router.pending_invoices 给财务的是全量。

断言口径一律**与网页接口对照**，不写「等于 N 条」的常量 ——
常量会随种子数据漂移，对照断言才能持续捕获「网页改了规则、智能体没跟」。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="rollperm")
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
from app.agent import perm, tools_entity as te, tools_sales as ts
from app.agent.cards import pay_req, sales_order

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def _u(uname: str) -> models.User:
    """按 username 取 User。roles 是 lazy=selectin，必须走查询才预加载。"""
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
        async def login(u, p="pass123"):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(name, codes, menus=None):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123", "full_name": name,
                "role_ids": [rid[x] for x in codes if x in rid]})
            assert r.status_code == 200, f"{name}: {r.text}"
            uid = r.json()["id"]
            if menus is not None:
                mr = await c.put(f"/api/admin/users/{uid}/menus", headers=H,
                                 json={"menus": menus})
                assert mr.status_code == 200, mr.text
            return uid

        # ── 造人。菜单给得比实际宽一点，目的就是证明：**光有菜单不等于能看钱** ──
        FULL = ["catalog", "list", "purchase_mgmt", "sales", "finance",
                "warehouse", "produce", "design", "messages"]
        await mkuser("rp_assembler", ["assembler"], ["catalog", "list", "produce", "messages"])
        await mkuser("rp_wh", ["warehouse"], ["catalog", "list", "warehouse", "messages"])
        await mkuser("rp_sales", ["sales"], ["catalog", "list", "sales", "messages"])
        await mkuser("rp_sales2", ["sales"], ["catalog", "list", "sales", "messages"])
        await mkuser("rp_slead", ["sales", "sales_lead"], FULL)
        await mkuser("rp_fin", ["finance"], ["catalog", "list", "finance", "messages"])
        buyer_id = await mkuser("rp_buyer", ["buyer"], ["catalog", "list", "purchase_mgmt", "messages"])
        await mkuser("rp_buyer2", ["buyer"], ["catalog", "list", "purchase_mgmt", "messages"])

        Hasm = await login("rp_assembler")
        Hwh = await login("rp_wh")
        Hsales = await login("rp_sales")
        Hslead = await login("rp_slead")
        Hfin = await login("rp_fin")
        Hbuy = await login("rp_buyer")
        Hbuy2 = await login("rp_buyer2")

        u_asm, u_wh = await _u("rp_assembler"), await _u("rp_wh")
        u_sales, u_sales2 = await _u("rp_sales"), await _u("rp_sales2")
        u_slead, u_fin = await _u("rp_slead"), await _u("rp_fin")
        u_buy, u_buy2 = await _u("rp_buyer"), await _u("rp_buyer2")

        # ── 造数据：一个项目 + 一行销售台账（挂在 rp_sales 名下）──
        pr_ = await c.post("/api/projects", headers=H,
                           json={"code": "2026-901", "name": "推广权限验证台"})
        assert pr_.status_code == 200, pr_.text
        pid = pr_.json()["id"]
        async with SessionLocal() as db:
            db.add(models.SalesLedger(
                project_id=pid, customer="南京迈克斯机械", amount=880000,
                prepay=264000, before_ship=440000, ship_receivable=132000,
                balance=44000, balance_date="2026-12-31",
                sales_uid=u_sales.id, order_state="approved",
                invoice_state="pending_invoice"))
            await db.commit()

        # 一张待审请款单（rp_buyer 下的单）
        sid = (await c.post("/api/purchase-mgmt/suppliers", headers=Hbuy,
                            json={"name": "无锡俊帆金属"})).json()["id"]
        iid = (await c.post("/api/purchase-mgmt/items", headers=Hbuy,
                            json={"supplier_id": sid, "item_name": "钢板 Q235",
                                  "qty": 2, "unit_price": 500,
                                  "project_code": "2026-901"})).json()["id"]
        r = await c.post("/api/purchase-mgmt/payment-requests", headers=Hbuy,
                         json={"supplier_id": sid, "requested_amount": 1000,
                               "items": [{"item_id": iid, "allocated_amount": 1000}]})
        chk(r.status_code in (200, 201), f"前置：造出一张待审请款单 {r.status_code} {r.text[:80]}")

        print("\n=== 1. 待审请款：谁都能看 → 只有财务/管理层 + 采购员看自己的 ===")
        for name, hh in (("装配工", Hasm), ("仓库", Hwh), ("销售", Hsales)):
            rr = await c.get("/api/agent/cards/pending", headers=hh)
            n = rr.json().get("count", -1) if rr.status_code == 200 else -1
            chk(rr.status_code == 200 and n == 0,
                f"{name}拿待办卡：必须 200 且**一张请款卡都没有**（改之前能看到"
                f"供应商+金额+账号后4位）→ {rr.status_code} count={n}")

        rr = await c.get("/api/agent/cards/pending", headers=Hfin)
        fin_cards = [x for x in rr.json().get("cards", []) if x["type"] == "pay_req_approve"]
        chk(rr.status_code == 200 and len(fin_cards) >= 1,
            f"财务照常看得到并且能批（别为了堵泄露把真正的审批人也堵了）→ {len(fin_cards)} 张")
        chk(all(a.get("disabled_by") is None for x in fin_cards for a in x["actions"]),
            "财务那张的审批按钮是亮的")

        rr = await c.get("/api/agent/cards/pending", headers=Hbuy)
        buy_cards = [x for x in rr.json().get("cards", []) if x["type"] == "pay_req_approve"]
        chk(len(buy_cards) >= 1, f"提单的采购员看得到自己那张（要能查进度）→ {len(buy_cards)} 张")
        chk(all(a.get("disabled_by") for x in buy_cards for a in x["actions"]),
            "**但按钮必须置灰** —— 亮着点下去吃 403，用户只会认为系统坏了")

        rr = await c.get("/api/agent/cards/pending", headers=Hbuy2)
        chk(len([x for x in rr.json().get("cards", []) if x["type"] == "pay_req_approve"]) == 0,
            "另一个采购员看不到别人的请款单（采购之间的隔离没被这次改动打穿）")

        async with SessionLocal() as db:
            chk(await pay_req.pending_pay_reqs(db, u_asm) == [], "装配工：pending_pay_reqs 直接空")
            chk(len(await pay_req.pending_pay_reqs(db, u_fin)) >= 1, "财务：pending_pay_reqs 有数")

        print("\n=== 2. 每日简报：批不了的人不该收到「待你审批」 ===")
        from app.agent import briefing
        async with SessionLocal() as db:
            chk(await briefing._pay_req_items(db, u_asm) == [], "装配工简报里没有请款审批条目")
            chk(await briefing._pay_req_items(db, u_buy) == [],
                "采购员简报里也没有 —— 他能看进度，但那不是「待他审批」，进简报就是假待办")
            chk(len(await briefing._pay_req_items(db, u_fin)) >= 1, "财务简报里有")

        print("\n=== 3. 销售订单审批卡：只给销售主管/管理层 ===")
        async with SessionLocal() as db:
            led = (await db.execute(select(models.SalesLedger)
                                    .where(models.SalesLedger.project_id == pid))).scalar_one()
            led.order_state = "pending"
            await db.commit()
        async with SessionLocal() as db:
            chk(await sales_order.pending_orders(db, u_sales) == [],
                "销售员看不到「待你审批」 —— 那是他自己提交的单，批不了")
            chk(await sales_order.pending_orders(db, u_asm) == [], "装配工更看不到")
            chk(len(await sales_order.pending_orders(db, u_slead)) >= 1, "销售主管看得到")
            chk(len(await briefing._order_items(db, u_sales)) == 0, "简报同口径")
        async with SessionLocal() as db:
            led = (await db.execute(select(models.SalesLedger)
                                    .where(models.SalesLedger.project_id == pid))).scalar_one()
            led.order_state = "approved"
            await db.commit()

        print("\n=== 4. 项目工具：合同额/客户/四段款按权限脱敏 ===")
        async with SessionLocal() as db:
            for name, uo in (("装配工", u_asm), ("仓库", u_wh), ("别人家的销售", u_sales2)):
                g = await te.get_project(db, uo, "2026-901")
                if not g.get("found"):
                    chk(True, f"{name}：连项目本身都看不到（受限岗位只看自己经手的）")
                    continue
                chk(g.get("ledger") is None,
                    f"{name}问 2026-901：**台账段整段没有**（合同额/客户/四段款一个都不给）"
                    f" → {g.get('ledger')}")
            for name, uo in (("负责这单的销售", u_sales), ("销售主管", u_slead),
                             ("财务", u_fin)):
                g = await te.get_project(db, uo, "2026-901")
                led_ = g.get("ledger") or {}
                chk(led_.get("contract") == 880000.0,
                    f"{name}照常看得到合同额 → {led_.get('contract')}")
                chk("sales_uid" not in led_, f"{name}：内部字段 sales_uid 不外泄")

        print("\n=== 5. 交期看板 / 模糊搜索：行级隔离 ===")
        async with SessionLocal() as db:
            pp = await te.project_progress(db, u_wh)
            rows = [x for x in pp["items"] if x["project"] == "2026-901"]
            chk(all("contract" not in x and "customer" not in x for x in rows),
                "仓库看交期看板：**没有 contract/customer 两个键**"
                "（留一个 contract:0 会被模型读成「合同额是 0」，比不给更糟）")
            pp2 = await te.project_progress(db, u_slead)
            rows2 = [x for x in pp2["items"] if x["project"] == "2026-901"]
            chk(rows2 and rows2[0].get("contract") == 880000.0, "销售主管照常有合同额")

            f_asm = await te.find_entity(db, u_asm, "迈克斯")
            chk(not f_asm["matches"].get("customer"),
                "装配工搜「迈克斯」：搜不到客户（客户名+台账行数本身就是销售信息）")
            f_sl = await te.find_entity(db, u_slead, "迈克斯")
            chk(f_sl["matches"].get("customer"), "销售主管搜得到")
            f_s2 = await te.find_entity(db, u_sales2, "迈克斯")
            chk(not f_s2["matches"].get("customer"), "另一个销售搜不到不归他的客户")

            gc = await te.get_customer(db, u_wh, "迈克斯")
            chk(not gc.get("found") and "看不到" in (gc.get("hint") or ""),
                f"仓库查客户全景：明说「你的账号看不到这部分」，"
                f"不是含糊的「没查到」→ {gc.get('hint')}")

        print("\n=== 6. 反向：财务看得太少（「待开票」回没有 ✅）===")
        web = await c.get("/api/finance/pending-invoices", headers=Hfin)
        chk(web.status_code == 200, f"网页待开票接口 {web.status_code}")
        async with SessionLocal() as db:
            tool = await ts.tool_invoice_pending(db, u_fin)
        chk(tool["count"] == len(web.json()),
            f"财务问智能体「待开票」= 网页那页的行数（改之前被过滤成 0，"
            f"助手回「没有 ✅」）→ 工具 {tool['count']} vs 网页 {len(web.json())}")
        chk(perm.sales_read_all(u_fin) and not perm.sales_read_all(u_sales),
            "口径：财务算「看全部」，普通销售不算")

        print("\n=== 7. 供应商画像：采购之间的隔离 ===")
        # rp_buyer2 也下一单给同一家供应商，两人各自只该看到自己的
        i2 = (await c.post("/api/purchase-mgmt/items", headers=Hbuy2,
                           json={"supplier_id": sid, "item_name": "型材",
                                 "qty": 1, "unit_price": 300})).json()
        chk("id" in i2, f"前置：另一个采购员也下一单 {str(i2)[:60]}")
        async with SessionLocal() as db:
            g1 = await te.get_supplier(db, u_buy, "俊帆")
            g2 = await te.get_supplier(db, u_buy2, "俊帆")
            gm = await te.get_supplier(db, await _u("admin"), "俊帆")
        chk(g1["purchase_items"] == 1 and g2["purchase_items"] == 1,
            f"两个受限采购员各看到自己那 1 条 → {g1['purchase_items']} / {g2['purchase_items']}")
        chk(gm["purchase_items"] == 2, f"管理层看到全部 2 条 → {gm['purchase_items']}")

        print("\n=== 8. /api/agent/models：普通员工不该吃 403 ===")
        for name, hh in (("装配工", Hasm), ("仓库", Hwh), ("销售", Hsales)):
            rr = await c.get("/api/agent/models", headers=hh)
            chk(rr.status_code == 200,
                f"{name}打开智能体页面探测模型列表：200，不是 403 弹红叉 → {rr.status_code}")
            if rr.status_code == 200:
                chk("api_key" not in rr.text, f"{name}：返回里没有任何密钥字段")
        rr = await c.get("/api/agent/config", headers=Hasm)
        chk(rr.status_code == 403, f"**写配置的口没放开**，仍然只给管理员 → {rr.status_code}")


asyncio.run(main())
print("\n" + ("全部通过 ✅" if not FAIL else f"{len(FAIL)} 条失败 ❌"))
for m in FAIL:
    print(" -", m)
sys.exit(1 if FAIL else 0)
