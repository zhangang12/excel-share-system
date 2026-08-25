"""OA 加「已付款」状态（反馈#412 杨坛，2026-08-25）。

原话：「OA申请里面的状态审批完成后加一个已付款，对公的上传付款凭证 对私的可以不上传
由最后一个审批人修改成已付款的状态」

查下来这套东西**几乎全都已经存在**：待付款状态、标记已付款接口、付款备注、
付款回单上传、前端弹窗，全是现成的。唯一的问题是 `mark_paid` 最后把状态**写回了 approved**，
于是付过款的单和只是批过的单在列表上一模一样——线上 44 单填了付款时间、状态却都是「已通过」。
（付款类单据的审批链末步本来就是 finance，所以「由最后一个审批人改」这条现在就成立。）

所以本次只改三件事，本文件逐条焊住：
  1. 标记已付款之后状态留在 paid
  2. 对公必须传凭证、现金/对私不强制
  3. ⚠️ **新状态必须进所有按 approved 取数的报表白名单** —— 漏一个就是"钱花了但报表里没有"。
     oa_router 里那条 `_OA_COST_STATUS` 的注释记的就是上次同款事故，这是第二遍。
"""
import asyncio, io, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb412")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, update
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns, backfill_oa_paid_status
from app import models

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        async def login(u, p):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        await c.post("/api/admin/users", headers=H, json={
            "username": "oafin", "password": "pass123", "full_name": "财务杨",
            "role_ids": [rid["finance"], rid["finance_lead"]]})
        Hf = await login("oafin", "pass123")

        dept = (await c.get("/api/oa/departments", headers=H)).json()[0]

        async def chain(doc_type):
            r = await c.post("/api/oa/chains", headers=H, json={
                "department_id": dept["id"], "doc_type": doc_type, "step_order": 1,
                "approver_role": "finance", "enabled": True})
            assert r.status_code == 200, r.text

        async def submit(doc_type, amount=1000):
            r = await c.post("/api/oa/requests", headers=H, json={
                "category": "business", "doc_type": doc_type, "department_id": dept["id"],
                "title": f"{doc_type} 测试", "amount": amount,
                "detail": {"payee": "某某", "reason": "货款",
                           "payee_account": "6222021234567890", "payee_bank": "工行"}})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        for dt in ("payment_public", "payment_private"):
            await chain(dt)

        # ========== 1) 对公：批完 → 待付款 → 标记已付款 ==========
        pub = await submit("payment_public")
        r = await c.put(f"/api/oa/requests/{pub}/approve", headers=Hf, json={"action": "approve"})
        chk(r.status_code == 200, f"1) 财务审批通过: {r.status_code} {r.text[:80]}")
        st = (await c.get(f"/api/oa/requests/{pub}", headers=H)).json()["status"]
        chk(st == "pending_payment", f"1) 末步是财务 → 进「待付款」（这一步本来就通）: {st}")

        # 对公不传凭证 → 拦住
        r = await c.put(f"/api/oa/requests/{pub}/mark-paid", headers=Hf, data={"pay_note": "8/25 转账"})
        chk(r.status_code == 400 and "凭证" in r.text,
            f"2) **对公不传凭证被拦**（财务月底要凭它对账）: {r.status_code} {r.text[:80]}")
        chk((await c.get(f"/api/oa/requests/{pub}", headers=H)).json()["status"] == "pending_payment",
            "2) 被拦之后状态没变")

        # 传了凭证 → 通过，且状态留在 paid
        r = await c.put(f"/api/oa/requests/{pub}/mark-paid", headers=Hf,
                        data={"pay_note": "8/25 建行尾号6688"},
                        files={"file": ("回单.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")})
        chk(r.status_code == 200, f"3) 传了凭证能标记已付款: {r.status_code} {r.text[:90]}")
        got = (await c.get(f"/api/oa/requests/{pub}", headers=H)).json()
        chk(got["status"] == "paid",
            f"3) **状态留在「已付款」**——改之前这里写回 approved，付没付看不出来: {got['status']}")
        chk(got.get("pay_note") and got.get("pay_at"), "3) 付款备注和时间都落库了")

        # ========== 4) 对私：不传凭证也能标记 ==========
        pri = await submit("payment_private", 500)
        await c.put(f"/api/oa/requests/{pri}/approve", headers=Hf, json={"action": "approve"})
        r = await c.put(f"/api/oa/requests/{pri}/mark-paid", headers=Hf, data={"pay_note": "微信转账"})
        chk(r.status_code == 200,
            f"4) 对私不传凭证也能标记（很多是微信/支付宝，截图不一定拿得到）: {r.status_code} {r.text[:80]}")
        chk((await c.get(f"/api/oa/requests/{pri}", headers=H)).json()["status"] == "paid",
            "4) 对私同样留在「已付款」")

        # ========== 5) ⚠️ 报表口径：已付款的钱不能从报表里消失 ==========
        summ = (await c.get("/api/oa/reports/summary", headers=H)).json()
        total = sum(x.get("amount") or 0 for x in (summ if isinstance(summ, list) else summ.get("rows", [])))
        chk(total >= 1500,
            f"5) **OA 财务汇总里还看得到这 1500 元**——只认 approved 的话，"
            f"付过款的单会从报表里凭空消失: 合计={total}")

        # ⚠️ 不能只看「JSON 里有没有出现 1500 这个字符串」——那种断言永远会过
        #    （返回里一堆 0 和别的数字，随便撞上就算通过）。要查**当月那一行的 oa 金额**。
        from datetime import datetime as _dt
        cur_month = _dt.now().strftime("%Y-%m")
        fin = await c.get("/api/finance/expense-overview", headers=H)
        chk(fin.status_code == 200, f"5) 财务支出总览能取到: {fin.status_code}")
        if fin.status_code == 200:
            rows = fin.json().get("rows", [])
            row = next((x for x in rows if x.get("month") == cur_month), None)
            chk(row is not None and (row.get("oa") or 0) >= 1500,
                f"5) **财务支出总览当月的 OA 金额含这 1500**（只认 approved 的话会漏掉已付款的）: "
                f"{cur_month} → {row}")

        # ========== 6) 已付款的单不能再被审批/驳回 ==========
        r = await c.put(f"/api/oa/requests/{pub}/approve", headers=Hf, json={"action": "approve"})
        chk(r.status_code == 400, f"6) 已付款的不能再审批: {r.status_code}")
        r = await c.put(f"/api/oa/requests/{pub}/mark-paid", headers=Hf,
                        files={"file": ("x.pdf", io.BytesIO(b"x"), "application/pdf")})
        chk(r.status_code == 400, f"6) 也不能重复标记已付款: {r.status_code}")

        # ========== 7) 存量回刷：付过款但状态还是 approved 的要变成 paid ==========
        async with SessionLocal() as db:
            await db.execute(update(models.OaRequest).where(models.OaRequest.id == pub)
                             .values(status="approved"))   # 复刻线上那 44 单的样子
            await db.commit()
        async with SessionLocal() as db:
            res = await backfill_oa_paid_status(db)
        chk(res["updated"] == 1, f"7) 回刷把「填了付款时间却还写着已通过」的单改成已付款: {res}")
        chk((await c.get(f"/api/oa/requests/{pub}", headers=H)).json()["status"] == "paid",
            "7) 回刷后状态是 paid")
        async with SessionLocal() as db:
            again = await backfill_oa_paid_status(db)
        chk(again["updated"] == 0, f"7) 回刷幂等，第二遍不动任何单: {again}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
