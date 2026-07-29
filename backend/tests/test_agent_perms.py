"""🆕 AI 助手全员开放 + 数据域按菜单门控 测试（临时 SQLite，规则降级路径，不依赖 LLM Key）：

1. 菜单：普通 warehouse 用户与 admin 的 /api/auth/menus 均含 key=agent 且 label=「AI 助手」。
2. warehouse 用户（catalog/list/warehouse/oa/messages + agent）：
   - po_arrival_overdue（问「采购未到货」）→ 被拒：无 purchase_mgmt 菜单；
   - overdue_orders（问「逾期任务」）→ 被拒：无 design/electric/produce 任一菜单
     （实现口径=无交集直接拒绝，不返回空清单）；
   - balance_due（问「尾款到期」）→ 被拒：无 finance/sales 菜单；
   - morning_report（问「今日晨报」）→ 全无可查询数据域 → 提示联系管理员开通菜单；
   - project_status：是项目成员的项目可查，但结果无 ledger（回款/尾款小节不渲染）；
     非项目成员的项目 → 「无权查看该项目」；
   - 无权时 suggestions 被过滤为空。
3. finance 用户（catalog/list/purchase_mgmt/finance/oa/messages + agent）：
   - balance_due 可用；project_status 结果含 ledger（渲染「回款/尾款」）；
   - 晨报只含其有菜单的小节（采购/尾款），不含人事到期。
4. admin：全部工具可用（晨报含全部小节）。
5. 配置接口仍仅管理层：GET /api/agent/models、GET/PUT /api/agent/config 对普通用户 403。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="agentperm")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.environ["AGENT_LLM_API_KEY"] = ""   # 明确走规则降级路径
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        async def login(u, p):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, f"login {u}: {r.text}"
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mk(uname, role_code):
            r = await c.post("/api/admin/users", headers=H,
                             json={"username": uname, "password": "pass123",
                                   "full_name": uname, "role_id": rid[role_code]})
            assert r.status_code == 200, f"mk {uname}: {r.text}"
            return r.json()["id"]

        w1_id = await mk("w1", "warehouse")
        f1_id = await mk("f1", "finance")
        Hw1 = await login("w1", "pass123")
        Hf1 = await login("f1", "pass123")

        async def chat(h, msg):
            r = await c.post("/api/agent/chat", headers=h, json={"message": msg})
            assert r.status_code == 200, f"chat {msg!r}: {r.status_code} {r.text[:200]}"
            return r.json()

        # 造项目：TH-9001（w1/f1 是成员 + 有台账）、TH-9002（无任何成员）
        async with SessionLocal() as db:
            p1 = models.Project(code="TH-9001", name="成员项目")
            p2 = models.Project(code="TH-9002", name="非成员项目")
            db.add_all([p1, p2])
            await db.flush()
            db.add_all([
                models.ProjectMember(project_id=p1.id, user_id=w1_id, permission="view"),
                models.ProjectMember(project_id=p1.id, user_id=f1_id, permission="view"),
                models.SalesLedger(project_id=p1.id, customer="测试客户", amount=100000,
                                   balance=50000, balance_date="2026-08-10"),
            ])
            await db.commit()

        # ===== 1. 菜单含 agent（全员）=====
        for h, who in ((Hw1, "warehouse"), (H, "admin")):
            r = await c.get("/api/auth/menus", headers=h)
            items = {m["key"]: m["label"] for m in r.json()["menus"]}
            chk("agent" in items, f"{who} 菜单含 agent: {sorted(items)}")
            chk(items.get("agent") == "AI 助手", f"{who} agent label=AI 助手: {items.get('agent')}")

        # ===== 2. warehouse 用户域门控 =====
        j = await chat(Hw1, "采购未到货")
        chk("你无权查询该域数据" in j["reply"] and "采购管理" in j["reply"],
            f"w1 采购域被拒: {j['reply'][:80]}")
        chk(j["suggestions"] == [], f"w1 无权时 suggestions 过滤为空: {j['suggestions']}")

        j = await chat(Hw1, "逾期任务")
        chk("你无权查询该域数据" in j["reply"],
            f"w1 部门逾期被拒(无 design/electric/produce 任一): {j['reply'][:80]}")

        j = await chat(Hw1, "尾款到期")
        chk("你无权查询该域数据" in j["reply"] and ("财务部" in j["reply"] or "销售部" in j["reply"]),
            f"w1 尾款被拒: {j['reply'][:80]}")

        j = await chat(Hw1, "今日晨报")
        chk("你没有可查询的数据域，请联系管理员开通菜单" in j["reply"],
            f"w1 晨报无可用域提示: {j['reply'][:80]}")

        # project_status：成员项目可查但无 ledger；非成员项目无权
        j = await chat(Hw1, "TH-9001 进度")
        chk("TH-9001" in j["reply"] and "无权" not in j["reply"],
            f"w1 可查成员项目: {j['reply'][:80]}")
        chk("回款/尾款" not in j["reply"] and "50000" not in j["reply"] and "50,000" not in j["reply"],
            f"w1(无 finance/sales) 项目结果无 ledger: {j['reply'][-120:]}")
        j = await chat(Hw1, "TH-9002 进度")
        chk("无权查看该项目" in j["reply"],
            f"w1 非成员项目无权: {j['reply'][:80]}")

        # ===== 3. finance 用户 =====
        j = await chat(Hf1, "尾款到期")
        chk("尾款到期" in j["reply"] and "无权" not in j["reply"],
            f"f1 尾款可用: {j['reply'][:80]}")
        j = await chat(Hf1, "TH-9001 进度")
        chk("回款/尾款" in j["reply"] and "50,000" in j["reply"],
            f"f1( finance) 项目结果含 ledger: {j['reply'][-120:]}")
        j = await chat(Hf1, "今日晨报")
        chk("采购到期未到货" in j["reply"] and "尾款到期/逾期" in j["reply"]
            and "人事到期" not in j["reply"],
            f"f1 晨报只含有菜单的小节(采购+尾款,无人事): {j['reply'][:150]}")

        # ===== 4. admin 全部可用 =====
        j = await chat(H, "采购未到货")
        chk("无权" not in j["reply"] and "到期未到货采购" in j["reply"],
            f"admin 采购域可用: {j['reply'][:80]}")
        j = await chat(H, "今日晨报")
        chk("人事到期" in j["reply"] and "采购到期未到货" in j["reply"],
            f"admin 晨报含全部小节: {j['reply'][:150]}")

        # ===== 5. 配置接口仍仅管理层（普通用户 403）=====
        r = await c.get("/api/agent/models", headers=Hw1)
        chk(r.status_code == 403, f"w1 GET /models 403: {r.status_code}")
        r = await c.get("/api/agent/config", headers=Hw1)
        chk(r.status_code == 403, f"w1 GET /config 403: {r.status_code}")
        r = await c.put("/api/agent/config", headers=Hw1, json={"model": "x"})
        chk(r.status_code == 403, f"w1 PUT /config 403: {r.status_code}")
        r = await c.get("/api/agent/models", headers=H)
        chk(r.status_code == 200, f"admin GET /models 200: {r.status_code}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
