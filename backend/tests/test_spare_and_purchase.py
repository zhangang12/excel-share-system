"""🆕 备机下单(设计部负责人/管理层) + CAD激光图纸/外购附图→采购部 + 采购部项目列表。"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="spare")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select, func
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models

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
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        # 子角色应重新可见/可分配
        chk("buyer_outsource" in rid and "buyer_standard" in rid, "采购两子角色已在角色列表(可分配)")

        async def mk(u, rc):
            r = await c.post("/api/admin/users", headers=H, json={"username":u,"password":"pass123","full_name":u,"role_id":rid[rc]})
            assert r.status_code == 200, r.text
            return r.json()["id"]
        async def login(u, p="pass123"):
            return {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':u,'password':p})).json()['access_token']}"}

        dlid = await mk("dl", "design_lead")
        d1id = await mk("d1", "designer")
        await mk("bo", "buyer_outsource")
        await mk("bs", "buyer_standard")
        Hdl, Hd1, Hbo, Hbs = await login("dl"), await login("d1"), await login("bo"), await login("bs")

        # ===== 备机下单（设计部负责人，默认派 生产+电工，不建台账）=====
        r = await c.post("/api/orders/spare", headers=Hdl, json={
            "code": "2026-SP1", "name": "备机甲", "qty": 2, "unit": "台"})
        chk(r.status_code == 200, f"备机下单: {r.text[:120]}")
        pid1 = r.json()["project_id"]
        chk(len(r.json()["order_ids"]) == 2, f"备机派2部门(生产+电工): {r.json()['order_ids']}")
        async with SessionLocal() as db:
            depts = {o.dept for o in (await db.execute(select(models.DeptOrder).where(models.DeptOrder.project_id == pid1))).scalars().all()}
            chk(depts == {"produce", "electric"}, f"备机派往生产+电工: {depts}")
            led = (await db.execute(select(models.SalesLedger).where(models.SalesLedger.project_id == pid1))).scalar_one_or_none()
            chk(led is None, "备机不建销售台账")
        # 进项目目录(admin overview 可见) + 一览数量/备机标记
        ov = {row["id"]: row for row in (await c.get("/api/overview", headers=H)).json()["rows"]}
        chk(pid1 in ov, "备机项目进项目目录")
        chk((ov[pid1]["extra"] or {}).get("__o__数量") == "2台", "备机数量写入一览")
        chk("备机" in ((ov[pid1]["extra"] or {}).get("__o__销售") or ""), "一览销售列标记备机")
        # 纯销售角色不可备机下单
        await mk("s1", "sales")
        rr = await c.post("/api/orders/spare", headers=await login("s1"), json={"code":"X","name":"x"})
        chk(rr.status_code == 403, f"纯销售不可备机下单: {rr.status_code}")

        # ===== 设计接单上传 CAD激光图纸 + 外购附图 → 采购部 =====
        r = await c.post("/api/orders/spare", headers=Hdl, json={"code":"2026-SP2","name":"备机乙","depts":["design"]})
        pid2 = r.json()["project_id"]; od = r.json()["order_ids"][0]
        await c.post(f"/api/orders/{od}/assign", headers=Hdl, json={"worker_id": d1id})
        # 设计部 options 的接单上传项应为 CAD激光图纸 + 外购附图
        opts = (await c.get("/api/orders/options?dept=design", headers=Hdl)).json()
        labels = {s["k"]: s["label"] for s in opts["start_outputs"]}
        chk(labels.get("sheetpkg") == "CAD激光图纸" and "outsource_img" in labels, f"设计上传项: {labels}")
        # d1 上传两类图纸
        for kind in ("sheetpkg", "outsource_img"):
            rr = await c.post(f"/api/orders/{od}/start-upload?kind={kind}", headers=Hd1,
                              files=[("files", (f"{kind}.pdf", b"X", "application/pdf"))])
            chk(rr.status_code == 200, f"上传 {kind}: {rr.status_code} {rr.text[:80]}")
        # 采购部项目列表（外协采购员）含该项目的 CAD激光图纸/外购附图 + 数据表引用
        pp = {x["project_id"]: x for x in (await c.get("/api/purchase/projects", headers=Hbo)).json()}
        chk(pid2 in pp, "采购部项目列表含备机乙")
        row = pp[pid2]
        chk(len(row["cad_laser_files"]) == 1, f"CAD激光图纸1个: {row['cad_laser_files']}")
        chk(len(row["outsource_img_files"]) == 1, f"外购附图1个: {row['outsource_img_files']}")
        chk(row["outsource_sheet_id"] and row["material_sheet_id"], "外协加工/不锈钢原料下料单 引用就绪")
        chk(row["elec_po_sheet_id"] and row["standard_sheet_id"], "电工采购单/标准件清单 引用就绪")
        chk(row["designer"] == "d1", f"设计师回带: {row['designer']}")
        # 标准件采购员也能访问
        chk((await c.get("/api/purchase/projects", headers=Hbs)).status_code == 200, "标准件采购员可访问采购列表")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
