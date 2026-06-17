"""🆕 尾款到期提醒：尾款日期前14天起触发，每条台账只提醒一次；
推送销售本人 + 抄送销售主管(sales_lead) + 管理层(manager)。

口径(用户 2026-06-17 确认)：提前14天、只提醒一次、抄送主管/管理层。
覆盖：窗内触发 / 窗外不触发 / 尾款0不触发 / 已逾期触发 / 只提醒一次幂等 /
      销售本人+主管+管理层均收到 / 内部端点鉴权。
"""
import asyncio, os, sys, tempfile, shutil
from datetime import datetime, timezone, timedelta

tmp = tempfile.mkdtemp(prefix="baldue")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app.overdue import scan_balance_due

_CN_TZ = timezone(timedelta(hours=8))
FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)


def d(offset_days: int) -> str:
    """相对业务今天(中国时区)的 ISO 日期字符串。"""
    return (datetime.now(_CN_TZ).date() + timedelta(days=offset_days)).isoformat()


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mk(u, rc, fn):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": u, "password": "pass123", "full_name": fn, "role_id": rid[rc]})
            return r.json()["id"]
        async def login(u, p="pass123"):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        s1_id = await mk("s1", "sales", "赵仁辉")
        await mk("sl", "sales_lead", "钱主管")
        Hs1 = await login("s1")

        async def order(name, balance, balance_date):
            r = await c.post("/api/sales/orders", headers=Hs1, json={
                "name": name, "customer": "客户甲", "cust_type": "经销商", "contract": "有",
                "amount": 100000, "tax_rate": "13%", "prepay": 0, "before_ship": 0,
                "ship_receivable": 0, "balance": balance, "balance_date": balance_date,
                "depts": ["design"], "receiver": {"name": "a", "phone": "1", "addr": "b"}})
            assert r.status_code == 200, r.text
            return r.json()["project_id"]

        # A 窗内(+5天) 触发 / B 窗外(+30天) 不触发 / C 尾款0(+3天) 不触发 / D 已逾期(-10天) 触发
        await order("机A_窗内", 50000, d(5))
        await order("机B_窗外", 30000, d(30))
        await order("机C_零尾款", 0, d(3))
        await order("机D_逾期", 20000, d(-10))

        # 首次扫描：A + D 共 2 条被提醒
        async with SessionLocal() as db:
            r1 = await scan_balance_due(db)
        chk(r1["notified"] == 2, f"首次扫描应提醒2条(窗内A+逾期D): {r1}")

        # 销售本人 s1 收到 2 条尾款提醒
        msgs = (await c.get("/api/messages?limit=100", headers=Hs1)).json()
        bal = [m for m in msgs if "尾款提醒" in m["text"]]
        chk(len(bal) == 2, f"销售本人收到2条尾款提醒: {len(bal)}")
        chk(any("已逾期 10 天" in m["text"] for m in bal), "逾期文案正确(已逾期10天)")
        chk(any("还有 5 天到期" in m["text"] for m in bal), "窗内文案正确(还有5天到期)")
        chk(not any("机B_窗外" in m["text"] or "机C_零尾款" in m["text"] for m in bal),
            "窗外/零尾款项目不应提醒")

        # 销售主管 sl + 管理层 manager 收到抄送
        Hsl = await login("sl")
        chk(len([m for m in (await c.get("/api/messages?limit=100", headers=Hsl)).json()
                 if "尾款提醒" in m["text"]]) == 2, "销售主管收2条抄送")
        Hmg = await login("manager", "manager123")
        chk(len([m for m in (await c.get("/api/messages?limit=100", headers=Hmg)).json()
                 if "尾款提醒" in m["text"]]) == 2, "管理层收2条抄送")

        # 再次扫描：只提醒一次 → 0
        async with SessionLocal() as db:
            r2 = await scan_balance_due(db)
        chk(r2["notified"] == 0, f"再次扫描只提醒一次(幂等0): {r2}")
        chk(len([m for m in (await c.get("/api/messages?limit=100", headers=Hs1)).json()
                 if "尾款提醒" in m["text"]]) == 2, "销售本人仍仅2条(不重复)")

        # 内部端点：管理层可调且幂等(0)，非管理层 403
        r = await c.post("/api/internal/balance-due-scan", headers=H)
        chk(r.status_code == 200 and r.json()["notified"] == 0, f"内部端点可调且幂等: {r.text[:80]}")
        r = await c.post("/api/internal/balance-due-scan", headers=Hs1)
        chk(r.status_code == 403, f"非管理层不能触发尾款扫描: {r.status_code}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
