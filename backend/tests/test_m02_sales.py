"""M02/M03 销售台账+下单+开票流集成测试（临时 SQLite）。"""
import asyncio, os, sys, tempfile, shutil, io

tmp = tempfile.mkdtemp(prefix="m02test")
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
        r = await c.post("/api/auth/login", json={"username":"admin","password":"admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mk(uname, rc, full):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "full_name": full, "role_id": rid[rc]})
            return r.json()["id"]

        ids = {}
        for u, rc, fn in [("s1","sales","赵仁辉"),("s2","sales","代文飞"),("sl","sales_lead","高总"),
                          ("fin1","finance","周会计"),("dl","design_lead","陈工"),
                          ("el","electric_lead","许工"),("pm","pm_lead","钱主管"),("lo","logistics","马师傅")]:
            ids[u] = await mk(u, rc, fn)

        async def login(u):
            r = await c.post("/api/auth/login", json={"username":u,"password":"pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        Hs1, Hs2, Hsl, Hfin = await login("s1"), await login("s2"), await login("sl"), await login("fin1")

        # ---- next-code 起始 ----
        from datetime import datetime
        yr = datetime.now().strftime("%Y")
        r = await c.get("/api/sales/next-code", headers=Hs1)
        chk(r.json()["code"] == f"{yr}-001", f"首个编号: {r.json()}")

        # ---- 销售下单：s1 下两单（一单带后缀），s2 下一单 ----
        async def place(h, name, depts, suffix="", **kw):
            r = await c.post("/api/sales/orders", headers=h, json={
                "code_suffix": suffix, "name": name, "customer": kw.get("customer","客户A"),
                "cust_type": kw.get("cust_type","经销商"), "contract": "有",
                "amount": kw.get("amount", 93000), "tax_rate": "13%",
                "prepay": kw.get("prepay", 1000), "before_ship": 2000,
                "ship_receivable": 0, "balance": 500, "balance_date": "2026/7/1",
                "depts": depts, "receiver": {"name":"王主管","phone":"139","addr":"无锡"}})
            return r

        r = await place(Hs1, "5L双行星压料一体机", ["design","electric"], suffix="A")
        chk(r.status_code==200, f"s1 下单A: {r.text[:200]}")
        chk(r.json()["code"]==f"{yr}-001A" and len(r.json()["order_ids"])==2, f"编号+两任务: {r.json()}")
        pidA = r.json()["project_id"]
        r = await place(Hs1, "100L真空均质乳化机", ["design","electric","produce"], amount=128000)
        chk(r.json()["code"]==f"{yr}-002", f"递增编号: {r.json()['code']}")
        r = await place(Hs2, "2L双行星分散混合机", ["design"], amount=76000)
        codeB = r.json()["code"]; lidB_pid = r.json()["project_id"]

        # 编号查重：再下 suffix=A 的 001 → 已存在? base 会递增到 004，不冲突；直接测项目接口唯一约束已有
        # 部门负责人收到推送
        Hdl = await login("dl")
        msgs = (await c.get("/api/messages", headers=Hdl)).json()
        chk(sum("销售下单" in m["text"] for m in msgs)==3, f"设计负责人收3条下单推送: {len(msgs)}")
        Hlo = await login("lo")
        msgs = (await c.get("/api/messages", headers=Hlo)).json()
        chk(sum("发货待办" in m["text"] for m in msgs)==3, "物流收3条发货待办")

        # ---- 台账行级隔离 + 合计 ----
        r = await c.get("/api/sales/ledger", headers=Hs1)
        j = r.json()
        chk(len(j["rows"])==2 and j["totals"] is None, f"销售员仅本人2行无合计: {len(j['rows'])}")
        r = await c.get("/api/sales/ledger", headers=Hsl)
        j = r.json()
        chk(len(j["rows"])==3 and j["totals"]["count"]==3, "主管全量3行+合计")
        chk(j["totals"]["amount"]==93000+128000+76000, f"金额合计: {j['totals']['amount']}")
        chk(j["totals"]["uninvoiced"]==j["totals"]["amount"], "未开票合计=全部")
        # 筛选
        r = await c.get(f"/api/sales/ledger?sales_uid={ids['s2']}", headers=Hsl)
        chk(len(r.json()["rows"])==1, "按销售员筛选")
        # 无权角色
        r = await c.get("/api/sales/ledger", headers=Hdl)
        chk(r.status_code==403, "设计负责人无台账权限")

        lidA = [x for x in j["rows"] if x["project_id"]==pidA][0]["id"]

        # ---- 上传合同：回写签订/交货日期到一览 ----
        r = await c.post(f"/api/sales/ledger/{lidA}/contract", headers=Hs1,
                         data={"sign_date":"2026/6/1","deliver_date":"2026-08-15"},
                         files={"file": ("合同.pdf", io.BytesIO(b"PDF"), "application/pdf")})
        chk(r.status_code==200, f"上传合同: {r.text[:150]}")
        r = await c.get(f"/api/projects/{pidA}", headers=H)
        om, hm = r.json()["overview_meta"], r.json()["header_meta"]
        chk(om.get("签订日期")=="2026-06-01" and om.get("交货日期")=="2026-08-15", f"日期回写+规范化: {om.get('签订日期')}")
        chk(hm.get("下单日期")=="2026-06-01", "alias 回写 __h__下单日期")
        r = await c.get("/api/sales/ledger", headers=Hs1)
        rowA = [x for x in r.json()["rows"] if x["id"]==lidA][0]
        chk(rowA["contract"]=="有" and rowA["contract_file_name"]=="合同.pdf", "台账合同列更新")
        chk(rowA["sign_date"]=="2026-06-01", "台账下单日期列")
        # 越权：s2 给 s1 的单传合同
        r = await c.post(f"/api/sales/ledger/{lidA}/contract", headers=Hs2,
                         data={"sign_date":"2026-06-01","deliver_date":"2026-08-15"},
                         files={"file": ("x.pdf", io.BytesIO(b"P"), "application/pdf")})
        chk(r.status_code==403, "销售员不能动他人订单")

        # ---- 🆕 收款批注（预付/发货前付，支持插入时间戳） ----
        # 销售本人对自己的台账行可批注（不受开票锁限制）
        r = await c.put(f"/api/sales/ledger/{lidA}/payment-note", headers=Hs1,
                        json={"field":"prepay","note":"【2026-06-16 14:30】收到预付款1000元（微信）"})
        chk(r.status_code==200, f"销售本人写预付批注: {r.text[:120]}")
        r = await c.put(f"/api/sales/ledger/{lidA}/payment-note", headers=Hs1,
                        json={"field":"before_ship","note":"发货前付待跟进\n【2026-06-16 15:00】口头确认"})
        chk(r.status_code==200, "销售本人写发货前付批注(多行)")
        rowA = [x for x in (await c.get("/api/sales/ledger", headers=Hs1)).json()["rows"] if x["id"]==lidA][0]
        chk(rowA["prepay_note"].startswith("【2026-06-16 14:30】") and "微信" in rowA["prepay_note"], f"预付批注持久化: {rowA['prepay_note']!r}")
        chk("\n" in rowA["before_ship_note"], "发货前付批注多行保留")
        # 越权：s2 批注 s1 的行 → 403
        r = await c.put(f"/api/sales/ledger/{lidA}/payment-note", headers=Hs2,
                        json={"field":"prepay","note":"x"})
        chk(r.status_code==403, "销售员不能批注他人台账行")
        # 非法 field → 400
        r = await c.put(f"/api/sales/ledger/{lidA}/payment-note", headers=Hs1,
                        json={"field":"balance","note":"x"})
        chk(r.status_code==400, "非法 field 被拒")
        # 空串/空白 → 清空为 None
        r = await c.put(f"/api/sales/ledger/{lidA}/payment-note", headers=Hs1,
                        json={"field":"prepay","note":"   "})
        chk(r.status_code==200, "清空批注")
        rowA = [x for x in (await c.get("/api/sales/ledger", headers=Hs1)).json()["rows"] if x["id"]==lidA][0]
        chk(rowA["prepay_note"] is None, f"空白批注归 None: {rowA['prepay_note']!r}")
        # 主管可批注任意行
        r = await c.put(f"/api/sales/ledger/{lidA}/payment-note", headers=Hsl,
                        json={"field":"prepay","note":"主管复核：已到账"})
        chk(r.status_code==200, "主管可批注任意台账行")
        # 下单时即可带批注（create 路径写入 prepay_note/before_ship_note）
        r = await c.post("/api/sales/orders", headers=Hs1, json={
            "name":"带批注下单样机","customer":"客户C","cust_type":"经销商","contract":"有",
            "amount":6000,"tax_rate":"13%","prepay":2000,"prepay_note":"【下单时】定金已收",
            "before_ship":0,"before_ship_note":"","ship_receivable":0,"balance":0,
            "depts":["produce"],"receiver":{"name":"李","phone":"138","addr":"苏州"}})  # produce 避免污染后续 design 计数
        chk(r.status_code==200, f"带批注下单成功: {r.text[:120]}")
        npid = r.json()["project_id"]
        nrow = [x for x in (await c.get("/api/sales/ledger", headers=Hs1)).json()["rows"] if x["project_id"]==npid][0]
        chk(nrow["prepay_note"]=="【下单时】定金已收", f"下单批注写入台账: {nrow['prepay_note']!r}")

        # ---- 🆕 销售下单·下单资料随附：下单后销售员把资料传到每个生成的部门任务单 ----
        # 用 electric/produce 避免污染后续 design==3 计数
        r = await place(Hs1, "带资料下单样机", ["electric", "produce"], amount=8000)
        chk(r.status_code==200, "带资料下单建单")
        oids = r.json()["order_ids"]
        chk(len(oids)==2, f"两个部门任务: {oids}")
        # 销售员(created_by)对自己下的单可传下发资料（input_files）—— 前端下单后正是这么回传
        for oid in oids:
            ru = await c.post(f"/api/orders/{oid}/input-files", headers=Hs1,
                              files=[("files", ("技术交底.pdf", io.BytesIO(b"PDF"), "application/pdf")),
                                     ("files", ("BOM.xlsx", io.BytesIO(b"X"), "application/vnd.ms-excel"))])
            chk(ru.status_code==200 and len(ru.json())==2, f"销售员给 order#{oid} 传资料: {ru.text[:120]}")
        # 部门负责人能在任务单看到下发资料
        Hel = await login("el")
        elecs = (await c.get("/api/orders?dept=electric", headers=Hel)).json()
        tgt = [o for o in elecs if o["id"]==oids[0]][0]
        chk(len(tgt["input_files"])==2 and {f["name"] for f in tgt["input_files"]}=={"技术交底.pdf","BOM.xlsx"},
            f"电工任务单含2个下发资料: {[f['name'] for f in tgt['input_files']]}")
        # 越权：他人(s2)不能给 s1 的单传资料
        ru = await c.post(f"/api/orders/{oids[0]}/input-files", headers=Hs2,
                          files=[("files", ("x.pdf", io.BytesIO(b"P"), "application/pdf"))])
        chk(ru.status_code==403, "非下单方不能传下发资料")

        # ---- 主管编辑台账 ----
        r = await c.put(f"/api/sales/ledger/{lidA}", headers=Hs1, json={"amount": 95000})
        chk(r.status_code==403, "销售员不能编辑台账")
        r = await c.put(f"/api/sales/ledger/{lidA}", headers=Hsl, json={"amount": 95000, "customer":"涿州腾源阁"})
        chk(r.status_code==200, "主管编辑台账")

        # ---- 开票流：申请→审批→财务开票→回传 ----
        r = await c.post(f"/api/sales/ledger/{lidA}/invoice-apply", headers=Hs1,
                         files={"file": ("开票申请表.xlsx", io.BytesIO(b"X"), "application/vnd.ms-excel")})
        chk(r.status_code==200, f"开票申请: {r.text[:150]}")
        msgs = (await c.get("/api/messages", headers=Hsl)).json()
        chk(any("开票申请" in m["text"] for m in msgs), "主管收申请推送")
        r = await c.get("/api/sales/invoice-approvals", headers=Hsl)
        chk(len(r.json()["rows"])==1, "待审批列表1条")
        # 重复申请被拒
        r = await c.post(f"/api/sales/ledger/{lidA}/invoice-apply", headers=Hs1,
                         files={"file": ("x.xlsx", io.BytesIO(b"X"), "application/vnd.ms-excel")})
        chk(r.status_code==400, "重复申请被拒")
        # 驳回路径
        r = await c.post(f"/api/sales/ledger/{lidA}/invoice-reject", headers=Hsl)
        chk(r.status_code==200, "驳回")
        msgs = (await c.get("/api/messages", headers=Hs1)).json()
        chk(any("开票驳回" in m["text"] for m in msgs), "申请人收驳回推送")
        r = await c.get("/api/sales/ledger", headers=Hs1)
        rowA = [x for x in r.json()["rows"] if x["id"]==lidA][0]
        chk(rowA["invoice_state"] is None and not rowA["invoice_apply_file_id"], "驳回清状态+申请文件")
        # 重新申请→通过→财务
        await c.post(f"/api/sales/ledger/{lidA}/invoice-apply", headers=Hs1,
                     files={"file": ("开票申请表2.xlsx", io.BytesIO(b"X"), "application/vnd.ms-excel")})
        r = await c.post(f"/api/sales/ledger/{lidA}/invoice-approve", headers=Hsl)
        chk(r.status_code==200, "审批通过")
        msgs = (await c.get("/api/messages", headers=Hfin)).json()
        chk(any("待开票" in m["text"] for m in msgs), "财务收待开票推送")
        # 财务上传发票（销售员不能传）
        r = await c.post(f"/api/sales/ledger/{lidA}/invoice-upload", headers=Hs1,
                         files={"file": ("发票.pdf", io.BytesIO(b"P"), "application/pdf")})
        chk(r.status_code==403, "销售员不能上传发票")
        r = await c.post(f"/api/sales/ledger/{lidA}/invoice-upload", headers=Hfin,
                         files={"file": ("增值税发票.pdf", io.BytesIO(b"P"), "application/pdf")})
        chk(r.status_code==200, f"财务上传发票: {r.text[:120]}")
        msgs = (await c.get("/api/messages", headers=Hs1)).json()
        chk(any("发票已开" in m["text"] for m in msgs), "销售收发票回传推送")
        r = await c.get("/api/sales/ledger", headers=Hsl)
        j = r.json()
        rowA = [x for x in j["rows"] if x["id"]==lidA][0]
        chk(rowA["invoice_state"]=="invoiced" and rowA["invoice_file_name"]=="增值税发票.pdf", "台账已开票+发票可下载")
        chk(j["totals"]["uninvoiced"]==j["totals"]["amount"]-95000, f"未开票合计扣除已开票: {j['totals']['uninvoiced']}")

        # ---- 🆕 #105 已开票后禁改金额/税票，但其它字段(客户)仍可改 ----
        r = await c.put(f"/api/sales/ledger/{lidA}", headers=Hsl, json={"amount": 88000})
        chk(r.status_code==400 and "开票流程" in r.text, f"#105 已开票改金额被拒: {r.status_code} {r.text[:80]}")
        r = await c.put(f"/api/sales/ledger/{lidA}", headers=Hsl, json={"tax_rate": "/"})
        chk(r.status_code==400, "#105 已开票改税票被拒")
        r = await c.put(f"/api/sales/ledger/{lidA}", headers=Hsl, json={"customer": "涿州腾源阁2"})
        chk(r.status_code==200, f"#105 已开票仍可改非金额字段(客户): {r.text[:80]}")
        r = await c.put(f"/api/sales/ledger/{lidA}", headers=Hsl, json={"amount": 95000})
        chk(r.status_code==200, "#105 金额改回原值(未变更)不拦截")

        # ---- 🆕 #2 财务开票纠错出口：invoiced→作废→退回待开票→重新上传 ----
        r = await c.post(f"/api/sales/ledger/{lidA}/invoice-revoke", headers=Hfin)
        chk(r.status_code==200, f"#2 财务作废发票退回待开票: {r.text[:80]}")
        rowA = [x for x in (await c.get("/api/sales/ledger", headers=Hsl)).json()["rows"] if x["id"]==lidA][0]
        chk(rowA["invoice_state"]=="pending_invoice" and not rowA.get("invoice_file_name"), "#2 退回待开票且清原发票")
        r = await c.post(f"/api/sales/ledger/{lidA}/invoice-upload", headers=Hfin,
                         files={"file": ("重开发票.pdf", io.BytesIO(b"P"), "application/pdf")})
        chk(r.status_code==200, "#2 重新上传发票(回到invoiced)")

        # ---- 存量回填迁移：手建一个无台账项目 → run_all 后补行 ----
        r = await c.post("/api/projects", headers=H, json={"code":"2025-099","name":"存量项目"})
        from app.data_migration import backfill_sales_ledger, backfill_shipments
        async with SessionLocal() as db:
            j1 = await backfill_sales_ledger(db)
            j2 = await backfill_shipments(db)
        chk(j1["created"]==1, f"存量台账回填: {j1}")
        chk(j2["created"]==1, f"存量发货待办回填: {j2}")
        async with SessionLocal() as db:
            j1 = await backfill_sales_ledger(db)
        chk(j1["created"]==0, "回填幂等")

        # 任务出现在部门工作台（销售下单→设计待派）
        r = await c.get("/api/orders?dept=design", headers=Hdl)
        chk(len(r.json())==3 and all(x["status"]=="pending_assign" for x in r.json()), "设计部3条待派任务")

        # ---- 🆕 #3 软删带进行中开票流(pending_invoice)的项目应被拦下(409) ----
        lidB = [x for x in (await c.get("/api/sales/ledger", headers=Hsl)).json()["rows"] if x["project_id"]==lidB_pid][0]["id"]
        await c.post(f"/api/sales/ledger/{lidB}/invoice-apply", headers=Hs2,
                     files={"file": ("申请.xlsx", io.BytesIO(b"X"), "application/vnd.ms-excel")})
        await c.post(f"/api/sales/ledger/{lidB}/invoice-approve", headers=Hsl)  # → pending_invoice
        r = await c.delete(f"/api/projects/{lidB_pid}", headers=H)
        chk(r.status_code==409 and "开票流程" in r.text, f"#3 软删pending_invoice项目被拒: {r.status_code} {r.text[:80]}")
        await c.post(f"/api/sales/ledger/{lidB}/invoice-upload", headers=Hfin,
                     files={"file": ("发票.pdf", io.BytesIO(b"P"), "application/pdf")})  # → invoiced(非进行中)
        r = await c.delete(f"/api/projects/{lidB_pid}", headers=H)
        chk(r.status_code==200, f"#3 开票完成后可删除: {r.text[:80]}")

        # ---- 🆕 #1/#104 不开票(税票=/)项目不得发起开票申请 ----
        r = await place(Hs1, "不开票样机", ["design"], amount=5000)
        np_pid = r.json()["project_id"]
        nlid = [x for x in (await c.get("/api/sales/ledger", headers=Hsl)).json()["rows"] if x["project_id"]==np_pid][0]["id"]
        await c.put(f"/api/sales/ledger/{nlid}", headers=Hsl, json={"tax_rate":"/"})
        r = await c.post(f"/api/sales/ledger/{nlid}/invoice-apply", headers=Hs1,
                         files={"file": ("申请.xlsx", io.BytesIO(b"X"), "application/vnd.ms-excel")})
        chk(r.status_code==400 and "不开票" in r.text, f"#1/#104 不开票项目申请被拒: {r.status_code} {r.text[:80]}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
