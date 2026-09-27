"""🆕 2026-09-27 网页版智能体「提示词」面板（GET /agent/prompts、PUT /agent/prompts/saved）。

原来网页版写死 5 个快捷问题，所有人一样：装配工点「尾款到期」得到「无权查询」，
财务点「采购未到货」也一样。本文件钉死：
  · 提示词按 _allowed_tools 过滤 —— 没权限的条目**一条都不下发**；
  · 每条都挂在真实存在的工具上（写错工具名不报错，只会让这条永远不出现）；
  · 「最近问过」来自审计日志、去重、最近的在前，且不含「[直答]…」这种日志标签；
  · 收藏的净化（去空/限长/去重/封顶）在后端做，前端传什么都兜得住；各人各存各的。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="prompts")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models
from app.agent import prompts as P
from app.routers.agent_router import TOOL_LABELS

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


def _qs(body):
    return [it["q"] for g in body["groups"] for it in g["items"]]


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    print("\n=== 0. 目录本身：每条都挂在真实工具上 ===")
    tools = {it["tool"] for g in P.GROUPS for it in g["items"] if it["tool"]}
    chk(tools <= set(TOOL_LABELS),
        f"提示词挂的工具都真实存在（写错不报错，只会让这条永远不出现）→ 不存在的: {tools - set(TOOL_LABELS)}")
    fills = [it for g in P.GROUPS for it in g["items"] if it.get("kind") == "fill"]
    chk(all("{" in it["q"] and "}" in it["q"] for it in fills),
        "填空模板里都有 {…} 占位（前端靠它选中、发送前靠它拦截）")
    sends = [it for g in P.GROUPS for it in g["items"] if it.get("kind", "send") == "send"]
    chk(not any("{" in it["q"] for it in sends),
        "直接发送的条目里**不能**有 {…}（有的话前端会把它当没填完的模板拦下，点了没反应）")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
        async def login(u, p="pass123"):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(name, codes, menus):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123", "full_name": name,
                "role_ids": [rid[x] for x in codes]})
            assert r.status_code == 200, r.text
            uid = r.json()["id"]
            m = await c.put(f"/api/admin/users/{uid}/menus", headers=H, json={"menus": menus})
            assert m.status_code == 200, m.text
            return uid

        asm_id = await mkuser("pr_asm", ["assembler"], ["catalog", "produce", "messages"])
        await mkuser("pr_fin", ["finance"], ["catalog", "list", "finance", "messages"])
        await mkuser("pr_buy", ["buyer"], ["catalog", "list", "purchase_mgmt", "messages"])
        Ha, Hf, Hb = await login("pr_asm"), await login("pr_fin"), await login("pr_buy")

        print("\n=== 1. 按权限下发：各人看到的不一样 ===")
        ra = await c.get("/api/agent/prompts", headers=Ha)
        rf = await c.get("/api/agent/prompts", headers=Hf)
        rb = await c.get("/api/agent/prompts", headers=Hb)
        rm = await c.get("/api/agent/prompts", headers=H)
        chk(all(r.status_code == 200 for r in (ra, rf, rb, rm)),
            f"四种身份都 200 → {[r.status_code for r in (ra, rf, rb, rm)]}")
        qa, qf, qb, qm = _qs(ra.json()), _qs(rf.json()), _qs(rb.json()), _qs(rm.json())

        chk("我手上的活" in qa and "待我审批的" in qa,
            f"装配工：有「我手上的活」「待我审批的」→ {qa}")
        chk("尾款到期" not in qa and "采购未到货" not in qa,
            "装配工：**没有**「尾款到期」「采购未到货」—— 原来写死的快捷问题里就有这两个，点了只会「无权查询」")
        chk("尾款到期" in qf and "待开票" in qf and "采购未到货" not in qf,
            "财务：有尾款、待开票；没有采购未到货")
        chk("采购未到货" in qb and "尾款到期" not in qb,
            "采购：有采购未到货；没有尾款到期")
        chk(len(qm) >= len(qf) and "给{某人}派个待办：{做什么}，{几号}前完成" in qm,
            f"管理层看得最全，含派待办模板 → {len(qm)} 条")
        chk("给{某人}派个待办：{做什么}，{几号}前完成" not in qa,
            "派待办模板只给能派的人（端点就是 admin/manager）")
        chk(all(g["items"] for g in ra.json()["groups"]), "空组整组不下发")
        chk(all("tool" not in it for g in rm.json()["groups"] for it in g["items"]),
            "下发的条目里不带 tool 字段（前端用不着，也不该知道背后是哪个工具）")

        print("\n=== 2. 最近问过 ===")
        async with SessionLocal() as db:
            for q in ("2026-080 卡在哪", "今日晨报", "[直答]今日晨报", "2026-080 卡在哪", "x", "这月销售额多少"):
                db.add(models.AgentChatLog(user_id=asm_id, username="pr_asm", question=q, answer="a",
                                           tools_used=[], via="llm", model="m", duration_ms=1,
                                           outcome="ok"))
            await db.commit()
        rec = (await c.get("/api/agent/prompts", headers=Ha)).json()["recent"]
        chk(rec[0] == "这月销售额多少", f"最近的在前 → {rec}")
        chk(rec.count("2026-080 卡在哪") == 1, "问过两次的只出现一次（去重）")
        chk(not any(q.startswith("[直答]") for q in rec),
            "不含「[直答]…」——那是门户直答写进日志的标签，点了会把它原样发出去")
        chk("x" not in rec, "一个字的噪音不进（多半是误触发出去的）")
        rec_f = (await c.get("/api/agent/prompts", headers=Hf)).json()["recent"]
        chk(rec_f == [], "别人问过的不会出现在我这里")

        print("\n=== 3. 我的收藏 ===")
        r = await c.put("/api/agent/prompts/saved", headers=Ha, json={
            "items": ["  2026-080 卡在哪  ", "", "今日晨报", "今日晨报", "长" * 500]})
        chk(r.status_code == 200, f"保存 → {r.status_code}")
        sv = r.json()["saved"]
        chk(sv[0] == "2026-080 卡在哪", f"去掉首尾空白 → {sv[0]!r}")
        chk(sv.count("今日晨报") == 1 and "" not in sv, "去空、去重")
        chk(all(len(x) <= P.MAX_LEN for x in sv), f"限长 {P.MAX_LEN}")
        r = await c.put("/api/agent/prompts/saved", headers=Ha,
                        json={"items": [f"问题{i}" for i in range(50)]})
        chk(len(r.json()["saved"]) == P.MAX_SAVED, f"封顶 {P.MAX_SAVED} 条 → {len(r.json()['saved'])}")
        got = (await c.get("/api/agent/prompts", headers=Ha)).json()["saved"]
        chk(got == r.json()["saved"], "GET 取回来和存进去的一致")
        chk((await c.get("/api/agent/prompts", headers=Hf)).json()["saved"] == [],
            "各人各存各的：财务那边是空的")
        r = await c.put("/api/agent/prompts/saved", headers=Ha, json={"items": []})
        chk(r.json()["saved"] == [], "能清空")

        print("\n=== 4. 没登录不给 ===")
        r = await c.get("/api/agent/prompts")
        chk(r.status_code == 401, f"未登录 401 → {r.status_code}")


asyncio.run(main())
print("\n" + ("全部通过 ✅" if not FAIL else f"{len(FAIL)} 条失败 ❌"))
for m in FAIL:
    print(" -", m)
sys.exit(1 if FAIL else 0)
