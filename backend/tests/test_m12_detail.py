"""M12 项目详单增强：第5表电工采购单(建表/解析/导入白名单保护)+工作流聚合+装配前置+done-flag。"""
import asyncio, os, sys, tempfile, shutil, io

tmp = tempfile.mkdtemp(prefix="m12test")
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

def make_xlsx(headers, rows):
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(headers)
    for r in rows: ws.append(r)
    bio = io.BytesIO(); wb.save(bio); bio.seek(0)
    return bio.read()

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
                          ("pm","pm_lead","钱主管"),("a1","assembler","赵师傅"),("bu","buyer","林采购")]:
            ids[u] = await mk(u, rc, fn)
        async def login(u):
            r = await c.post("/api/auth/login", json={"username":u,"password":"pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        # ---- 新建项目自动预置 5 张表（含电工采购单） ----
        r = await c.post("/api/projects", headers=H, json={"code":"T-120","name":"测试机"})
        pid = r.json()["id"]
        sheets = await c.get(f"/api/projects/{pid}/datasheets", headers=H)
        names = [d["name"] for d in sheets.json()]
        chk(len(names)==5 and "电工采购单" in names, f"新项目预置5表含电工采购单: {names}")
        chk(names[-1]=="电工采购单", f"电工采购单在最后: {names}")
        elec_did = [d["id"] for d in sheets.json() if d["name"]=="电工采购单"][0]
        # 第5表字段=10固定列
        fres = await c.get(f"/api/datasheets/{elec_did}/fields", headers=H)
        fnames = [f["name"] for f in fres.json()]
        chk(fnames==['项目','规格型号','数量','品牌','采购负责人','订购日期','到货日期','进度','仓库签字','备注'],
            f"第5表固定列名: {fnames}")

        # ---- D1 四表校验不含第5表：销售下单建项目 → 设计接单完成只校验四表 ----
        Hs1, Hdl, Hd1, Hel, He1 = (await login("s1"), await login("dl"), await login("d1"),
                                   await login("el"), await login("e1"))
        r = await c.post("/api/sales/orders", headers=Hs1, json={
            "name":"乳化机","customer":"客户","cust_type":"经销商","contract":"有","amount":100000,
            "tax_rate":"13%","prepay":0,"before_ship":0,"ship_receivable":0,"balance":0,
            "balance_date":"","depts":["design","electric"],"receiver":{"name":"x","phone":"1","addr":"y"}})
        pid2, code2 = r.json()["project_id"], r.json()["code"]
        # 第5表也在新项目里
        sheets2 = (await c.get(f"/api/projects/{pid2}/datasheets", headers=H)).json()
        chk(sum(1 for d in sheets2 if d["name"]=="电工采购单")==1, "销售下单项目也有第5表")

        # 设计完成：模拟四表(不含第5表)导入后应可完成
        from sqlalchemy import update as _upd
        from app import models as M
        from datetime import datetime, timezone
        async with SessionLocal() as db:
            await db.execute(_upd(M.Datasheet).where(
                M.Datasheet.project_id==pid2,
                M.Datasheet.name.in_(['钣金装配','标准件清单','外协加工','不锈钢原料下料单']))
                .values(imported_at=datetime.now(timezone.utc)))
            await db.commit()
        od = [o for o in (await c.get("/api/orders?dept=design", headers=Hdl)).json() if o["project_id"]==pid2][0]["id"]
        await c.post(f"/api/orders/{od}/assign", headers=Hdl, json={"worker_id":ids["d1"]})
        await c.post(f"/api/orders/{od}/start", headers=Hd1, json={"start_date":"2026-06-01","due_date":"2026-12-31"})
        # 通知池 logistics 需要一个用户
        ids["lo"] = await mk("lo","logistics","马师傅")
        r = await c.post(f"/api/orders/{od}/complete", headers=Hd1, json={"notify_user_id":ids["lo"]})
        chk(r.status_code==200, f"设计完成不被第5表卡(D1只校验四表): {r.text[:150]}")

        # ---- 电工上传采购清单 → 自动解析写入第5表 ----
        oe = [o for o in (await c.get("/api/orders?dept=electric", headers=Hel)).json() if o["project_id"]==pid2][0]["id"]
        await c.post(f"/api/orders/{oe}/assign", headers=Hel, json={"worker_id":ids["e1"]})
        await c.post(f"/api/orders/{oe}/start", headers=He1, json={"start_date":"2026-06-01","due_date":"2026-12-31"})
        xlsx = make_xlsx(["规格型号","数量","品牌","备注"],
                         [["变频器 G120 7.5kW", 1, "西门子", "主驱动"],
                          ["接触器 3RT", 3, "施耐德", ""],
                          [None, None, None, None]])
        r = await c.post(f"/api/orders/{oe}/start-upload?kind=plist", headers=He1,
                         files=[("files", ("采购清单.xlsx", io.BytesIO(xlsx), "application/vnd.ms-excel"))])
        chk(r.status_code==200, f"电工上传清单: {r.text[:150]}")
        # 第5表应有 2 行解析数据
        elec_did2 = [d["id"] for d in sheets2 if d["name"]=="电工采购单"][0]
        recs = (await c.get(f"/api/datasheets/{elec_did2}/records", headers=H)).json()
        chk(len(recs)==2, f"采购清单解析写入第5表2行: {len(recs)}")
        # 校验项目列=编号、规格型号映射
        fres2 = {f["name"]: f["id"] for f in (await c.get(f"/api/datasheets/{elec_did2}/fields", headers=H)).json()}
        vals0 = recs[0]["values"]
        chk(vals0.get(str(fres2["项目"]))==code2, "第5表项目列=编号")
        chk(vals0.get(str(fres2["规格型号"]))=="变频器 G120 7.5kW", "规格型号映射")
        chk(vals0.get(str(fres2["品牌"]))=="西门子", "品牌映射")
        chk(str(fres2["采购负责人"]) not in vals0, "采购负责人留空待采购填")

        # 二次上传不覆盖（已有数据）
        await c.post(f"/api/orders/{oe}/start-upload?kind=plist", headers=He1,
                     files=[("files", ("采购清单2.xlsx", io.BytesIO(xlsx), "application/vnd.ms-excel"))])
        recs = (await c.get(f"/api/datasheets/{elec_did2}/records", headers=H)).json()
        chk(len(recs)==2, "二次上传不覆盖已有数据")

        # ---- 导入Excel全量替换保护第5表 ----
        four = make_xlsx(["名称","图纸名称"], [["A","B01"]])
        # import-excel 需要每个 sheet 一个工作表；用一个含"钣金装配"sheet名的工作簿
        from openpyxl import Workbook
        wb = Workbook(); wb.active.title="钣金装配"; wb.active.append(["名称","图纸名称"]); wb.active.append(["件1","B01"])
        bio = io.BytesIO(); wb.save(bio); bio.seek(0)
        r = await c.post(f"/api/projects/{pid2}/import-excel", headers=H,
                         files={"file": ("项目.xlsx", bio, "application/vnd.ms-excel")})
        chk(r.status_code==200, f"导入Excel: {r.text[:150]}")
        sheets3 = (await c.get(f"/api/projects/{pid2}/datasheets", headers=H)).json()
        elec3 = [d for d in sheets3 if d["name"]=="电工采购单"]
        chk(len(elec3)==1, "导入后第5表仍在(白名单保护)")
        recs = (await c.get(f"/api/datasheets/{elec3[0]['id']}/records", headers=H)).json()
        chk(len(recs)==2, "导入后第5表数据保留")
        chk(sheets3[-1]["name"]=="电工采购单", f"导入后第5表仍在末尾: {[d['name'] for d in sheets3]}")

        # ---- 工作流聚合 ----
        r = await c.get(f"/api/projects/{pid2}/workflow", headers=H)
        wf = r.json()
        chk(wf["code"]==code2 and wf["sales_name"]=="赵仁辉", f"工作流销售: {wf.get('sales_name')}")
        design_node = [d for d in wf["depts"] if d["dept"]=="design"][0]
        chk(design_node["status"]=="done" and design_node["worker_name"]=="张工", "工作流设计节点done")
        prod_node = [d for d in wf["depts"] if d["dept"]=="produce"][0]
        chk(prod_node["status"]=="none", "工作流生产未下单")
        chk(wf["purchase_list_count"]>=1, f"采购清单计数: {wf['purchase_list_count']}")
        # 销售本人可查全览
        r = await c.get(f"/api/projects/{pid2}/workflow", headers=Hs1)
        chk(r.status_code==200, "销售本人可查工作流全览")

        # ---- 装配前置三表 done-flag ----
        # 先给 pid2 派生产任务让装配工有项目
        r = await c.post("/api/orders", headers=H, json={"project_id":pid2,"dept":"produce","worker_id":ids["a1"]})
        Ha1, Hpm = await login("a1"), await login("pm")
        r = await c.get("/api/assembly/sheet-status", headers=Ha1)
        chk(len(r.json())==1 and r.json()[0]["code"]==code2, f"装配工看到前置表状态: {r.json()}")
        chk(all(v is False for v in r.json()[0]["sheets"].values()), "初始三表均未完成")
        # designer 标记钣金装配完成
        bj_did = [d["id"] for d in sheets3 if d["name"]=="钣金装配"][0]
        r = await c.put(f"/api/datasheets/{bj_did}/done-flag", headers=Hd1, json={"done": True})
        chk(r.status_code==200, f"设计师标记钣金装配完成: {r.text[:120]}")
        r = await c.get("/api/assembly/sheet-status", headers=Hpm)
        chk(r.json()[0]["sheets"]["钣金装配"] is True, "生产主管看到钣金装配已完成")
        # 第5表不可标记
        r = await c.put(f"/api/datasheets/{elec3[0]['id']}/done-flag", headers=Hd1, json={"done": True})
        chk(r.status_code==400, "第5表不支持完成标记")
        # 电工无权标记
        r = await c.put(f"/api/datasheets/{bj_did}/done-flag", headers=He1, json={"done": True})
        chk(r.status_code==403, "电工无权标记")

        # ---- 存量补第5表迁移 ----
        # 手动删掉一个项目的第5表模拟存量4表项目
        async with SessionLocal() as db:
            from sqlalchemy import delete as _del
            r2 = await db.execute(M.Datasheet.__table__.select().where(
                (M.Datasheet.project_id==pid) & (M.Datasheet.name=="电工采购单")))
            row = r2.first()
            await db.execute(_del(M.Datasheet).where(M.Datasheet.id==row.id))
            await db.commit()
        from app.data_migration import backfill_elec_po_sheet
        async with SessionLocal() as db:
            j = await backfill_elec_po_sheet(db)
        chk(j["created"]>=1, f"存量补第5表: {j}")
        async with SessionLocal() as db:
            j2 = await backfill_elec_po_sheet(db)
        chk(j2["created"]==0, "补第5表幂等")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
