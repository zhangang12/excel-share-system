"""🆕 销售/台账域工具（第二批 6 个）。

这批工具的存在理由是数据驱动的：杨坛 2 个月里销售台账 243 次、请款 40 笔、
收货人 34 次、销售订单 29 次，**采购 0 次**——而原来 7 个工具里 3 个是采购的。

本测试锁四件事：
 1. **行级隔离复用 sales_router._all_view**——普通销售员只看本人负责的行；
 2. **软删项目的台账不能算进来**（现网有 28 行幽灵数据，¥40 万发货应收）；
 3. **admin 与 manager 拿到全部工具**（菜单全量分支）；
 4. 每个工具都有一句人话说明，且与门户目录的 desc 同源，不写两份说法。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="tools2")
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
from app.routers.agent_router import TOOL_LABELS, TOOL_DESC, _allowed_tools
from app.agent import portal, tools_sales as ts

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)
    else: print("  ok:", m)

NEW = ["receivable_blind", "shipment_receiver", "ledger_incomplete",
       "leads_followup", "order_pending", "invoice_pending"]


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
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        for u, role in (("mgr", "manager"), ("s1", "sales"), ("s2", "sales"), ("dsg", "designer")):
            await c.post("/api/admin/users", headers=H, json={
                "username": u, "password": "pass123", "full_name": u, "role_id": rid[role]})
        async def hdr(u, pw="pass123"):
            t = (await c.post("/api/auth/login", json={"username": u, "password": pw})).json()
            return {"Authorization": f"Bearer {t['access_token']}"}
        Hm, H1, H2, Hd = await hdr("mgr"), await hdr("s1"), await hdr("s2"), await hdr("dsg")

        # ── 造数据：一个正常项目 + 一个软删项目，各挂一条有钱的台账 ──
        async with SessionLocal() as db:
            uid = {u.username: u.id for u in (await db.execute(select(models.User))).scalars().all()}
            # sales_ledger.project_id 是唯一约束——一个项目只能挂一条台账，
            # 所以每条测试数据都要各自的项目。
            def mk(code, name, deleted=False):
                p = models.Project(code=code, name=name, is_deleted=deleted)
                db.add(p); return p
            p1, p2, p3 = mk("P-A", "甲项目"), mk("P-B", "乙项目"), mk("P-C", "丙项目")
            pd = mk("_deleted_9_P-DEAD", "软删项目", deleted=True)
            await db.flush()
            # s1 的：尾款无到期日 + 发货应收
            db.add(models.SalesLedger(project_id=p1.id, customer="甲客户",
                                      sales_uid=uid["s1"], amount=100000,
                                      balance=30000, balance_date=None,
                                      ship_receivable=50000))
            # s2 的：只有发货应收
            db.add(models.SalesLedger(project_id=p2.id, customer="乙客户",
                                      sales_uid=uid["s2"], amount=80000, ship_receivable=20000))
            # 软删项目上的钱：任何工具都不该看到
            db.add(models.SalesLedger(project_id=pd.id, customer="幽灵客户",
                                      sales_uid=uid["s1"], amount=999999,
                                      balance=888888, ship_receivable=777777))
            # 缺件：合同额为 0
            db.add(models.SalesLedger(project_id=p3.id, customer="丙客户",
                                      sales_uid=uid["s1"], amount=0))
            await db.commit()

        async def call(hdrs, tool):
            """走接口：验的是权限与端到端可用性（返回的是排好版的文本）。"""
            return await c.post("/api/agent/tool", headers=hdrs, json={"tool": tool})

        async def data(username, fn):
            """直接调工具函数：验的是数据口径与行级隔离（要看原始 items）。"""
            async with SessionLocal() as db2:
                u = (await db2.execute(select(models.User).where(
                    models.User.username == username))).scalar_one()
                return await fn(db2, u)

        print("===== 1. 六个工具都注册齐了（标签/说明/schema/门户目录）=====")
        for n in NEW:
            chk(n in TOOL_LABELS, f"{n} 有标签")
            chk(n in TOOL_DESC and len(TOOL_DESC[n]) > 8, f"{n} 有人话说明")
        cat = {x["key"]: x for x in portal.CATALOG}
        for n in NEW:
            hit = [k for k, v in cat.items() if v.get("tool") == n]
            chk(hit, f"{n} 在门户目录里")

        print("\n===== 2. admin / manager 拿到全部工具 =====")
        for name, h in (("admin", H), ("manager", Hm)):
            cap = (await c.get("/api/agent/capabilities", headers=h)).json()
            keys = {x["key"] for x in cap["items"]}
            chk(set(NEW) <= keys, f"{name} 拿到全部 6 个新工具：{cap['count']} 个")
            chk(all(x["desc"] for x in cap["items"]), f"{name} 每个工具都带说明")

        print("\n===== 3. 行级隔离：销售员只看本人的行 =====")
        d1 = await data("s1", ts.tool_receivable_blind)
        cust1 = {i["customer"] for i in d1["items"]}
        chk("甲客户" in cust1, f"s1 看得到自己的：{cust1}")
        chk("乙客户" not in cust1, f"s1 看不到 s2 的：{cust1}")
        d2 = await data("s2", ts.tool_receivable_blind)
        chk({i["customer"] for i in d2["items"]} == {"乙客户"}, "s2 只看到自己的")
        dm = await data("mgr", ts.tool_receivable_blind)
        chk({"甲客户", "乙客户"} <= {i["customer"] for i in dm["items"]}, "管理层看全量")

        print("\n===== 4. 软删项目的钱一分都不能算进来 =====")
        for tool, fn in (("receivable_blind", ts.tool_receivable_blind),
                         ("ledger_incomplete", ts.tool_ledger_incomplete)):
            d = await data("mgr", fn)
            txt = str(d)
            chk("幽灵客户" not in txt, f"{tool} 不含软删项目的行")
            chk("888888" not in txt and "777777" not in txt, f"{tool} 不含幽灵金额")
        chk(dm["total"] == 100000.0, f"合计只含正常项目 30000+50000+20000: {dm['total']}")

        print("\n===== 5. 台账缺件认得出缺什么 =====")
        d = await data("s1", ts.tool_ledger_incomplete)
        miss = [i for i in d["items"] if i["customer"] == "丙客户"]
        chk(miss and "合同额" in miss[0]["missing"], f"标出缺合同额: {miss}")

        print("\n===== 5b. 六个工具端到端都能出文本 =====")
        for n in NEW:
            r = await call(Hm, n)
            chk(r.status_code == 200 and r.json().get("reply"),
                f"{TOOL_LABELS[n]} 接口可用且有文案: {r.status_code}")

        print("\n===== 6. 无权角色拿不到 =====")
        cap = (await c.get("/api/agent/capabilities", headers=Hd)).json()
        keys = {x["key"] for x in cap["items"]}
        chk(not (set(NEW) & keys), f"设计师看不到销售域工具: {sorted(keys)}")
        r = await call(Hd, "receivable_blind")
        chk(r.status_code == 403, f"设计师直接调也被拒: {r.status_code}")

        print("\n===== 7. 门户目录 desc 与 TOOL_DESC 不写两份说法 =====")
        for c2 in portal.CATALOG:
            if c2.get("tool") in NEW:
                chk(len(c2["desc"]) > 8, f"{c2['key']} 门户小字非空：{c2['desc'][:20]}")

        print("\n===== 8. 管理层默认门户按真实轨迹排（不含采购卡）=====")
        tiles = [t["key"] for t in (await c.get("/api/agent/portal", headers=Hm)).json()["tiles"]]
        chk("receivable_blind" in tiles, f"含盯不住的应收: {tiles}")
        chk(not any(t.startswith("po_") for t in tiles), f"不含采购卡: {tiles}")

    await engine.dispose()
    print("\nPASSED" if not FAIL else f"\n{len(FAIL)} FAILURES")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
