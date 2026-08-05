"""第二批：售后登记自带报销（④）+ 费用按部门归成本（②）。

这两条必须捆在一起验，因为它们互相牵制——单独做任何一条，同一笔售后费用都会被算两遍。
生产上已经抓到实例：售后登记里 2025-120 记了 ¥216，OA 里又有一张
「2025-120行星搅拌机售后维修」¥1,136。

要锁死的口径：
  1. 费用清单可加行、带发票；**总额由明细自动合计**，不接受手填（手填必然跟发票对不上）
  2. 售后主管审批 → 财务核对发票 → 安排报销；发票对不上退回**登记人**重传，
     改完直接回财务这一步，不用主管再批一遍
  3. 退回发票**不能动 status**——费用已经被主管认过了，退的是发票不是费用。
     打回 pending 会让这笔钱从售后成本里消失，月底对不上
  4. 还有行没传发票时不许点「安排报销」
  5. 成本口径：审批通过即计入（**含 pending_payment**，那也是审批完的）；
     金额按核定金额、为空回退申请金额；采购申请不计
  6. **售后成本只认售后登记**，售后部走 OA 的存量报销单不并进合计（否则重复）
  7. 售后部**不能再新提** OA 报销单，报错要指路到售后登记
"""
import asyncio, io, json, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="asreim")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
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


def _f(name="发票.pdf"):
    return {"file": (name, io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")}


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mk(u, rc, fn):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": u, "password": "pass123", "full_name": fn, "role_id": rid[rc]})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        async def login(u):
            r = await c.post("/api/auth/login", json={"username": u, "password": "pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        await mk("asw", "as_worker", "售后小王")
        await mk("asl", "as_lead", "售后主管")
        await mk("fin", "finance", "财务小李")
        await mk("sal", "sales", "销售小张")
        Hw, Hl, Hf, Hs = (await login("asw"), await login("asl"),
                          await login("fin"), await login("sal"))

        # 建个项目，售后挂到它上面
        r = await c.post("/api/projects", headers=H, json={"code": "2026-T99", "name": "成本测试机"})
        pid = r.json()["id"]

        # ===== 1) 费用清单：合计自动算 =====
        items = [{"name": "配件费", "amount": 800},
                 {"name": "差旅", "amount": 350.5},
                 {"name": "住宿", "amount": 249.5}]
        r = await c.post("/api/aftersales", headers=Hw,
                         data={"project_id": pid, "problem": "行星箱体异响，更换轴承",
                               "cost": 999999,          # 故意填个错的，必须被明细合计覆盖
                               "items": json.dumps(items, ensure_ascii=False)},
                         files=_f("物料清单.pdf"))
        chk(r.status_code == 200, f"带费用清单登记: {r.status_code} {r.text[:150]}")
        lst = (await c.get("/api/aftersales", headers=Hw)).json()
        row = lst["rows"][0]
        aid = row["id"]
        chk(abs(row["cost"] - 1400.0) < 0.01,
            f"总额由明细自动合计 800+350.5+249.5=1400，手填的 999999 被覆盖: {row['cost']}")
        chk(len(row["items"]) == 3, f"明细 3 行: {len(row['items'])}")
        chk(row["missing_invoice"] == 3, f"3 行都还没传发票: {row['missing_invoice']}")

        # 空名字带金额要拒
        r = await c.post("/api/aftersales", headers=Hw,
                         data={"project_id": pid, "problem": "x", "cost": 0,
                               "items": json.dumps([{"name": "", "amount": 100}])},
                         files=_f())
        chk(r.status_code == 400 and "费用项" in r.text, f"有金额没填费用项被拒: {r.text[:90]}")

        # ===== 2) 审批 → 进财务核对 =====
        chk((await c.get(f"/api/aftersales", headers=Hf)).status_code == 200, "财务能看列表")
        r = await c.post(f"/api/aftersales/{aid}/approve", headers=Hl)
        chk(r.status_code == 200, f"售后主管审批: {r.text[:100]}")
        row = [x for x in (await c.get("/api/aftersales", headers=Hf)).json()["rows"] if x["id"] == aid][0]
        chk(row["status"] == "approved", f"status=approved（成本口径）: {row['status']}")
        chk(row["pay_status"] == "checking", f"pay_status=checking（待财务核对）: {row['pay_status']}")

        # ===== 4) 还有行没发票，不许安排报销 =====
        r = await c.post(f"/api/aftersales/{aid}/reimburse", headers=Hf, data={})
        chk(r.status_code == 400 and "没传发票" in r.text, f"缺发票不许报销: {r.status_code} {r.text[:90]}")

        # ===== 2b) 财务退回 → 登记人重传 =====
        r = await c.post(f"/api/aftersales/{aid}/pay-reject", headers=Hf, data={"reason": ""})
        chk(r.status_code == 400, "退回必须填原因（否则登记人不知道改什么）")
        r = await c.post(f"/api/aftersales/{aid}/pay-reject", headers=Hf,
                         data={"reason": "差旅那行发票抬头不对"})
        chk(r.status_code == 200, f"财务退回: {r.text[:100]}")
        row = [x for x in (await c.get("/api/aftersales", headers=Hf)).json()["rows"] if x["id"] == aid][0]
        chk(row["pay_status"] == "invoice_fix", f"退回后 pay_status=invoice_fix: {row['pay_status']}")
        # 3) 关键：status 不能被打回，否则钱从售后成本里消失
        chk(row["status"] == "approved", f"退发票不动 status（钱仍在售后成本里）: {row['status']}")

        # 传三张发票再交回
        inv_ids, up_err = [], ""
        for i in range(3):
            r = await c.post("/api/attachments", headers=Hw,
                             data={"biz_type": "aftersales_invoice", "biz_id": aid},
                             files=_f(f"发票{i+1}.pdf"))
            if r.status_code != 200:
                up_err = f"{r.status_code} {r.text[:120]}"
            inv_ids.append(r.json().get("id") if r.status_code == 200 else None)
        chk(all(inv_ids), f"发票上传拿到 id: {inv_ids} {up_err}")

        fixed = [dict(items[i], invoice_file_id=inv_ids[i]) for i in range(3)]
        # 顺便改个金额，验证合计跟着变
        fixed[1]["amount"] = 300
        r = await c.post(f"/api/aftersales/{aid}/resubmit-invoice", headers=Hw,
                         data={"items": json.dumps(fixed, ensure_ascii=False)})
        chk(r.status_code == 200, f"登记人重传发票: {r.status_code} {r.text[:120]}")
        row = [x for x in (await c.get("/api/aftersales", headers=Hf)).json()["rows"] if x["id"] == aid][0]
        chk(row["pay_status"] == "checking", "重传后回到财务核对，不用主管再批")
        chk(abs(row["cost"] - 1349.5) < 0.01, f"改了金额，合计跟着变 800+300+249.5=1349.5: {row['cost']}")
        chk(row["missing_invoice"] == 0, f"发票齐了: {row['missing_invoice']}")

        # 别人不能替登记人重传
        r = await c.post(f"/api/aftersales/{aid}/resubmit-invoice", headers=Hs,
                         data={"items": json.dumps(fixed)})
        chk(r.status_code in (403, 401), f"非登记人不能重传: {r.status_code}")

        # ===== 安排报销 =====
        r = await c.post(f"/api/aftersales/{aid}/reimburse", headers=Hf, data={"note": "已入 8 月账"})
        chk(r.status_code == 200, f"财务安排报销: {r.text[:100]}")
        row = [x for x in (await c.get("/api/aftersales", headers=Hf)).json()["rows"] if x["id"] == aid][0]
        chk(row["pay_status"] == "reimbursed", f"pay_status=reimbursed: {row['pay_status']}")
        chk(row["pay_by_name"] == "财务小李", f"记了是谁办的: {row['pay_by_name']}")
        # 售后自己不能点报销
        chk((await c.post(f"/api/aftersales/{aid}/reimburse", headers=Hw, data={})).status_code in (400, 403),
            "售后不能自己点安排报销")

        # ===== 7) 售后部不能再走 OA 报销 =====
        depts = (await c.get("/api/oa/departments", headers=H)).json()
        as_dept = [d for d in depts if "售后" in d["name"]][0]
        sales_dept = [d for d in depts if d["name"] == "销售部"][0]
        docs = [d for d in (await c.get("/api/oa/doc-types", headers=H)).json() if d["enabled"]]
        reim = [d for d in docs if d["category"] == "reimbursement"][0]

        r = await c.post("/api/oa/requests", headers=Hw, json={
            "category": "reimbursement", "doc_type": reim["key"],
            "department_id": as_dept["id"], "title": "售后维修费", "amount": 500})
        chk(r.status_code == 400 and "售后" in r.text and "登记" in r.text,
            f"售后部走 OA 报销被挡且指路: {r.status_code} {r.text[:140]}")

        # ===== 5/6) 成本归集 =====
        # 给部门配成本科目
        for d, cc in ((sales_dept, "销售成本"), (as_dept, "售后成本")):
            r = await c.put(f"/api/oa/departments/{d['id']}", headers=H, json={
                "name": d["name"], "lead_role": d.get("lead_role"),
                "cost_center": cc, "sort_order": d["sort_order"], "enabled": True})
            chk(r.status_code == 200 and r.json().get("cost_center") == cc,
                f"{d['name']} → {cc}: {r.status_code}")
        r = await c.put(f"/api/oa/departments/{sales_dept['id']}", headers=H, json={
            "name": sales_dept["name"], "cost_center": "不存在的科目", "sort_order": 0, "enabled": True})
        chk(r.status_code == 400, f"乱填成本科目被拒: {r.status_code}")
        await c.put(f"/api/oa/departments/{sales_dept['id']}", headers=H, json={
            "name": sales_dept["name"], "lead_role": sales_dept.get("lead_role"),
            "cost_center": "销售成本", "sort_order": sales_dept["sort_order"], "enabled": True})

        # 销售部提一张报销单并批掉
        await c.post("/api/oa/chains", headers=H, json={
            "department_id": sales_dept["id"], "doc_type": reim["key"], "step_order": 1,
            "approver_role": "manager", "enabled": True})
        r = await c.post("/api/oa/requests", headers=Hs, json={
            "category": "reimbursement", "doc_type": reim["key"],
            "department_id": sales_dept["id"], "title": "客户招待", "amount": 1000})
        chk(r.status_code == 200, f"销售部提报销单: {r.text[:120]}")
        oid = r.json()["id"]
        # 财务核定成 800（成本要按核定的算，不是申请的 1000）
        r = await c.put(f"/api/oa/requests/{oid}/approve", headers=H,
                        json={"settle_amount": 800})
        chk(r.status_code == 200, f"审批并核定 800: {r.text[:120]}")

        cost = (await c.get("/api/oa/reports/cost", headers=Hf)).json()
        by = cost["by_center"]
        chk(abs(by.get("销售成本", 0) - 800) < 0.01,
            f"销售成本按核定金额 800（不是申请的 1000）: {by.get('销售成本')}")
        chk(abs(by.get("售后成本", 0) - 1349.5) < 0.01,
            f"售后成本 = 售后登记的 1349.5，只此一个来源: {by.get('售后成本')}")
        srcs = {(r_["cost_center"], r_["source"]) for r_ in cost["rows"]}
        chk(("售后成本", "aftersales") in srcs, "售后成本来自售后登记")
        chk(("售后成本", "oa_reimbursement") not in srcs,
            "售后成本里没有 OA 报销那一份（否则就是重复计算）")

        # 采购申请不计成本
        purch = [d for d in docs if d["category"] == "purchase"]
        if purch:
            await c.post("/api/oa/chains", headers=H, json={
                "department_id": sales_dept["id"], "doc_type": purch[0]["key"], "step_order": 1,
                "approver_role": "manager", "enabled": True})
            r = await c.post("/api/oa/requests", headers=Hs, json={
                "category": "purchase", "doc_type": purch[0]["key"],
                "department_id": sales_dept["id"], "title": "买台电脑", "amount": 5000})
            if r.status_code == 200:
                await c.put(f"/api/oa/requests/{r.json()['id']}/approve", headers=H, json={})
                by2 = (await c.get("/api/oa/reports/cost", headers=Hf)).json()["by_center"]
                chk(abs(by2.get("销售成本", 0) - 800) < 0.01,
                    f"采购申请不计入成本（仍是 800，不是 5800）: {by2.get('销售成本')}")

        chk(any("售后成本只统计售后" in n for n in cost["notes"]), "报表自带口径说明")

        # 非财务不能看成本报表
        chk((await c.get("/api/oa/reports/cost", headers=Hw)).status_code == 403,
            "售后看不到成本报表")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
