"""🆕 合并开票：同客户多项目，一份申请 + 一张合并发票，整组一次审批/开票。

口径(用户 2026-06-17 确认)：必须同客户；一份申请、一张合并发票，主管/财务整组一次处理。
覆盖：同客户合并申请成功(共享申请文件+同 batch) / 跨客户拒绝 / 不足2个拒绝 /
      主管整组审批→财务待开票 / 财务一张合并发票→整组已开票(共享发票) /
      批次内单项目审批/开票被拦截 / 驳回整组回退。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="mergeinv")
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

        await mk("s1", "sales", "赵仁辉")
        await mk("sl", "sales_lead", "钱主管")
        await mk("fin", "finance", "孙财务")
        Hs1, Hsl, Hfin = await login("s1"), await login("sl"), await login("fin")

        async def order(name, customer):
            r = await c.post("/api/sales/orders", headers=Hs1, json={
                "name": name, "customer": customer, "cust_type": "经销商", "contract": "有",
                "amount": 50000, "tax_rate": "13%", "prepay": 0, "before_ship": 0,
                "ship_receivable": 0, "balance": 0, "balance_date": "",
                "depts": ["design"], "receiver": {"name": "a", "phone": "1", "addr": "b"}})
            assert r.status_code == 200, r.text
            return r.json()["project_id"]

        async def ledger_id_of(pid):
            rows = (await c.get("/api/sales/ledger", headers=Hs1)).json()["rows"]
            return next(x["id"] for x in rows if x["project_id"] == pid)

        def upload(name="merge_apply.pdf"):
            return {"file": (name, b"%PDF-1.4 merge", "application/pdf")}

        # 同客户「甲」两个项目 002A/002B + 异客户「乙」一个
        a1 = await ledger_id_of(await order("机A", "客户甲"))
        a2 = await ledger_id_of(await order("机B", "客户甲"))
        b1 = await ledger_id_of(await order("机C", "客户乙"))

        # 跨客户合并 → 400
        r = await c.post("/api/sales/invoice-apply-merge", headers=Hs1,
                         data={"ledger_ids": f"{a1},{b1}"}, files=upload())
        chk(r.status_code == 400, f"跨客户合并应拒绝: {r.status_code} {r.text[:80]}")
        # 不足2个 → 400
        r = await c.post("/api/sales/invoice-apply-merge", headers=Hs1,
                         data={"ledger_ids": f"{a1}"}, files=upload())
        chk(r.status_code == 400, f"不足2个应拒绝: {r.status_code}")

        # 同客户合并申请 → 200
        r = await c.post("/api/sales/invoice-apply-merge", headers=Hs1,
                         data={"ledger_ids": f"{a1},{a2}"}, files=upload())
        chk(r.status_code == 200, f"同客户合并申请: {r.status_code} {r.text[:120]}")

        # 两条进入 applying + 同 batch_id + 共享申请文件
        rows = {x["id"]: x for x in (await c.get("/api/sales/ledger", headers=Hs1)).json()["rows"]}
        chk(rows[a1]["invoice_state"] == "applying" and rows[a2]["invoice_state"] == "applying",
            "两项目均待审批")
        bid = rows[a1]["invoice_batch_id"]
        chk(bid and rows[a2]["invoice_batch_id"] == bid, f"共享同一 batch_id: {bid}")
        chk(rows[a1]["invoice_apply_file_id"] == rows[a2]["invoice_apply_file_id"] is not None,
            "共享同一份申请文件")

        # 主管审批列表含这两条；批次内单项目审批被拦截
        r = await c.post(f"/api/sales/ledger/{a1}/invoice-approve", headers=Hsl)
        chk(r.status_code == 400, f"批次内单项目审批应被拦截: {r.status_code} {r.text[:80]}")

        # 主管整组审批 → 两条 pending_invoice
        r = await c.post(f"/api/sales/invoice-batch/{bid}/approve", headers=Hsl)
        chk(r.status_code == 200, f"整组审批: {r.status_code} {r.text[:120]}")
        # 财务待开票列表含这两条，带 batch_id
        pend = (await c.get("/api/finance/pending-invoices", headers=Hfin)).json()
        pend_batch = [x for x in pend if x["invoice_batch_id"] == bid]
        chk(len(pend_batch) == 2, f"财务待开票含合并组2条: {len(pend_batch)}")

        # 批次内单项目开票被拦截
        r = await c.post(f"/api/sales/ledger/{a1}/invoice-upload", headers=Hfin, files=upload("x.pdf"))
        chk(r.status_code == 400, f"批次内单项目开票应被拦截: {r.status_code}")

        # 财务一张合并发票 → 整组已开票 + 共享发票文件
        r = await c.post(f"/api/sales/invoice-batch/{bid}/invoice-upload", headers=Hfin,
                         files=upload("merge_invoice.pdf"))
        chk(r.status_code == 200, f"合并开票上传: {r.status_code} {r.text[:120]}")
        rows = {x["id"]: x for x in (await c.get("/api/sales/ledger", headers=Hs1)).json()["rows"]}
        chk(rows[a1]["invoice_state"] == "invoiced" and rows[a2]["invoice_state"] == "invoiced",
            "两项目均已开票")
        chk(rows[a1]["invoice_file_id"] == rows[a2]["invoice_file_id"] is not None,
            "共享同一张合并发票")

        # 销售本人收到合并发票通知
        msgs = (await c.get("/api/messages?limit=100", headers=Hs1)).json()
        chk(any("合并发票已开" in m["text"] for m in msgs), "销售收到合并发票通知")

        # ===== 驳回路径：新建一组 → 主管驳回 → 整组回退 None、batch 清空 =====
        d1 = await ledger_id_of(await order("机D", "客户丙"))
        d2 = await ledger_id_of(await order("机E", "客户丙"))
        r = await c.post("/api/sales/invoice-apply-merge", headers=Hs1,
                         data={"ledger_ids": f"{d1},{d2}"}, files=upload())
        rows = {x["id"]: x for x in (await c.get("/api/sales/ledger", headers=Hs1)).json()["rows"]}
        bid2 = rows[d1]["invoice_batch_id"]
        r = await c.post(f"/api/sales/invoice-batch/{bid2}/reject", headers=Hsl)
        chk(r.status_code == 200, f"整组驳回: {r.status_code} {r.text[:120]}")
        rows = {x["id"]: x for x in (await c.get("/api/sales/ledger", headers=Hs1)).json()["rows"]}
        chk(rows[d1]["invoice_state"] is None and rows[d1]["invoice_batch_id"] is None,
            "驳回后回退未申请且清批次")
        chk(rows[d2]["invoice_apply_file_id"] is None, "驳回后清申请文件")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
