"""🆕 #303 上传与推送分离 + #294 电路图前置 测试：
1. 设计图纸(sheetpkg)上传后附件 pushed=0，下游接口(produce 组图纸列/钣金图纸包/采购侧文件)均不可见、无推送消息；
2. POST /api/orders/{oid}/start-push → pushed=1、下游可见、消息发给 to_role（sheetpkg 双推 buyer+sheetmetal 都收到）；
3. 推送权限与 start-upload 同口径（非任务负责人 403）；无待推送文件 400；push-state 计数正确；
4. 推送后再上传 → 新文件仍是 pushed=0，需再次推送；
5. #294：电工电路图(circuit)进行中即可上传 → pushed=0、物流无消息 → start-push 后 pushed=1、物流收到消息；
6. 兼容：电工采购清单(plist)维持上传即推送(pushed=1、收件箱立即可见)；
7. 存量兼容：旧表(无 pushed 列)经 ensure_schema_columns 补列后旧行 pushed=1（视为已推送，行为不变）。
"""
import asyncio, os, sys, tempfile, shutil, io

tmp = tempfile.mkdtemp(prefix="pushgate")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
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
        r = await c.post("/api/auth/login", json={"username":"admin","password":"admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mk(uname, rc, full):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "full_name": full, "role_id": rid[rc]})
            return r.json()["id"]
        ids = {}
        for u, rc, fn in [("s1","sales","赵仁辉"),("dl","design_lead","陈工"),("d1","designer","张工"),("d2","designer","王工"),
                          ("el","electric_lead","许工"),("e1","electrician","宋朴"),("pl","pm_lead","生产主管"),
                          ("sm","sheetmetal","何师傅"),("bu","buyer","林采购"),("lo","logistics","马师傅")]:
            ids[u] = await mk(u, rc, fn)
        async def login(u):
            r = await c.post("/api/auth/login", json={"username":u,"password":"pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}
        Hs1, Hdl, Hd1, Hd2, Hel, He1, Hpl, Hsm, Hbu, Hlo = (
            await login("s1"), await login("dl"), await login("d1"), await login("d2"), await login("el"),
            await login("e1"), await login("pl"), await login("sm"), await login("bu"), await login("lo"))

        # 销售下单（设计+电工+生产）
        r = await c.post("/api/sales/orders", headers=Hs1, json={
            "name":"乳化机","customer":"x","cust_type":"经销商","contract":"有","amount":100000,
            "tax_rate":"13%","prepay":0,"before_ship":0,"ship_receivable":0,"balance":0,
            "balance_date":"","depts":["design","electric","produce"],"receiver":{"name":"a","phone":"1","addr":"b"}})
        chk(r.status_code == 200, f"销售下单: {r.status_code} {r.text[:120]}")
        pid, code = r.json()["project_id"], r.json()["code"]

        od = [o for o in (await c.get("/api/orders?dept=design", headers=Hdl)).json() if o["project_id"]==pid][0]["id"]
        oe = [o for o in (await c.get("/api/orders?dept=electric", headers=Hel)).json() if o["project_id"]==pid][0]["id"]
        op = [o for o in (await c.get("/api/orders?dept=produce", headers=Hpl)).json() if o["project_id"]==pid][0]["id"]
        await c.post(f"/api/orders/{od}/assign", headers=Hdl, json={"worker_id":ids["d1"]})
        await c.post(f"/api/orders/{od}/start", headers=Hd1, json={"start_date":"2026-07-01","due_date":"2026-12-31"})
        await c.post(f"/api/orders/{oe}/assign", headers=Hel, json={"worker_id":ids["e1"]})
        await c.post(f"/api/orders/{oe}/start", headers=He1, json={"start_date":"2026-07-01","due_date":"2026-12-31"})
        # 生产派发到钣金组（produce 组图纸列的前提）
        r = await c.post(f"/api/produce/dispatch/{op}", headers=Hpl, json={"sheetmetal_worker_id": ids["sm"]})
        chk(r.status_code == 200, f"生产派发钣金组: {r.status_code} {r.text[:120]}")

        async def pushed_flags(biz_type, kind):
            async with SessionLocal() as db:
                res = await db.execute(select(models.Attachment.pushed).where(
                    models.Attachment.biz_type == biz_type, models.Attachment.kind == kind))
                return [x[0] for x in res.all()]

        # ===== 1. sheetpkg 上传 → pushed=0、下游不可见、无消息 =====
        up = await c.post(f"/api/orders/{od}/start-upload?kind=sheetpkg", headers=Hd1,
                          files=[("files", ("总装图.pdf", io.BytesIO(b"P1"), "application/pdf")),
                                 ("files", ("钣金件图.pdf", io.BytesIO(b"P2"), "application/pdf"))])
        chk(up.status_code == 200 and len(up.json()) == 2, f"sheetpkg 上传: {up.status_code} {up.text[:120]}")
        flags = await pushed_flags("order_start_output", "sheetpkg")
        chk(len(flags) == 2 and all(f is False for f in flags), f"上传后 pushed=0: {flags}")

        row = [x for x in (await c.get("/api/sheetmetal/projects", headers=Hsm)).json() if x["project_id"]==pid][0]
        chk(len(row["pkg_files"]) == 0, f"未推送时钣金图纸包不可见: {len(row['pkg_files'])}")
        grow = [x for x in (await c.get("/api/produce/sheetmetal-projects", headers=Hsm)).json() if x["project_id"]==pid][0]
        chk(len(grow["laser_files"]) == 0, f"未推送时钣金组CAD激光图纸列不可见: {len(grow['laser_files'])}")
        prow = [x for x in (await c.get("/api/purchase/projects", headers=Hbu)).json() if x["project_id"]==pid][0]
        chk(len(prow["cad_laser_files"]) == 0, f"未推送时采购CAD激光图纸不可见: {len(prow['cad_laser_files'])}")
        msgs = (await c.get("/api/messages", headers=Hbu)).json()
        chk(not any("CAD激光图纸" in m["text"] for m in msgs), "未推送时采购无图纸消息")
        msgs = (await c.get("/api/messages", headers=Hsm)).json()
        chk(not any("CAD激光图纸" in m["text"] for m in msgs), "未推送时钣金组无图纸消息")

        # push-state 计数
        ps = (await c.get("/api/orders/push-state", headers=Hd1, params={"dept":"design"})).json()
        chk(ps.get(str(od), {}).get("sheetpkg") == 2, f"push-state 待推送2: {ps}")

        # ===== 2. 权限/守卫 =====
        r = await c.post(f"/api/orders/{od}/start-push", headers=Hd2, json={"kind":"sheetpkg"})
        chk(r.status_code == 403, f"非任务负责人推送 403: {r.status_code}")
        r = await c.post(f"/api/orders/{od}/start-push", headers=Hd1, json={"kind":"bogus"})
        chk(r.status_code == 400, f"未知 kind 400: {r.status_code}")
        r = await c.post(f"/api/orders/{od}/start-push", headers=Hd1, json={"kind":"outsource_img"})
        chk(r.status_code == 400, f"无待推送文件 400: {r.status_code} {r.text[:80]}")

        # ===== 3. start-push → pushed=1、下游可见、双推 buyer+sheetmetal =====
        r = await c.post(f"/api/orders/{od}/start-push", headers=Hd1, json={"kind":"sheetpkg"})
        chk(r.status_code == 200 and "2" in r.json().get("message",""), f"推送成功: {r.status_code} {r.text[:120]}")
        flags = await pushed_flags("order_start_output", "sheetpkg")
        chk(all(f is True for f in flags), f"推送后 pushed=1: {flags}")
        row = [x for x in (await c.get("/api/sheetmetal/projects", headers=Hsm)).json() if x["project_id"]==pid][0]
        chk(len(row["pkg_files"]) == 2, f"推送后钣金图纸包可见2: {len(row['pkg_files'])}")
        grow = [x for x in (await c.get("/api/produce/sheetmetal-projects", headers=Hsm)).json() if x["project_id"]==pid][0]
        chk(len(grow["laser_files"]) == 2, f"推送后钣金组图纸列可见2: {len(grow['laser_files'])}")
        prow = [x for x in (await c.get("/api/purchase/projects", headers=Hbu)).json() if x["project_id"]==pid][0]
        chk(len(prow["cad_laser_files"]) == 2, f"推送后采购CAD激光图纸可见2: {len(prow['cad_laser_files'])}")
        msgs = (await c.get("/api/messages", headers=Hbu)).json()
        chk(any("CAD激光图纸" in m["text"] for m in msgs), "推送后采购(buyer)收到图纸消息")
        msgs = (await c.get("/api/messages", headers=Hsm)).json()
        chk(any("CAD激光图纸" in m["text"] for m in msgs), "推送后钣金组(sheetmetal)收到图纸消息(双推)")
        ps = (await c.get("/api/orders/push-state", headers=Hd1, params={"dept":"design"})).json()
        chk(ps.get(str(od), {}).get("sheetpkg") is None, f"推送后 push-state 清零: {ps}")

        # ===== 4. 推送后再上传 → 新文件仍待推送 =====
        up = await c.post(f"/api/orders/{od}/start-upload?kind=sheetpkg", headers=Hd1,
                          files=[("files", ("补充图.pdf", io.BytesIO(b"P3"), "application/pdf"))])
        chk(up.status_code == 200, "补充上传")
        row = [x for x in (await c.get("/api/sheetmetal/projects", headers=Hsm)).json() if x["project_id"]==pid][0]
        chk(len(row["pkg_files"]) == 2, f"补充未推送时下游仍2: {len(row['pkg_files'])}")
        r = await c.post(f"/api/orders/{od}/start-push", headers=Hd1, json={"kind":"sheetpkg"})
        chk(r.status_code == 200, "二次推送")
        row = [x for x in (await c.get("/api/sheetmetal/projects", headers=Hsm)).json() if x["project_id"]==pid][0]
        chk(len(row["pkg_files"]) == 3, f"二次推送后下游3: {len(row['pkg_files'])}")

        # ===== 5. #294 电工电路图：进行中可上传 → 待推送 → 推送下发物流 =====
        up = await c.post(f"/api/orders/{oe}/output-upload?kind=circuit", headers=He1,
                          files=[("files", ("电路图.pdf", io.BytesIO(b"%PDF-c"), "application/pdf"))])
        chk(up.status_code == 200, f"进行中上传电路图: {up.status_code} {up.text[:120]}")
        flags = await pushed_flags("order_output", "circuit")
        chk(len(flags) == 1 and flags[0] is False, f"电路图上传后 pushed=0: {flags}")
        msgs = (await c.get("/api/messages", headers=Hlo)).json()
        chk(not any("电路图" in m["text"] for m in msgs), "电路图未推送时物流无消息")
        ps = (await c.get("/api/orders/push-state", headers=He1, params={"dept":"electric"})).json()
        chk(ps.get(str(oe), {}).get("circuit") == 1, f"push-state 电路图待推送1: {ps}")
        r = await c.post(f"/api/orders/{oe}/start-push", headers=He1, json={"kind":"circuit"})
        chk(r.status_code == 200, f"推送电路图: {r.status_code} {r.text[:120]}")
        flags = await pushed_flags("order_output", "circuit")
        chk(flags[0] is True, f"电路图推送后 pushed=1: {flags}")
        msgs = (await c.get("/api/messages", headers=Hlo)).json()
        chk(any("电路图" in m["text"] for m in msgs), "电路图推送后物流(logistics)收到消息")

        # ===== 6. 兼容：plist 维持上传即推送 =====
        up = await c.post(f"/api/orders/{oe}/start-upload?kind=plist", headers=He1,
                          files=[("files", ("采购清单.xlsx", io.BytesIO(b"XL"), "application/vnd.ms-excel"))])
        chk(up.status_code == 200, "plist 上传")
        flags = await pushed_flags("order_start_output", "plist")
        chk(len(flags) == 1 and flags[0] is True, f"plist 上传即推送 pushed=1: {flags}")
        inbox = (await c.get("/api/purchase/inbox", headers=Hbu)).json()
        chk(any(x["project_id"] == pid for x in inbox), "plist 收件箱立即可见(无需推送)")

    # ===== 7. 存量兼容：旧表补列后 pushed=1 =====
    mig_url = f"sqlite+aiosqlite:///{tmp}/legacy.db"
    mig_engine = create_async_engine(mig_url)
    async with mig_engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE attachments (id INTEGER PRIMARY KEY, biz_type VARCHAR(32), name VARCHAR(255))"))
        await conn.execute(text(
            "INSERT INTO attachments (biz_type, name) VALUES ('order_start_output', '旧图纸.pdf')"))
    added = await ensure_schema_columns(mig_engine)
    async with mig_engine.begin() as conn:
        val = (await conn.execute(text("SELECT pushed FROM attachments WHERE name='旧图纸.pdf'"))).scalar()
    chk(added >= 1 and val == 1, f"存量附件补列后 pushed=1(视为已推送): added={added} val={val}")
    await mig_engine.dispose()

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
