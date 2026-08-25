"""🆕 反馈#285 OA「付款申请」单据类型测试：
1. 启动迁移后单据类型字典含 payment（业务申请类/付款申请/启用）——提交表单下拉可见；
2. 配好审批链后可提交：金额/收款单位/付款事由/期望付款日期落入 detail，标题默认取类型名；
3. 服务端必填校验：缺收款单位/金额缺失或≤0/缺付款事由（含纯空白）均 400；
4. 列表可见：scope=mine 可见、doc_type=payment_public 过滤生效；
5. 通用待付款闭环（不为新类型单独开路）：末环节 finance 审批通过→待付款→标记已付款(paid)。
   ⚠️ 2026-08-25 起（#412）：对公必须传付款凭证；标记后状态留在 paid，不再写回 approved。

⚠️ 2026-08-07 起「付款申请」按反馈拆成 对公(payment_public) / 现金(payment_cash)——
   审批流程不一样。旧的 payment 已停用（在途旧单仍走完），所以本用例整体迁到
   payment_public。**不是放松校验**：对公的必填口径跟原来一模一样。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="oa_pay")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns

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
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'admin','password':'admin123'})).json()['access_token']}"}

        # ===== 1. 单据类型字典含 payment =====
        r = await c.get("/api/oa/doc-types", headers=H)
        chk(r.status_code == 200, f"单据类型列表: {r.status_code}")
        dt = {d["key"]: d for d in r.json()}
        chk("payment_public" in dt, f"字典含 payment: {sorted(dt)}")
        if "payment_public" in dt:
            p = dt["payment_public"]
            chk(p["category"] == "business" and p["label"] == "对公付款申请" and p["enabled"],
                f"payment_public 类目/名称/启用: {p}")

        # 部门（反馈来自 hr，用「人事部」走流程）+ 审批链：一步财务审批
        depts = (await c.get("/api/oa/departments", headers=H)).json()
        dept_id = next(d["id"] for d in depts if d["name"] == "人事部")
        r = await c.post("/api/oa/chains", headers=H,
                         json={"department_id": dept_id, "doc_type": "payment_public",
                               "step_order": 1, "approver_role": "finance"})
        chk(r.status_code == 200, f"配置付款申请审批链: {r.status_code} {r.text[:150]}")

        # ===== 2. 必填校验（缺字段 → 400） =====
        base = {"category": "business", "doc_type": "payment_public", "department_id": dept_id,
                "amount": 12800.5,
                # 🆕 反馈#348 起收款账号/开户行也是必填（财务批完要直接打款），
                #    老 payload 没有这两项会被 400 挡在门外——补上，不是放松校验。
                "detail": {"payee": "苏州某某供应商有限公司", "reason": "2026-061M 项目尾款",
                           "payee_account": "6222021234567890",
                           "payee_bank": "工商银行苏州分行营业部",
                           "expect_pay_date": "2026-07-31"}}
        bad_cases = [
            ({**base, "detail": {**base["detail"], "payee": ""}}, "缺收款单位"),
            ({**base, "detail": {**base["detail"], "payee": "   "}}, "收款单位纯空白"),
            ({**base, "amount": None}, "缺付款金额"),
            ({**base, "amount": 0}, "付款金额为0"),
            ({**base, "detail": {**base["detail"], "reason": ""}}, "缺付款事由"),
            ({**base, "detail": {**base["detail"], "payee_account": ""}}, "缺收款账号"),
            ({**base, "detail": {**base["detail"], "payee_bank": ""}}, "缺开户行"),
        ]
        for body, name in bad_cases:
            r = await c.post("/api/oa/requests", headers=H, json=body)
            chk(r.status_code == 400, f"必填校验[{name}] 400: {r.status_code} {r.text[:120]}")

        # ===== 3. 完整提交 → 200，字段落库 =====
        r = await c.post("/api/oa/requests", headers=H, json=base)
        chk(r.status_code == 200, f"付款申请可提交: {r.status_code} {r.text[:200]}")
        req = r.json() if r.status_code == 200 else {}
        rid = req.get("id")
        if req:
            chk(req["doc_type"] == "payment_public" and req["category"] == "business",
                f"doc_type/category: {req['doc_type']}/{req['category']}")
            chk(req["title"] == "对公付款申请", f"标题默认取类型名: {req['title']}")
            chk(abs((req["amount"] or 0) - 12800.5) < 1e-6, f"金额: {req['amount']}")
            d = req["detail"]
            chk(d.get("payee") == "苏州某某供应商有限公司" and d.get("reason") == "2026-061M 项目尾款"
                and d.get("expect_pay_date") == "2026-07-31", f"detail 三字段: {d}")
            chk(req["status"] == "pending" and len(req["steps"]) == 1
                and req["steps"][0]["approver_role"] == "finance",
                f"审批链快照: status={req['status']} steps={req['steps']}")

        # ===== 4. 列表可见 + doc_type 过滤 =====
        r = await c.get("/api/oa/requests", headers=H, params={"scope": "mine"})
        chk(any(x["id"] == rid for x in r.json()), "scope=mine 可见该付款申请")
        r = await c.get("/api/oa/requests", headers=H, params={"scope": "mine", "doc_type": "payment_public"})
        chk([x["id"] for x in r.json()] == [rid], f"doc_type=payment_public 过滤: {[x['id'] for x in r.json()]}")
        r = await c.get("/api/oa/requests", headers=H, params={"scope": "mine", "doc_type": "expense"})
        chk(not any(x["id"] == rid for x in r.json()), "doc_type=expense 过滤不含付款申请")

        # ===== 5. 通用待付款闭环：finance 末环节批准 → 待付款 → 标记已付款 =====
        r = await c.put(f"/api/oa/requests/{rid}/approve", headers=H, json={})
        chk(r.status_code == 200 and r.json()["status"] == "pending_payment",
            f"末环节finance批准→待付款: {r.status_code} {r.json().get('status')}")
        r = await c.get("/api/oa/requests", headers=H, params={"scope": "pending_pay"})
        chk(any(x["id"] == rid for x in r.json()), "待付款队列可见")
        # ⚠️ 2026-08-25（反馈#412）这一步的口径变了两处，本用例跟着改：
        #   ① 对公付款**必须传付款凭证**（现金/对私不强制）——财务月底要凭它对账；
        #   ② 标记之后状态留在 **paid**，不再写回 approved。
        #      原来写回 approved 的后果：付过款的单和只是批过的单在列表上一模一样，
        #      杨坛看到的全是「已通过」，判断不了钱走没走（线上 44 单就是这个样子）。
        r = await c.put(f"/api/oa/requests/{rid}/mark-paid", headers=H)
        chk(r.status_code == 400,
            f"对公付款不传凭证要被拦: {r.status_code} {str(r.json())[:70]}")
        import io as _io
        r = await c.put(f"/api/oa/requests/{rid}/mark-paid", headers=H,
                        files={"file": ("回单.pdf", _io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")})
        chk(r.status_code == 200 and r.json()["status"] == "paid",
            f"标记已付款→已付款(paid，不再写回已通过): {r.status_code} {r.json().get('status')}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
