"""P0 行级越权修复验证：AI 助手看到的行，必须与页面接口**一字不差**。

背景：修复前 7 个工具里只有 tool_project_status 接收 current，其余 6 个函数签名里
根本没有用户对象，因此不可能做行级过滤。后果：
  · 普通采购员（_buyer_restricted）页面只看得到自己录的单，问 AI 拿到全公司采购明细
  · 普通销售员页面只看得到自己的客户，问一句「尾款」拿到全公司客户名+金额+归属销售
  · 工人页面只看得到派给自己的单，问 AI 拿到同部门其他人的任务

本测试的断言口径刻意选成「**与页面接口对照**」而不是「行数等于某个常量」——
常量断言会随种子数据漂移，而对照断言能持续捕获"页面改了规则、工具没跟"这类回归。
这正是当初缺失、因而没能拦住越权的那一类测试。

覆盖：
  1. 采购：受限采购员 vs 采购主管 vs 管理层，三种身份下 AI == 页面
  2. 销售：普通销售员 vs 销售主管，AI == 页面
  3. 任务：工人只见自己的单；部门主管见本部门全部
  4. 聚合口径 bug：po_overdue_by_supplier 的 item_total 必须等于全量而非 Top-20
  5. 晨报按身份收窄（复用同样的行级过滤）
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="agentscope")
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
from app.routers import agent_router as ag

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)


async def _user(uname):
    """按 username 重新查出 User（roles 是 lazy=selectin，必须查询才预加载）。"""
    async with SessionLocal() as db:
        return (await db.execute(select(models.User)
                                 .where(models.User.username == uname))).scalar_one()


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    tr = ASGITransport(app=app)
    async with AsyncClient(transport=tr, base_url="http://test", timeout=60) as c:
        async def login(u, p):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(name, codes, menus=None):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123", "full_name": name,
                "role_ids": [rid[x] for x in codes]})
            assert r.status_code == 200, r.text
            uid = r.json()["id"]
            if menus is not None:
                mr = await c.put(f"/api/admin/users/{uid}/menus", headers=H, json={"menus": menus})
                assert mr.status_code == 200, mr.text
            return uid

        PO_MENU = ["catalog", "list", "purchase_mgmt", "messages"]
        SA_MENU = ["catalog", "list", "sales", "messages"]
        DP_MENU = ["catalog", "list", "design", "produce", "messages"]

        b1 = await mkuser("sc_b1", ["buyer"], PO_MENU)          # 受限采购员
        b2 = await mkuser("sc_b2", ["buyer"], PO_MENU)          # 另一个受限采购员
        await mkuser("sc_blead", ["buyer_lead"], PO_MENU)       # 采购主管：看全部
        s1 = await mkuser("sc_s1", ["sales"], SA_MENU)          # 普通销售员
        s2 = await mkuser("sc_s2", ["sales"], SA_MENU)
        await mkuser("sc_slead", ["sales_lead"], SA_MENU)       # 销售主管：看全部
        w1 = await mkuser("sc_w1", ["designer"], DP_MENU)       # 工人
        w2 = await mkuser("sc_w2", ["designer"], DP_MENU)
        await mkuser("sc_dlead", ["design_lead"], DP_MENU)      # 设计主管

        # ---------- 造数：两个采购员各 3 条超期采购 ----------
        Hb1, Hb2 = await login("sc_b1", "pass123"), await login("sc_b2", "pass123")
        sid1 = (await c.post("/api/purchase-mgmt/suppliers", headers=Hb1,
                             json={"name": "供应商甲"})).json()["id"]
        sid2 = (await c.post("/api/purchase-mgmt/suppliers", headers=Hb2,
                             json={"name": "供应商乙"})).json()["id"]
        for H_, sid_, tagname in ((Hb1, sid1, "b1件"), (Hb2, sid2, "b2件")):
            for i in range(3):
                r = await c.post("/api/purchase-mgmt/items", headers=H_, json={
                    "supplier_id": sid_, "item_name": f"{tagname}{i}", "qty": 1,
                    "expected_arrival": "2026-01-0%d" % (i + 1)})
                assert r.status_code in (200, 201), r.text

        # ---------- 造数：两个销售员各 2 个项目 + 台账 ----------
        for i, (uid, code) in enumerate([(s1, "SC-S1A"), (s1, "SC-S1B"),
                                         (s2, "SC-S2A"), (s2, "SC-S2B")]):
            await c.post("/api/projects", headers=H, json={"code": code, "name": "项目" + code})
        async with SessionLocal() as db:
            for uid, code, cust in [(s1, "SC-S1A", "客户甲"), (s1, "SC-S1B", "客户乙"),
                                    (s2, "SC-S2A", "客户丙"), (s2, "SC-S2B", "客户丁")]:
                pid = (await db.execute(select(models.Project.id)
                                        .where(models.Project.code == code))).scalar_one()
                db.add(models.SalesLedger(project_id=pid, sales_uid=uid, customer=cust,
                                          amount=100000, balance=50000, balance_date="2026-01-15"))
            await db.commit()

        # ---------- 造数：两个工人各 2 张逾期设计单 ----------
        async with SessionLocal() as db:
            pid = (await db.execute(select(models.Project.id)
                                    .where(models.Project.code == "SC-S1A"))).scalar_one()
            for uid in (w1, w2):
                for i in range(2):
                    db.add(models.DeptOrder(project_id=pid, dept="design", worker_id=uid,
                                            status="in_progress", due_date="2026-01-0%d" % (i + 1)))
            await db.commit()

        # ================= 1. 采购：AI == 页面 =================
        async def page_po_count(H_):
            r = await c.get("/api/purchase-mgmt/items", headers=H_, params={"page_size": 200})
            assert r.status_code == 200, r.text
            j = r.json()
            return len(j["items"] if isinstance(j, dict) and "items" in j else j)

        for uname, label in (("sc_b1", "受限采购员"), ("sc_blead", "采购主管"), ("admin", "管理层")):
            u = await _user(uname)
            H_ = await login(uname, "admin123" if uname == "admin" else "pass123")
            async with SessionLocal() as db:
                ai = await ag.tool_po_arrival_overdue(db, u)
            page = await page_po_count(H_)
            chk(ai["count"] == page,
                f"★采购 {label}：AI {ai['count']} 条 vs 页面 {page} 条，必须相等")
            if uname == "sc_b1":
                chk(ai["count"] == 3, f"受限采购员应只看到自己的 3 条，实得 {ai['count']}")
                names = {x["item_name"] for x in ai["items"]}
                chk(not any(n.startswith("b2件") for n in names),
                    f"★受限采购员不得看到他人明细: {names}")

        # ================= 2. 销售：AI == 页面 =================
        async def page_ledger_count(H_):
            r = await c.get("/api/sales/ledger", headers=H_, params={"page_size": 200})
            assert r.status_code == 200, r.text
            return len(r.json()["rows"])

        for uname, label, expect in (("sc_s1", "普通销售员", 2), ("sc_slead", "销售主管", 4)):
            u = await _user(uname)
            H_ = await login(uname, "pass123")
            async with SessionLocal() as db:
                ai = await ag.tool_balance_due(db, u)
            page = await page_ledger_count(H_)
            chk(ai["count"] == page, f"★销售 {label}：AI {ai['count']} vs 页面 {page}，必须相等")
            chk(ai["count"] == expect, f"销售 {label} 应为 {expect} 条，实得 {ai['count']}")
            if uname == "sc_s1":
                custs = {x["customer"] for x in ai["items"]}
                chk(custs == {"客户甲", "客户乙"}, f"★普通销售员不得看到他人客户: {custs}")

        # ================= 3. 任务：工人只见自己 / 主管见本部门 =================
        for uname, label, expect in (("sc_w1", "工人", 2), ("sc_dlead", "设计主管", 4),
                                     ("admin", "管理层", 4)):
            u = await _user(uname)
            async with SessionLocal() as db:
                ai = await ag.tool_overdue_orders(db, u, allowed_depts=["design"])
            chk(ai["count"] == expect, f"★任务 {label} 应为 {expect} 条，实得 {ai['count']}")
            if uname == "sc_w1":
                workers = {x["worker"] for x in ai["items"]}
                chk(workers == {"sc_w1"}, f"★工人不得看到他人任务: {workers}")

        # ================= 4. 聚合口径：item_total 必须是全量 =================
        u_lead = await _user("sc_blead")
        async with SessionLocal() as db:
            agg = await ag.tool_po_overdue_by_supplier(db, u_lead)
            full = await ag.tool_po_arrival_overdue(db, u_lead)
        s = sum(x["count"] for x in agg["suppliers"])
        chk(agg["item_total"] == full["count"],
            f"★item_total 应等于全量 {full['count']}，实得 {agg['item_total']}")
        chk(s == agg["item_total"],
            f"★供应商分项之和 {s} 应等于 item_total {agg['item_total']}（此前在 Top-20 上聚合导致对不上）")

        # 受限采购员的聚合也必须只含自己的供应商
        u_b1 = await _user("sc_b1")
        async with SessionLocal() as db:
            agg1 = await ag.tool_po_overdue_by_supplier(db, u_b1)
        sups = {x["supplier"] for x in agg1["suppliers"]}
        chk(sups == {"供应商甲"}, f"★受限采购员的供应商聚合不得含他人供应商: {sups}")

        # ================= 5. 晨报同样收窄 =================
        async with SessionLocal() as db:
            mr_b1 = await ag.tool_morning_report(db, u_b1, {"po"}, None)
            mr_s1 = await ag.tool_morning_report(db, await _user("sc_s1"), {"balance"}, None)
        chk(mr_b1["po_arrival_overdue"]["count"] == 3,
            f"★晨报采购小节应只含本人 3 条，实得 {mr_b1['po_arrival_overdue']['count']}")
        chk(mr_s1["balance_due"]["count"] == 2,
            f"★晨报尾款小节应只含本人 2 条，实得 {mr_s1['balance_due']['count']}")

        # ================= 6. 菜单门控未被破坏（回归） =================
        u_s1 = await _user("sc_s1")
        async with SessionLocal() as db:
            denied = await ag._run_tool("po_arrival_overdue", {}, db, u_s1)
        chk("error" in denied, f"★无 purchase_mgmt 菜单应被拒: {denied}")

    print("PASSED" if not FAIL else f"FAILED {len(FAIL)}")

asyncio.run(main())
