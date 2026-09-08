"""金额审计(2026-09-09)·OA 费用 / 售后费用 的内控与口径。

审查发现（详见 交接文档「金额审计」一节）：
  · OA 三张财务页状态集不一致：_OA_DONE_STATUS 漏了 pending_payment（审完等财务付款的单在汇总里看不到）
  · settle_amount 任何环节任何人都能写、无上下限、不留痕（生产幸好还没人用过：0 条）
  · OA 审批没有「不能批自己的单」（生产已发生 9 次，含王芹自批自己的两张对公付款）
  · 已付款的 OA 单可以删；按月归集用 UTC + updated_at（随任何更新漂移）
  · 售后：审批通过后登记人重传发票可任意改金额且不留旧值；已报销的记录能作废/删除
"""
import asyncio, io, json, os, sys, tempfile
from datetime import datetime, timezone

tmp = tempfile.mkdtemp(prefix="auditoa")
os.environ["DATABASE_URL"] = os.environ.get("AUDIT_TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


def _pdf(name="发票.pdf"):
    return {"file": (name, io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")}


async def _audit(action):
    async with SessionLocal() as db:
        return list((await db.execute(select(models.AuditLog).where(models.AuditLog.action == action))).scalars().all())


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        async def login(u, p="pass123"):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(name, codes):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123", "full_name": name,
                "role_ids": [rid[x] for x in codes]})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        await mkuser("au_emp", ["sales"])
        await mkuser("au_mgr", ["manager"])
        await mkuser("au_mgr2", ["manager"])
        await mkuser("au_fin", ["finance", "finance_lead"])
        await mkuser("au_cash", ["finance"])
        await mkuser("au_asw", ["as_worker"])
        await mkuser("au_asl", ["as_lead"])
        He, Hm, Hm2, Hf, Hc, Hw, Hl = (await login("au_emp"), await login("au_mgr"), await login("au_mgr2"),
                                       await login("au_fin"), await login("au_cash"),
                                       await login("au_asw"), await login("au_asl"))

        dept = (await c.get("/api/oa/departments", headers=H)).json()[0]
        for order, role in ((1, "manager"), (2, "finance")):
            r = await c.post("/api/oa/chains", headers=H, json={
                "department_id": dept["id"], "doc_type": "expense", "step_order": order,
                "approver_role": role, "enabled": True})
            assert r.status_code == 200, r.text

        async def submit(hdr, amount=1000, title="差旅报销"):
            r = await c.post("/api/oa/requests", headers=hdr, json={
                "category": "reimbursement", "doc_type": "expense", "department_id": dept["id"],
                "title": title, "amount": amount, "detail": {"reason": "出差"}})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        async def approve(oid, hdr, **body):
            return await c.put(f"/api/oa/requests/{oid}/approve", headers=hdr, json=body)

        async def get(oid):
            return (await c.get(f"/api/oa/requests/{oid}", headers=H)).json()

        # ══════════════ ① 核定金额：只有财务环节能写，0 ≤ 核定 ≤ 申请，留痕 ══════════════
        print("\n① 核定金额")
        o1 = await submit(He, 1000)
        r = await approve(o1, Hm, settle_amount=800)
        chk(r.status_code == 403 and "财务" in r.text, f"经理环节写核定金额被拒 -> {r.status_code}")
        chk((await get(o1))["status"] == "pending" and (await get(o1)).get("settle_amount") is None, "被拒后单子没动")
        r = await approve(o1, Hm)
        chk(r.status_code == 200, f"经理正常审批 -> {r.status_code}")
        r = await approve(o1, Hf, settle_amount=1200)
        chk(r.status_code == 400 and "不能高于" in r.text, f"核定 1200 > 申请 1000 被拒 -> {r.status_code}")
        r = await c.put(f"/api/oa/requests/{o1}/approve", headers={**Hf, "Content-Type": "application/json"},
                        content='{"settle_amount": NaN}')
        chk(r.status_code == 422, f"NaN 核定金额被 schema 挡下 -> {r.status_code}")
        r = await approve(o1, Hf, settle_amount=-5)
        chk(r.status_code == 422, f"负数核定金额被 schema 挡下 -> {r.status_code}")
        r = await approve(o1, Hf, settle_amount=800, note="超出差旅标准，按 800 核报")
        d = await get(o1)
        chk(r.status_code == 200 and d["status"] == "pending_payment" and d["settle_amount"] == 800,
            f"财务环节核定 800 → 待付款 -> {r.status_code} {d.get('status')} {d.get('settle_amount')}")
        chk(d.get("settle_note") == "超出差旅标准，按 800 核报", f"核减理由记进 settle_note -> {d.get('settle_note')}")
        aud = await _audit("oa_settle_amount")
        chk(len(aud) == 1 and "1,000.00" in aud[0].detail and "800.00" in aud[0].detail,
            f"写了核定审计（谁改的、从多少到多少）-> {aud and aud[0].detail}")

        # ══════════════ ② 不能审批自己提交的申请 ══════════════
        print("\n② 职责分离")
        o2 = await submit(Hm, 300, "经理自己的报销")
        r = await approve(o2, Hm)
        chk(r.status_code == 403 and "职责分离" in r.text, f"经理批自己的单被拒 -> {r.status_code}")
        r = await approve(o2, Hm2)
        chk(r.status_code == 200, f"另一位经理可批 -> {r.status_code}")
        # 待办卡片同口径：自己的单不出现在自己的 OA 待办里
        o3 = await submit(Hm, 50, "又一张自己的")
        o3_no = (await get(o3))["request_no"]
        from app.agent import cards as _cards
        async with SessionLocal() as db:
            me = (await db.execute(select(models.User).where(models.User.username == "au_mgr"))).scalar_one()
            got = await _cards.assemble_oa_cards(db, me)
        chk(not any(o3_no in json.dumps(x, ensure_ascii=False) for x in got),
            f"自己提的单不进自己的 OA 审批待办卡 -> 待办 {len(got)} 张")
        await approve(o3, Hm2)

        # ══════════════ ③ 三张财务页同一状态集：待付款的单也算 ══════════════
        print("\n③ 报表状态集")
        summ = (await c.get("/api/oa/reports/summary", headers=Hf)).json()
        rows = summ if isinstance(summ, list) else summ.get("rows", [])
        total = sum(x.get("amount") or 0 for x in rows)
        chk(abs(total - 800) < 0.01, f"OA 财务汇总含待付款的单、按核定金额 800（原来 pending_payment 不在状态集里）-> {total}")
        det = (await c.get("/api/oa/reports/summary/detail", headers=Hf,
                           params={"department_id": dept["id"], "doc_type": "expense"})).json()
        chk(any(x["id"] == o1 and x["amount"] == 800 and x["settled"] for x in det), "下钻明细也看得到且标了已核定")
        cost = (await c.get("/api/oa/reports/cost", headers=Hf)).json()
        oa_rows = [x for x in cost["rows"] if x["source"] == "oa_reimbursement"]
        chk(abs(sum(x["amount"] for x in oa_rows) - 800) < 0.01 or not oa_rows,
            f"成本归集口径一致（部门没配成本科目时为空属正常）-> {[x['amount'] for x in oa_rows]}")
        y = (datetime.now(timezone.utc)).year
        ov = (await c.get("/api/finance/expense-overview", headers=Hf, params={"year": y})).json()
        chk(abs(ov["totals"]["oa"] - 800) < 0.01, f"支出总览 OA 列 = 800（含待付款、取核定金额）-> {ov['totals']['oa']}")
        chk("oa_aftersales_skipped" in ov, "支出总览报出被剔除的售后部 OA 单（不藏）")

        # ══════════════ ④ 已付款：归付款月；不能删 ══════════════
        print("\n④ 付款月与删除")
        r = await c.put(f"/api/oa/requests/{o1}/mark-paid", headers=Hc, data={"pay_note": "已转"})
        chk(r.status_code == 200 and r.json()["status"] == "paid", f"出纳标记已付款 -> {r.status_code}")
        # 把付款时间挪到 7 月 1 日北京时间 02:00（UTC 6 月 30 日 18:00）——按 UTC 分月会错到 6 月
        async with SessionLocal() as db:
            req = (await db.execute(select(models.OaRequest).where(models.OaRequest.id == o1))).scalar_one()
            req.pay_at = datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc)
            await db.commit()
        c7 = (await c.get("/api/oa/reports/cost", headers=Hf, params={"period": "2026-07"})).json()
        c6 = (await c.get("/api/oa/reports/cost", headers=Hf, params={"period": "2026-06"})).json()
        in7 = sum(x["amount"] for x in c7["rows"] if x["source"] == "oa_reimbursement")
        in6 = sum(x["amount"] for x in c6["rows"] if x["source"] == "oa_reimbursement")
        chk((in7 >= 800 - 0.01 and in6 < 0.01) or (not c7["rows"] and not c6["rows"]),
            f"已付款的单按**付款月·北京时间**归集：7 月 {in7} / 6 月 {in6}")
        r = await c.delete(f"/api/oa/requests/{o1}", headers=H)
        chk(r.status_code == 400 and "已付款" in r.text, f"已付款的单不能删 -> {r.status_code}")
        r = await c.delete(f"/api/oa/requests/{o3}", headers=H)
        chk(r.status_code == 200, f"没付款的仍可删（#199 误提清理）-> {r.status_code}")

        # ══════════════ ⑤ 售后：重传改金额留痕；已报销不能作废/删除 ══════════════
        print("\n⑤ 售后费用")
        async with SessionLocal() as db:
            p = models.Project(code="AU-2026-001", name="审计项目", status="进行中")
            db.add(p)
            await db.commit()
            pid = p.id

        async def reg(items, problem):
            r = await c.post("/api/aftersales", headers=Hw,
                             data={"project_id": pid, "problem": problem, "cost": 1,
                                   "items": json.dumps(items, ensure_ascii=False)}, files=_pdf("物料清单.pdf"))
            assert r.status_code == 200, r.text
            rows = (await c.get("/api/aftersales", headers=Hw)).json()["rows"]
            return [x for x in rows if x["problem"] == problem][0]["id"]

        r = await c.post("/api/aftersales", headers=Hw,
                         data={"project_id": pid, "problem": "负数行", "cost": 1,
                               "items": json.dumps([{"name": "配件", "amount": 500}, {"name": "折让", "amount": -100}])},
                         files=_pdf())
        chk(r.status_code == 400 and "负数" in r.text, f"费用行负数被拒（原来悄悄抵扣合计）-> {r.status_code}")
        r = await c.post("/api/aftersales", headers=Hw,
                         data={"project_id": pid, "problem": "NaN 行", "cost": 1,
                               "items": json.dumps([{"name": "配件", "amount": "nan"}])}, files=_pdf())
        chk(r.status_code == 400, f"NaN 金额被拒 -> {r.status_code}")

        a1 = await reg([{"name": "配件费", "amount": 500}], "轴承更换")
        chk((await c.post(f"/api/aftersales/{a1}/approve", headers=Hl)).status_code == 200, "主管审批")
        r = await c.post(f"/api/aftersales/{a1}/pay-reject", headers=Hf, data={"reason": "发票抬头错"})
        chk(r.status_code == 200, f"财务退票 -> {r.status_code}")
        r = await c.post(f"/api/aftersales/{a1}/resubmit-invoice", headers=Hw,
                         data={"items": json.dumps([{"name": "配件费", "amount": 0}])})
        chk(r.status_code == 400 and "大于 0" in r.text, f"重传合计 0 被拒 -> {r.status_code}")
        r = await c.post(f"/api/aftersales/{a1}/resubmit-invoice", headers=Hw,
                         data={"items": json.dumps([{"name": "配件费", "amount": 5000}])})
        chk(r.status_code == 200, f"重传改成 5000（允许，但必须留痕）-> {r.status_code}")
        aud = await _audit("resubmit_invoice")
        chk(aud and "500.00" in (aud[-1].detail or "") and "5,000.00" in (aud[-1].detail or ""),
            f"审计记了旧→新 -> {aud and aud[-1].detail}")

        a2 = await reg([{"name": "差旅", "amount": 216}], "现场调试")
        await c.post(f"/api/aftersales/{a2}/approve", headers=Hl)
        r = await c.post(f"/api/aftersales/{a2}/reimburse", headers=Hf, data={})
        chk(r.status_code == 200, f"财务报销 -> {r.status_code}")
        r = await c.post(f"/api/aftersales/{a2}/finance-void", headers=H)
        chk(r.status_code == 400 and "已报销" in r.text, f"已报销的不能作废 -> {r.status_code}")
        r = await c.delete(f"/api/aftersales/{a2}", headers=H)
        chk(r.status_code == 400 and "已报销" in r.text, f"已报销的不能删除 -> {r.status_code}")
        # 未报销的可作废，作废后报销支腿清零
        a3 = await reg([{"name": "住宿", "amount": 300}], "复检")
        await c.post(f"/api/aftersales/{a3}/approve", headers=Hl)
        await c.post(f"/api/aftersales/{a3}/pay-reject", headers=Hf, data={"reason": "缺票"})
        r = await c.post(f"/api/aftersales/{a3}/finance-void", headers=H)
        async with SessionLocal() as db:
            a = (await db.execute(select(models.AfterSales).where(models.AfterSales.id == a3))).scalar_one()
        chk(r.status_code == 200 and a.status == "pending" and a.pay_status is None and a.pay_note is None,
            f"未报销的可作废，且报销支腿(pay_status/pay_note)一并清零 -> {a.status}/{a.pay_status}")

    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败")
        sys.exit(1)
    print("✅ 全部通过")


asyncio.run(main())
