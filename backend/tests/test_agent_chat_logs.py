"""🆕 AI 助手审计日志测试：
1. 规则降级 chat 后库里落 1 条日志：user_id/username/question/answer 非空、via="rule"、
   model="rule-fallback"、tools_used 与实际调用工具一致、duration_ms/created_at 非空；
2. 截断：超长问题（>5000）入库截到 5000；_log_chat 直调验证 answer 同样截到 5000；
3. LLM 失败自动降级：via="rule" 且 model 形如 "rule-fallback:<原因简述>"；
4. GET /api/agent/chat-logs：admin 可拉（字段契约齐全、按时间倒序、username 模糊过滤、
   分页 size 上限 100）；普通用户 403；未登录 401。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="agentlog")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.environ.pop("AGENT_LLM_API_KEY", None)   # 强制走规则降级路径
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models
from app.routers import agent_router

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)


async def rows():
    async with SessionLocal() as db:
        return list((await db.execute(
            select(models.AgentChatLog).order_by(models.AgentChatLog.id))).scalars().all())


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'admin','password':'admin123'})).json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        r = await c.post("/api/admin/users", headers=H,
                         json={"username": "b1", "password": "pass123", "full_name": "采购员一", "role_id": rid["buyer"]})
        assert r.status_code == 200, r.text
        Hb = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'b1','password':'pass123'})).json()['access_token']}"}
        async with SessionLocal() as db:
            b1 = (await db.execute(select(models.User).where(models.User.username == "b1"))).scalar_one()
            b1_id = b1.id

        # ===== 1. 规则降级 chat → 落 1 条审计日志，字段正确 =====
        r = await c.post("/api/agent/chat", headers=Hb, json={"message": "采购未到货"})
        j = r.json() if r.status_code == 200 else {}
        chk(r.status_code == 200 and j.get("fallback") is True, f"b1 规则降级 chat 200: {r.status_code} {r.text[:150]}")
        rs = await rows()
        chk(len(rs) == 1, f"chat 后落 1 条日志: {len(rs)}")
        if rs:
            row = rs[0]
            chk(row.user_id == b1_id, f"user_id=提问人: {row.user_id} vs {b1_id}")
            chk(row.username == "b1", f"username 快照: {row.username}")
            chk(row.question == "采购未到货", f"question 原文: {row.question!r}")
            chk(bool(row.answer) and "未到货" in row.answer, f"answer 非空且为回复原文: {row.answer[:80]!r}")
            chk(row.via == "rule", f"via=rule: {row.via}")
            chk(row.model == "rule-fallback", f"model=rule-fallback: {row.model}")
            chk(row.tools_used == ["po_arrival_overdue"], f"tools_used 与实际工具一致: {row.tools_used}")
            chk(row.duration_ms is not None and row.duration_ms >= 0, f"duration_ms 非空: {row.duration_ms}")
            chk(row.created_at is not None, f"created_at 非空: {row.created_at}")

        # ===== 2. 超长问题截断到 5000 =====
        long_q = "未到货" + "长" * 6000
        r = await c.post("/api/agent/chat", headers=Hb, json={"message": long_q})
        chk(r.status_code == 200, f"超长问题 chat 仍 200: {r.status_code}")
        rs = await rows()
        chk(len(rs) == 2, f"第二次 chat 后 2 条日志: {len(rs)}")
        if len(rs) == 2:
            chk(len(rs[1].question) == 5000, f"超长问题截到 5000: {len(rs[1].question)}")
            chk(rs[1].question.startswith("未到货"), "截断保留开头内容")
            chk(rs[1].tools_used == ["po_arrival_overdue"], f"长问题仍路由到采购工具: {rs[1].tools_used}")

        # answer 截断：直调 _log_chat 验证（规则模板造不出 5000+ 回答）
        async with SessionLocal() as db:
            admin = (await db.execute(select(models.User).where(models.User.username == "admin"))).scalar_one()
            await agent_router._log_chat(db, admin, "直调问题", "答" * 6000,
                                         ["balance_due"], "rule", "rule-fallback", 5)
        rs = await rows()
        chk(len(rs) == 3, f"直调后 3 条日志: {len(rs)}")
        if len(rs) == 3:
            chk(len(rs[2].answer) == 5000, f"answer 截到 5000: {len(rs[2].answer)}")
            chk(rs[2].tools_used == ["balance_due"] and rs[2].duration_ms == 5,
                f"直调字段正确: {rs[2].tools_used}/{rs[2].duration_ms}")

        # ===== 3. LLM 失败降级 → via=rule，model=rule-fallback:<原因> =====
        r = await c.put("/api/agent/config", headers=H,
                        json={"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-fake-unreachable"})
        chk(r.status_code == 200, f"配假 key: {r.status_code} {r.text[:120]}")
        r = await c.post("/api/agent/chat", headers=H, json={"message": "今日晨报"})
        j = r.json() if r.status_code == 200 else {}
        chk(r.status_code == 200 and j.get("fallback") is True, f"LLM 不可达自动降级: {r.status_code}")
        rs = await rows()
        chk(len(rs) == 4, f"LLM 失败降级也落日志: {len(rs)}")
        if len(rs) == 4:
            chk(rs[3].via == "rule", f"降级 via=rule: {rs[3].via}")
            chk(rs[3].model.startswith("rule-fallback:") and len(rs[3].model) > len("rule-fallback:"),
                f"model 记失败原因简述: {rs[3].model}")
            chk(rs[3].username == "admin" and rs[3].question == "今日晨报",
                f"降级日志用户/问题正确: {rs[3].username}/{rs[3].question}")
        # 清理配置，避免影响同进程其它断言
        await c.put("/api/agent/config", headers=H,
                    json={"base_url": "-", "api_key": "-", "model": "-", "models": "-"})

        # ===== 4. chat-logs 查询接口 =====
        r = await c.get("/api/agent/chat-logs", headers=H)
        j = r.json() if r.status_code == 200 else {}
        chk(r.status_code == 200, f"admin 拉日志 200: {r.status_code} {r.text[:150]}")
        chk(j.get("total") == 4 and len(j.get("items", [])) == 4, f"total/items: {j.get('total')}/{len(j.get('items', []))}")
        if j.get("items"):
            it = j["items"][0]
            chk(set(it.keys()) == {"id", "username", "question", "answer", "tools_used",
                                   "via", "model", "duration_ms", "created_at"},
                f"items 字段契约: {sorted(it.keys())}")
            ids = [x["id"] for x in j["items"]]
            chk(ids == sorted(ids, reverse=True), f"按时间倒序(最新在前): {ids}")
            chk(it["username"] == "admin", f"最新一条是 admin 的降级日志: {it['username']}")

        # username 模糊过滤
        r = await c.get("/api/agent/chat-logs", headers=H, params={"username": "b1"})
        j = r.json()
        chk(j.get("total") == 2 and all(x["username"] == "b1" for x in j["items"]),
            f"username=b1 过滤出 2 条: {j.get('total')}")
        r = await c.get("/api/agent/chat-logs", headers=H, params={"username": "不存在的人"})
        chk(r.json().get("total") == 0 and r.json().get("items") == [], f"过滤无结果: {r.json().get('total')}")

        # 分页：size=1 逐页取；size 上限 100（>100 被 422 拒）
        r = await c.get("/api/agent/chat-logs", headers=H, params={"size": 1, "page": 1})
        j = r.json()
        chk(len(j.get("items", [])) == 1 and j.get("total") == 4, f"size=1 分页: {len(j.get('items', []))}/{j.get('total')}")
        r = await c.get("/api/agent/chat-logs", headers=H, params={"size": 1, "page": 4})
        chk(len(r.json().get("items", [])) == 1, "第 4 页仍有 1 条")
        r = await c.get("/api/agent/chat-logs", headers=H, params={"size": 1, "page": 5})
        chk(r.json().get("items") == [], "第 5 页为空")
        r = await c.get("/api/agent/chat-logs", headers=H, params={"size": 200})
        chk(r.status_code == 422, f"size>100 应 422: {r.status_code}")

        # 权限：普通用户 403；未登录 401
        r = await c.get("/api/agent/chat-logs", headers=Hb)
        chk(r.status_code == 403, f"普通用户(buyer) 403: {r.status_code}")
        r = await c.get("/api/agent/chat-logs")
        chk(r.status_code == 401, f"未登录 401: {r.status_code}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
