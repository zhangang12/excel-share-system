"""M05 钣金组 + M06 采购部收件箱：消费电工/设计上传附件，撤回联动。"""
import asyncio, os, sys, tempfile, shutil, io

tmp = tempfile.mkdtemp(prefix="m56test")
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
        for u, rc, fn in [("s1","sales","赵仁辉"),("dl","design_lead","陈工"),("d1","designer","张工"),
                          ("el","electric_lead","许工"),("e1","electrician","刘工"),
                          ("sm","sheetmetal","何师傅"),("bu","buyer","林采购"),("lo","logistics","马师傅")]:
            ids[u] = await mk(u, rc, fn)
        async def login(u):
            r = await c.post("/api/auth/login", json={"username":u,"password":"pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        Hs1, Hdl, Hd1, Hel, He1, Hsm, Hbu = (await login("s1"), await login("dl"), await login("d1"),
                                             await login("el"), await login("e1"), await login("sm"), await login("bu"))

        # 销售下单（设计+电工）
        r = await c.post("/api/sales/orders", headers=Hs1, json={
            "name":"乳化机","customer":"x","cust_type":"经销商","contract":"有","amount":100000,
            "tax_rate":"13%","prepay":0,"before_ship":0,"ship_receivable":0,"balance":0,
            "balance_date":"","depts":["design","electric"],"receiver":{"name":"a","phone":"1","addr":"b"}})
        pid, code = r.json()["project_id"], r.json()["code"]

        # ===== M05：初始无图纸包 =====
        r = await c.get("/api/sheetmetal/projects", headers=Hsm)
        chk(r.status_code==200, f"钣金列表: {r.status_code}")
        row = [x for x in r.json() if x["code"]==code][0]
        chk(row["designer"] is None or True, "钣金行有设计师字段")
        chk(len(row["pkg_files"])==0, "初始无图纸包")
        chk(row["sheetmetal_datasheet_id"] is not None, "钣金装配表id存在(只读引用)")

        # 设计接单上传图纸包 → 钣金看到
        od = [o for o in (await c.get("/api/orders?dept=design", headers=Hdl)).json() if o["project_id"]==pid][0]["id"]
        await c.post(f"/api/orders/{od}/assign", headers=Hdl, json={"worker_id":ids["d1"]})
        await c.post(f"/api/orders/{od}/start", headers=Hd1, json={"start_date":"2026-06-01","due_date":"2026-12-31"})
        up = await c.post(f"/api/orders/{od}/start-upload?kind=sheetpkg", headers=Hd1,
                          files=[("files", ("总装图.pdf", io.BytesIO(b"P1"), "application/pdf")),
                                 ("files", ("钣金件图.pdf", io.BytesIO(b"P2"), "application/pdf"))])
        att_id = up.json()[0]["id"]
        # CAD激光图纸推送给采购部(2026-06-19 改向，不再推钣金)；钣金组仍可只读引用图纸包附件
        msgs = (await c.get("/api/messages", headers=Hbu)).json()
        chk(any("CAD激光图纸" in m["text"] for m in msgs), "采购部收CAD激光图纸推送")
        row = [x for x in (await c.get("/api/sheetmetal/projects", headers=Hsm)).json() if x["code"]==code][0]
        chk(len(row["pkg_files"])==2, f"钣金仍可见2个图纸包附件: {len(row['pkg_files'])}")
        # 钣金下载图纸包
        r = await c.get(f"/api/attachments/{att_id}/download", headers=Hsm)
        chk(r.status_code==200, "钣金下载图纸包")
        # 钣金可只读访问钣金装配表（有详单权限）
        r = await c.get(f"/api/datasheets/{row['sheetmetal_datasheet_id']}/records", headers=Hsm)
        chk(r.status_code==200, "钣金只读访问钣金装配表")
        # 非钣金角色访问被拒
        r = await c.get("/api/sheetmetal/projects", headers=Hbu)
        chk(r.status_code==403, "非钣金角色访问钣金列表被拒")

        # 撤回联动：移除图纸包 → 钣金消失
        await c.delete(f"/api/orders/{od}/attachments/{att_id}", headers=Hd1)
        row = [x for x in (await c.get("/api/sheetmetal/projects", headers=Hsm)).json() if x["code"]==code][0]
        chk(len(row["pkg_files"])==1, f"移除后图纸包减少: {len(row['pkg_files'])}")

        # ===== M06：采购收件箱 =====
        r = await c.get("/api/purchase/inbox", headers=Hbu)
        chk(r.status_code==200 and len(r.json())==0, f"初始收件箱空: {len(r.json())}")
        # 电工接单上传采购清单 → 采购收件箱出现
        oe = [o for o in (await c.get("/api/orders?dept=electric", headers=Hel)).json() if o["project_id"]==pid][0]["id"]
        await c.post(f"/api/orders/{oe}/assign", headers=Hel, json={"worker_id":ids["e1"]})
        await c.post(f"/api/orders/{oe}/start", headers=He1, json={"start_date":"2026-06-01","due_date":"2026-12-31"})
        up = await c.post(f"/api/orders/{oe}/start-upload?kind=plist", headers=He1,
                          files=[("files", ("采购清单.xlsx", io.BytesIO(b"XL"), "application/vnd.ms-excel"))])
        plist_id = up.json()[0]["id"]
        r = await c.get("/api/purchase/inbox", headers=Hbu)
        chk(len(r.json())==1, f"收件箱1条: {len(r.json())}")
        item = r.json()[0]
        chk(item["code"]==code and item["source"]=="电工部" and item["file"]["name"]=="采购清单.xlsx",
            f"收件箱内容: {item['code']} {item['source']}")
        # 🆕 改口径: 电工采购清单不再单独推送采购部(已进项目详单电工采购单);
        # 收件箱接口数据保留(前端已隐藏,可逆),故上面收件箱仍可查到附件,但无推送消息。
        msgs = (await c.get("/api/messages", headers=Hbu)).json()
        chk(not any("采购清单" in m["text"] for m in msgs), "电工采购清单不再推送采购部")
        # 非采购角色被拒
        r = await c.get("/api/purchase/inbox", headers=Hsm)
        chk(r.status_code==403, "非采购访问收件箱被拒")
        # 撤回联动：移除采购清单 → 收件箱消失
        await c.delete(f"/api/orders/{oe}/attachments/{plist_id}", headers=He1)
        r = await c.get("/api/purchase/inbox", headers=Hbu)
        chk(len(r.json())==0, "撤回后收件箱清空")

        # ===== 🆕 #7 作废电工单 → 采购收件箱清空(即使附件仍在,消除幻影数据) =====
        await c.post(f"/api/orders/{oe}/start-upload?kind=plist", headers=He1,
                     files=[("files", ("采购清单2.xlsx", io.BytesIO(b"XL"), "application/vnd.ms-excel"))])
        r = await c.get("/api/purchase/inbox", headers=Hbu)
        chk(len(r.json())==1, "重传后收件箱恢复1条")
        r = await c.post(f"/api/orders/{oe}/void", headers=Hel)
        chk(r.status_code==200, f"#7 作废电工单: {r.text[:80]}")
        r = await c.get("/api/purchase/inbox", headers=Hbu)
        chk(len(r.json())==0, f"#7 作废后采购收件箱清空: {len(r.json())}")
        # 管理层可见两者
        r = await c.get("/api/sheetmetal/projects", headers=H)
        chk(r.status_code==200, "管理层可见钣金列表")
        r = await c.get("/api/purchase/inbox", headers=H)
        chk(r.status_code==200, "管理层可见采购收件箱")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
