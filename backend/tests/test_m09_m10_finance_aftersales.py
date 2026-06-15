"""M10 售后部 + M09 财务部：售后登记→审批→同步财务；财务待开票/已开票/售后费用三tab。"""
import asyncio, os, sys, tempfile, shutil, io

tmp = tempfile.mkdtemp(prefix="m910test")
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
        async def mk(u, rc, fn):
            r = await c.post("/api/admin/users", headers=H, json={"username":u,"password":"pass123","full_name":fn,"role_id":rid[rc]})
            return r.json()["id"]
        ids = {}
        for u, rc, fn in [("s1","sales","赵仁辉"),("sl","sales_lead","高总"),("fin","finance","周会计"),
                          ("aw","as_worker","韩师傅"),("al","as_lead","冯主管")]:
            ids[u] = await mk(u, rc, fn)
        async def login(u):
            r = await c.post("/api/auth/login", json={"username":u,"password":"pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}
        Hs1, Hsl, Hfin, Haw, Hal = (await login("s1"), await login("sl"), await login("fin"),
                                    await login("aw"), await login("al"))

        # 建项目 + 台账（销售下单）
        r = await c.post("/api/sales/orders", headers=Hs1, json={
            "name":"乳化机","customer":"华东","cust_type":"经销商","contract":"有","amount":120000,
            "tax_rate":"13%","prepay":0,"before_ship":0,"ship_receivable":0,"balance":0,
            "balance_date":"","depts":["design"],"receiver":{"name":"a","phone":"1","addr":"b"}})
        pid, code = r.json()["project_id"], r.json()["code"]
        led_id = (await c.get("/api/sales/ledger", headers=Hsl)).json()["rows"][0]["id"]

        # ===== M09 开票流 → 财务待开票/已开票 =====
        await c.post(f"/api/sales/ledger/{led_id}/invoice-apply", headers=Hs1,
                     files={"file":("申请表.xlsx", io.BytesIO(b"X"), "application/vnd.ms-excel")})
        await c.post(f"/api/sales/ledger/{led_id}/invoice-approve", headers=Hsl)
        r = await c.get("/api/finance/pending-invoices", headers=Hfin)
        chk(len(r.json())==1 and r.json()[0]["code"]==code, f"财务待开票1条: {len(r.json())}")
        chk(r.json()[0]["apply_file_name"]=="申请表.xlsx", "待开票含申请表")
        # 财务上传发票（复用 sales invoice-upload）
        r = await c.post(f"/api/sales/ledger/{led_id}/invoice-upload", headers=Hfin,
                         files={"file":("发票.pdf", io.BytesIO(b"P"), "application/pdf")})
        chk(r.status_code==200, f"财务上传发票: {r.text[:120]}")
        r = await c.get("/api/finance/pending-invoices", headers=Hfin)
        chk(len(r.json())==0, "上传后待开票清空")
        r = await c.get("/api/finance/invoiced", headers=Hfin)
        chk(len(r.json())==1 and r.json()[0]["invoice_file_name"]=="发票.pdf", "已开票含发票")

        # ===== M10 售后登记→审批→同步财务 =====
        # 售后无项目目录菜单，但能查项目选项
        r = await c.get("/api/auth/menus", headers=Haw)
        ks = [m["key"] for m in r.json()["menus"]]
        chk("aftersales" in ks and "catalog" not in ks, f"售后菜单: {ks}")
        r = await c.get("/api/aftersales/projects", headers=Haw)
        chk(any(p["code"]==code for p in r.json()), "售后可查项目选项")

        # 登记缺物料清单（必传）
        r = await c.post("/api/aftersales", headers=Haw,
                         data={"project_id":str(pid),"problem":"机封渗漏","cost":"1200"})
        chk(r.status_code==422 or r.status_code==400, f"缺物料清单被拒: {r.status_code}")
        # 完整登记
        r = await c.post("/api/aftersales", headers=Haw,
                         data={"project_id":str(pid),"problem":"出厂后机封渗漏，上门更换","cost":"1200"},
                         files={"file":("售后物料清单.xlsx", io.BytesIO(b"X"), "application/vnd.ms-excel")})
        chk(r.status_code==200, f"售后登记: {r.text[:150]}")
        msgs = (await c.get("/api/messages", headers=Hal)).json()
        chk(any("售后待审批" in m["text"] for m in msgs), "售后主管收推送")
        r = await c.get("/api/aftersales", headers=Haw)
        chk(r.json()["stats"]["pending"]==1 and r.json()["stats"]["total_cost"]==1200, f"售后统计: {r.json()['stats']}")
        aid = r.json()["rows"][0]["id"]
        chk(r.json()["rows"][0]["mat_file_name"]=="售后物料清单.xlsx", "登记带物料清单")

        # 员工不能审批
        r = await c.post(f"/api/aftersales/{aid}/approve", headers=Haw)
        chk(r.status_code==403, "售后员工不能审批")
        # 财务此时看不到（未审批）
        r = await c.get("/api/finance/aftersales", headers=Hfin)
        chk(len(r.json()["rows"])==0, "未审批财务看不到售后")
        # 主管审批通过 → 同步财务
        r = await c.post(f"/api/aftersales/{aid}/approve", headers=Hal)
        chk(r.status_code==200, "售后主管审批通过")
        msgs = (await c.get("/api/messages", headers=Hfin)).json()
        chk(any("售后费用同步" in m["text"] and "物料清单" in m["text"] for m in msgs), "财务收售后同步推送")
        r = await c.get("/api/finance/aftersales", headers=Hfin)
        chk(len(r.json()["rows"])==1 and r.json()["rows"][0]["code"]==code, "财务售后tab出现已审批")
        chk(r.json()["rows"][0]["mat_file_name"]=="售后物料清单.xlsx", "财务可见物料清单")
        chk(r.json()["stats"]["approved_cost"]==1200, "财务售后费用合计")

        # 第二条登记后驳回 → 不同步财务
        r = await c.post("/api/aftersales", headers=Haw,
                         data={"project_id":str(pid),"problem":"测试驳回","cost":"500"},
                         files={"file":("x.pdf", io.BytesIO(b"P"), "application/pdf")})
        aid2 = (await c.get("/api/aftersales", headers=Haw)).json()["rows"][0]["id"]
        await c.post(f"/api/aftersales/{aid2}/reject", headers=Hal, data={"reason":"费用与实际不符"})
        r = await c.get("/api/finance/aftersales", headers=Hfin)
        chk(len(r.json()["rows"])==1, "驳回的售后不进财务")
        # 🆕 #97/#98 驳回通知登记人(含原因)
        msgs = (await c.get("/api/messages", headers=Haw)).json()
        chk(any("售后驳回" in m["text"] and "费用与实际不符" in m["text"] for m in msgs),
            "#97/#98 登记人收驳回通知含原因")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
