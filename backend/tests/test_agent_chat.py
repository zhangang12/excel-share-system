"""🆕 Agent 助手（只读问数 POC）回归测试：
1. 权限：非 admin/manager（buyer）调 POST /api/agent/chat → 403；admin 正常 200。
2. 降级路径（无 LLM key，强制规则模式）：「采购未到货吗」「今日晨报」「AGT-2501 项目进度」
   都能回答，且回复里带真实数据关键词（明细名/项目名/部门名），fallback=true 且 sources 正确。
3. 工具口径：tool_po_arrival_overdue 查得出「预计昨天到货且未收货」的明细，
   查不出已收货的明细；超期天数/供应商/采购单号正确。
"""
import asyncio, json, os, sys, tempfile, shutil
from datetime import datetime, timezone, timedelta

tmp = tempfile.mkdtemp(prefix="agent")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.environ.pop("AGENT_LLM_API_KEY", None)   # 强制走规则降级路径
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

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

CN = timezone(timedelta(hours=8))
TODAY = datetime.now(CN).date()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()
BALANCE_DUE = (TODAY + timedelta(days=5)).isoformat()


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

        # 造一个非管理层账号（采购员）
        r = await c.post("/api/admin/users", headers=H,
                         json={"username": "b1", "password": "pass123", "full_name": "采购员一", "role_id": rid["buyer"]})
        chk(r.status_code == 200, f"建采购员: {r.text[:120]}")
        Hb = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'b1','password':'pass123'})).json()['access_token']}"}

        # ===== 造业务数据（直接 ORM，只读测试不改现有接口行为） =====
        async with SessionLocal() as db:
            sup = models.Supplier(name="测试供应商A")
            db.add(sup); await db.flush()
            proj = models.Project(code="AGT-2501", name="AGENT测试项目")
            db.add(proj); await db.flush()
            # ① 预计昨天到货、未收货 → 应被查出来（超期 1 天）
            it_over = models.PurchaseItem(supplier_id=sup.id, item_name="轴承", po_no="PO-AGT1",
                                          project_code="AGT-2501", expected_arrival=YESTERDAY)
            # ② 预计昨天到货、已收货 → 不应被查出
            it_done = models.PurchaseItem(supplier_id=sup.id, item_name="电机", po_no="PO-AGT2",
                                          project_code="AGT-2501", expected_arrival=YESTERDAY,
                                          arrival_date=TODAY.isoformat())
            # ③ 设计部进行中任务、预计昨天完成 → 逾期
            od = models.DeptOrder(project_id=proj.id, dept="design", status="in_progress",
                                  due_date=YESTERDAY)
            # ④ 尾款 5 万、5 天后到期 → 进入 14 天窗口
            led = models.SalesLedger(project_id=proj.id, customer="测试客户", amount=100000,
                                     balance=50000, balance_date=BALANCE_DUE)
            db.add_all([it_over, it_done, od, led])
            await db.commit()

        # ===== 1. 权限：非管理层 403 =====
        r = await c.post("/api/agent/chat", headers=Hb, json={"message": "晨报"})
        chk(r.status_code == 403, f"buyer 调 /api/agent/chat 应 403: {r.status_code} {r.text[:120]}")
        r = await c.post("/api/agent/chat", json={"message": "晨报"})
        chk(r.status_code == 401, f"未登录应 401: {r.status_code}")

        # ===== 2. 降级路径（无 LLM key → 规则模式，永远可用） =====
        r = await c.post("/api/agent/chat", headers=H, json={"message": "采购未到货吗"})
        j = r.json() if r.status_code == 200 else {}
        chk(r.status_code == 200, f"admin 问采购未到货 200: {r.status_code} {r.text[:200]}")
        chk(j.get("fallback") is True, f"无 key 时 fallback=true: {j}")
        chk("轴承" in j.get("reply", ""), f"回复含未到货明细名「轴承」: {j.get('reply','')[:200]}")
        chk("电机" not in j.get("reply", ""), "回复不含已收货明细「电机」")
        chk("超期" in j.get("reply", ""), "回复含超期描述")
        chk("采购到期未到货" in j.get("sources", []), f"sources 含「采购到期未到货」: {j.get('sources')}")

        r = await c.post("/api/agent/chat", headers=H, json={"message": "今日晨报"})
        j = r.json() if r.status_code == 200 else {}
        chk(r.status_code == 200 and "晨报" in j.get("reply", ""), f"晨报回答: {r.text[:200]}")
        chk("轴承" in j.get("reply", "") and "AGT-2501" in j.get("reply", ""),
            f"晨报含采购/逾期/尾款数据: {j.get('reply','')[:300]}")
        chk("晨报聚合" in j.get("sources", []), f"sources 含「晨报聚合」: {j.get('sources')}")

        r = await c.post("/api/agent/chat", headers=H, json={"message": "AGT-2501 项目进度怎么样"})
        j = r.json() if r.status_code == 200 else {}
        rep = j.get("reply", "")
        chk(r.status_code == 200 and "AGENT测试项目" in rep, f"项目进度含项目名: {rep[:200]}")
        chk("设计部" in rep, f"项目进度含部门任务: {rep[:300]}")
        chk("轴承" in rep, f"项目进度含未到货采购: {rep[:300]}")
        chk("尾款" in rep and "50,000" in rep, f"项目进度含尾款金额: {rep[:300]}")
        chk("项目进度查询" in j.get("sources", []), f"sources 含「项目进度查询」: {j.get('sources')}")

        # 查不到的项目编号 → 如实说查不到，不编造
        r = await c.post("/api/agent/chat", headers=H, json={"message": "ZZZ-9999 进度"})
        chk("查不到" in r.json().get("reply", ""), f"不存在的项目如实回复: {r.text[:150]}")

        # 兜底：无关键词 → 能力说明
        r = await c.post("/api/agent/chat", headers=H, json={"message": "你好"})
        chk("晨报" in r.json().get("reply", ""), f"兜底返回能力说明: {r.text[:150]}")

        # ===== 2b. 模型选择：models 接口 + chat 的 model 入参 =====
        r = await c.get("/api/agent/models", headers=Hb)
        chk(r.status_code == 403, f"buyer 调 /api/agent/models 应 403: {r.status_code}")
        r = await c.get("/api/agent/models", headers=H)
        j = r.json() if r.status_code == 200 else {}
        chk(r.status_code == 200, f"admin 取 models 200: {r.status_code} {r.text[:200]}")
        chk("deepseek-chat" in j.get("models", []) and "deepseek-reasoner" in j.get("models", []),
            f"models 返回白名单: {j.get('models')}")
        chk(j.get("default") == "deepseek-chat", f"default=配置默认模型: {j.get('default')}")
        chk(j.get("llm_enabled") is False, f"未配置 key 时 llm_enabled=false: {j.get('llm_enabled')}")
        chk("api_key" not in j and "key" not in json.dumps(j).lower().replace("llm_enabled", ""),
            f"models 接口不泄露 api_key: {j}")

        # chat 传非法模型 → 400（与现有参数校验风格一致）
        r = await c.post("/api/agent/chat", headers=H, json={"message": "晨报", "model": "gpt-99"})
        chk(r.status_code == 400, f"非法 model 应 400: {r.status_code} {r.text[:150]}")
        # chat 传合法模型（白名单内）：降级路径忽略该参数，仍正常回答
        r = await c.post("/api/agent/chat", headers=H,
                         json={"message": "采购未到货吗", "model": "deepseek-reasoner"})
        j = r.json() if r.status_code == 200 else {}
        chk(r.status_code == 200 and j.get("fallback") is True and "轴承" in j.get("reply", ""),
            f"合法 model 走降级仍正常回答: {r.status_code} {r.text[:200]}")

        # 白名单 helper：默认模型不在 AGENT_LLM_MODELS 里时自动并入（保证默认值总能选到）
        from app.config import settings as _st
        orig_m, orig_ms = _st.agent_llm_model, _st.agent_llm_models
        _st.agent_llm_model, _st.agent_llm_models = "custom-model-x", "m-a, m-b ,m-a"
        try:
            wl = agent_router._model_whitelist()
            chk(wl == ["m-a", "m-b", "custom-model-x"], f"白名单解析(去空白/去重/并入默认): {wl}")
        finally:
            _st.agent_llm_model, _st.agent_llm_models = orig_m, orig_ms

        # ===== 2c. 页面化 LLM 配置（admin 专属，DB 持久化，生效优先级 DB > .env） =====
        # config 接口仅 admin：manager 也 403
        r = await c.post("/api/admin/users", headers=H,
                         json={"username": "mgr1", "password": "pass123", "full_name": "管理层一",
                               "role_id": rid["manager"]})
        chk(r.status_code == 200, f"建 manager: {r.text[:120]}")
        Hm = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'mgr1','password':'pass123'})).json()['access_token']}"}
        r = await c.get("/api/agent/config", headers=Hb)
        chk(r.status_code == 403, f"buyer 调 GET /config 应 403: {r.status_code}")
        r = await c.get("/api/agent/config", headers=Hm)
        chk(r.status_code == 403, f"manager 调 GET /config 应 403: {r.status_code}")
        r = await c.put("/api/agent/config", headers=Hm, json={"base_url": "http://x"})
        chk(r.status_code == 403, f"manager 调 PUT /config 应 403: {r.status_code}")

        # 初始 GET（.env 无 key）：has_key=false，打码为空
        r = await c.get("/api/agent/config", headers=H)
        j = r.json() if r.status_code == 200 else {}
        chk(r.status_code == 200 and j.get("has_key") is False and j.get("api_key_masked") == "",
            f"初始 config 无 key: {j}")
        chk(j.get("base_url", "").startswith("http") and j.get("model") == "deepseek-chat",
            f"初始 config 默认 base_url/model: {j}")

        # PUT 全量配置（假 key + 本地不可达 base_url）：打码、自动并入默认模型、无明文
        KEY = "sk-test12345678abcd"
        r = await c.put("/api/agent/config", headers=H, json={
            "base_url": "http://127.0.0.1:9/v1", "api_key": KEY, "model": "my-model", "models": "m1,m2"})
        j = r.json() if r.status_code == 200 else {}
        chk(r.status_code == 200, f"PUT config 200: {r.status_code} {r.text[:200]}")
        chk(j.get("api_key_masked") == "****abcd" and KEY not in r.text,
            f"api_key 打码且明文不出现在响应: {j}")
        chk(j.get("model") == "my-model" and "my-model" in j.get("models", ""),
            f"默认模型自动并入白名单: {j}")
        r = await c.get("/api/agent/config", headers=H)
        j = r.json()
        chk(j.get("has_key") is True and j.get("api_key_masked") == "****abcd" and KEY not in r.text,
            f"GET config 打码一致且无明文: {j}")
        # models 接口立即反映新配置（DB 覆盖优先于 .env）
        r = await c.get("/api/agent/models", headers=H)
        j = r.json()
        chk(j.get("llm_enabled") is True and j.get("default") == "my-model"
            and j.get("models") == ["m1", "m2", "my-model"],
            f"models 接口走生效配置: {j}")

        # api_key 传空字符串 = 保持不变
        r = await c.put("/api/agent/config", headers=H, json={"api_key": ""})
        chk(r.json().get("api_key_masked") == "****abcd", f"空串不改 key: {r.text[:150]}")
        # 短 key（<=4 位）全打码
        r = await c.put("/api/agent/config", headers=H, json={"api_key": "abc"})
        chk(r.json().get("api_key_masked") == "****" and "abc" not in r.text,
            f"短 key 全打码: {r.text[:150]}")

        # 已配 key（不可达地址）时 chat 走 LLM 主路径 → 调用失败自动降级，仍 200 且 fallback=true
        r = await c.post("/api/agent/chat", headers=H, json={"message": "采购未到货吗"})
        j = r.json() if r.status_code == 200 else {}
        chk(r.status_code == 200 and j.get("fallback") is True and "轴承" in j.get("reply", ""),
            f"假 key 时 LLM 异常自动降级: {r.status_code} {r.text[:200]}")

        # 清除 key（"-"=删库中覆盖回退 .env；测试环境 .env 无 key）→ llm_enabled 变回 false
        r = await c.put("/api/agent/config", headers=H, json={"api_key": "-"})
        chk(r.json().get("has_key") is False and r.json().get("api_key_masked") == "",
            f"清除 key 生效: {r.text[:150]}")
        r = await c.get("/api/agent/models", headers=H)
        chk(r.json().get("llm_enabled") is False, f"清除后 llm_enabled=false: {r.text[:150]}")
        # 清除 base_url/model/models → 回退 .env 默认
        r = await c.put("/api/agent/config", headers=H,
                        json={"base_url": "-", "model": "-", "models": "-"})
        j = r.json()
        chk(j.get("model") == "deepseek-chat" and "deepseek.com" in j.get("base_url", ""),
            f"清除后回退 .env 默认: {j}")
        r = await c.get("/api/agent/models", headers=H)
        chk(r.json().get("default") == "deepseek-chat"
            and r.json().get("models") == ["deepseek-chat", "deepseek-reasoner"],
            f"清除后 models 接口回退默认: {r.text[:150]}")

        # 存库前校验：非法 base_url / 空 models 被拒
        r = await c.put("/api/agent/config", headers=H, json={"base_url": "not-a-url"})
        chk(r.status_code == 400, f"非法 base_url 应 400: {r.status_code}")
        r = await c.put("/api/agent/config", headers=H, json={"models": " , ,"})
        chk(r.status_code == 400, f"空 models 应 400: {r.status_code}")

        # ===== 3. 工具函数口径（直接调用） =====
        async with SessionLocal() as db:
            d = await agent_router.tool_po_arrival_overdue(db)
            names = [x["item_name"] for x in d["items"]]
            chk("轴承" in names, f"工具查出逾期未收货「轴承」: {names}")
            chk("电机" not in names, f"工具查不出已收货「电机」: {names}")
            row = next((x for x in d["items"] if x["item_name"] == "轴承"), None)
            chk(row is not None and row["over_days"] == 1 and row["supplier"] == "测试供应商A"
                and row["po_no"] == "PO-AGT1" and row["project_code"] == "AGT-2501",
                f"轴承行口径(超期1天/供应商/采购单号/项目编号): {row}")

            d = await agent_router.tool_overdue_orders(db)
            chk(d["count"] >= 1 and any(x["dept_name"] == "设计部" and x["project_code"] == "AGT-2501"
                                        and x["over_days"] == 1 for x in d["items"]),
                f"逾期任务口径: {d['items'][:3]}")

            d = await agent_router.tool_balance_due(db)
            chk(any(x["project_code"] == "AGT-2501" and x["balance"] == 50000
                    and x["days"] == 5 for x in d["items"]),
                f"尾款到期口径(5天后到期进窗口): {d['items'][:3]}")

            d = await agent_router.tool_project_status(db, "AGT-2501")
            chk(d.get("found") and d["name"] == "AGENT测试项目"
                and len(d["dept_orders"]) == 1 and d["po_pending_count"] == 1
                and d["ledger"] and d["ledger"]["balance"] == 50000,
                f"项目进度工具聚合: {str(d)[:300]}")

            d = await agent_router.tool_morning_report(db)
            chk(d["po_arrival_overdue"]["count"] >= 1 and d["overdue_orders"]["count"] >= 1
                and d["balance_due"]["count"] >= 1,
                f"晨报四类聚合计数: po={d['po_arrival_overdue']['count']} "
                f"orders={d['overdue_orders']['count']} bal={d['balance_due']['count']}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
