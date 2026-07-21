"""🆕 2026-06-19 生产部分组派发：主管派发→钣金组/装配组两组项目列表→两组完成=生产完成。

覆盖：
- 角色改名后 code 不变；钣金组(sheetmetal)菜单并入生产部(produce)、不再有 sheet 菜单
- 主管派发：建钣金组+装配组两组任务，生产单转 in_progress
- 钣金组/装配组只看本组被派发项目；越权访问对方组 403
- 装配组「标准件清单/外协加工」备齐判定=该表「进度」列全为「完成」
- 两组都标记完成 → 父生产任务单 done（驱动发货闸门 D5/部门报表口径不变）
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="pgroup")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
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

        # 角色改名：code 不变、label 改为「生产部-钣金组/装配组」
        roles = {x["code"]: x for x in (await c.get("/api/admin/roles", headers=H)).json()}
        chk(roles["sheetmetal"]["name"] == "生产部-钣金组", f"钣金组改名: {roles['sheetmetal']['name']}")
        chk(roles["assembler"]["name"] == "生产部-装配组", f"装配组改名: {roles['assembler']['name']}")

        async def mk(u, rc):
            r = await c.post("/api/admin/users", headers=H, json={"username":u,"password":"pass123","full_name":u,"role_id":rid[rc]})
            assert r.status_code == 200, r.text
            return r.json()["id"]
        async def login(u):
            return {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':u,'password':'pass123'})).json()['access_token']}"}

        dlid = await mk("dl", "design_lead")
        await mk("pm", "pm_lead")
        smid = await mk("sm", "sheetmetal")
        asmid = await mk("asm", "assembler")
        sm2id = await mk("sm2", "sheetmetal")   # 第二个钣金组人员：验证只看派给自己的
        Hdl, Hpm, Hsm, Hasm = await login("dl"), await login("pm"), await login("sm"), await login("asm")
        Hsm2 = await login("sm2")

        # 钣金组菜单并入生产部：含 produce、不含 sheet
        ks = [m["key"] for m in (await c.get("/api/auth/menus", headers=Hsm)).json()["menus"]]
        chk("produce" in ks and "sheet" not in ks, f"钣金组菜单=生产部(无sheet): {ks}")

        # 建一个生产任务（用备机下单派生产部，省去销售台账）
        r = await c.post("/api/orders/spare", headers=Hdl, json={"code":"2026-PG1","name":"分组测试机","depts":["produce"]})
        chk(r.status_code == 200, f"建生产任务: {r.text[:120]}")
        pid = r.json()["project_id"]; oid = r.json()["order_ids"][0]

        # 派发前：钣金/装配组都看不到
        chk((await c.get("/api/produce/sheetmetal-projects", headers=Hsm)).json() == [], "派发前钣金组空")
        chk((await c.get("/api/produce/assembly-projects", headers=Hasm)).json() == [], "派发前装配组空")

        # 派发下拉：钣金组/装配组各自人员
        opts = (await c.get("/api/produce/dispatch-options", headers=Hpm)).json()
        chk(smid in [u["id"] for u in opts["sheetmetal"]] and asmid in [u["id"] for u in opts["assembly"]],
            f"派发下拉含两组人员: {opts}")

        # 非主管不可派发（带合法 body，确保是 403 而非 422）
        chk((await c.post(f"/api/produce/dispatch/{oid}", headers=Hsm,
             json={"sheetmetal_worker_id": smid, "assembly_worker_id": asmid})).status_code == 403, "钣金组不可派发")
        # 角色不符校验：把装配组的人填到钣金组位 → 400
        chk((await c.post(f"/api/produce/dispatch/{oid}", headers=Hpm,
             json={"sheetmetal_worker_id": asmid, "assembly_worker_id": asmid})).status_code == 400, "派发对象角色校验")

        # 主管派发：分别指定钣金组(sm)、装配组(asm) → 建两组任务、生产单 in_progress
        r = await c.post(f"/api/produce/dispatch/{oid}", headers=Hpm,
                         json={"sheetmetal_worker_id": smid, "assembly_worker_id": asmid})
        chk(r.status_code == 200, f"主管派发: {r.text[:120]}")
        async with SessionLocal() as db:
            tasks = {t.group: t for t in (await db.execute(select(models.ProduceGroupTask).where(models.ProduceGroupTask.order_id == oid))).scalars().all()}
            chk(set(tasks) == {"sheetmetal", "assembly"}, f"建钣金+装配两组: {list(tasks)}")
            chk(tasks["sheetmetal"].worker_id == smid and tasks["assembly"].worker_id == asmid, "两组各记派给的人")
            o = (await db.execute(select(models.DeptOrder).where(models.DeptOrder.id == oid))).scalar_one()
            chk(o.status == "in_progress", f"生产单 in_progress: {o.status}")
        # 重复派发=换人（不重复建组）
        await c.post(f"/api/produce/dispatch/{oid}", headers=Hpm,
                     json={"sheetmetal_worker_id": smid, "assembly_worker_id": asmid})
        async with SessionLocal() as db:
            n = len((await db.execute(select(models.ProduceGroupTask).where(models.ProduceGroupTask.order_id == oid))).scalars().all())
            chk(n == 2, f"重复派发仍仅2组: {n}")

        # 按人可见：sm 看到派给自己的；sm2(另一钣金组人)看不到；越权看对方组 403
        smrows = (await c.get("/api/produce/sheetmetal-projects", headers=Hsm)).json()
        chk(len(smrows) == 1 and smrows[0]["project_id"] == pid, f"钣金组看到派给自己的项目: {smrows}")
        chk(smrows[0]["worker_name"] == "sm", f"派给列=sm: {smrows[0].get('worker_name')}")
        chk((await c.get("/api/produce/sheetmetal-projects", headers=Hsm2)).json() == [], "另一钣金组人看不到非自己的")
        # 主管看本组全部（含派给信息）
        chk(len((await c.get("/api/produce/sheetmetal-projects", headers=Hpm)).json()) == 1, "主管看本组全部")
        asmrows = (await c.get("/api/produce/assembly-projects", headers=Hasm)).json()
        chk(len(asmrows) == 1, f"装配组看到派发项目: {asmrows}")
        chk(asmrows[0]["standard_ready"] is False and asmrows[0]["outsource_ready"] is False, f"无记录→未备齐: {asmrows[0]}")
        chk((await c.get("/api/produce/assembly-projects", headers=Hsm)).status_code == 403, "钣金组越权装配列表403")
        chk((await c.get("/api/produce/sheetmetal-projects", headers=Hasm)).status_code == 403, "装配组越权钣金列表403")

        # 🆕 #269 冷作图纸：设计任务产出(order_start_output/coldwork_pkg)聚合到钣金组项目行 coldwork_files
        async with SessionLocal() as db:
            dord = models.DeptOrder(project_id=pid, dept="design", status="in_progress")
            db.add(dord)
            await db.commit()
            db.add(models.Attachment(biz_type="order_start_output", biz_id=dord.id, kind="coldwork_pkg",
                                     project_id=pid, name="冷作图A.dwg", ext="dwg", size=1, path="x/coldwork_a.dwg"))
            await db.commit()
        smrows = (await c.get("/api/produce/sheetmetal-projects", headers=Hsm)).json()
        chk([f["name"] for f in smrows[0].get("coldwork_files", [])] == ["冷作图A.dwg"],
            f"钣金组行聚合冷作图纸: {smrows[0].get('coldwork_files')}")

        # 备齐判定：给「标准件清单」每条记录的「进度」列填「完成」→ standard_ready=True
        async with SessionLocal() as db:
            ds = (await db.execute(select(models.Datasheet).where(
                models.Datasheet.project_id == pid, models.Datasheet.name == "标准件清单"))).scalar_one()
            fld = (await db.execute(select(models.Field).where(
                models.Field.datasheet_id == ds.id, models.Field.name == "进度"))).scalar_one()
            db.add(models.Record(datasheet_id=ds.id, values={str(fld.id): "完成"}))
            db.add(models.Record(datasheet_id=ds.id, values={str(fld.id): "完成"}))
            await db.commit()
        asmrows = (await c.get("/api/produce/assembly-projects", headers=Hasm)).json()
        chk(asmrows[0]["standard_ready"] is True, f"标准件清单全完成→已备齐: {asmrows[0]['standard_ready']}")
        # 再加一条「进行中」→ 立即回落未备齐
        async with SessionLocal() as db:
            ds = (await db.execute(select(models.Datasheet).where(
                models.Datasheet.project_id == pid, models.Datasheet.name == "标准件清单"))).scalar_one()
            fld = (await db.execute(select(models.Field).where(
                models.Field.datasheet_id == ds.id, models.Field.name == "进度"))).scalar_one()
            db.add(models.Record(datasheet_id=ds.id, values={str(fld.id): "进行中"}))
            await db.commit()
        asmrows = (await c.get("/api/produce/assembly-projects", headers=Hasm)).json()
        chk(asmrows[0]["standard_ready"] is False, "有一条进行中→未备齐")

        # 组完成：先钣金完成→生产单仍未完成；再装配完成→生产单 done
        sm_task = smrows[0]["task_id"]; asm_task = asmrows[0]["task_id"]
        r = await c.post(f"/api/produce/group/{sm_task}/done", headers=Hsm, json={"done": True})
        chk(r.status_code == 200, f"钣金标记完成: {r.text[:80]}")
        async with SessionLocal() as db:
            o = (await db.execute(select(models.DeptOrder).where(models.DeptOrder.id == oid))).scalar_one()
            chk(o.status == "in_progress", f"仅钣金完成→生产单未 done: {o.status}")
        r = await c.post(f"/api/produce/group/{asm_task}/done", headers=Hasm, json={"done": True})
        chk(r.status_code == 200, f"装配标记完成: {r.text[:80]}")
        async with SessionLocal() as db:
            o = (await db.execute(select(models.DeptOrder).where(models.DeptOrder.id == oid))).scalar_one()
            chk(o.status == "done" and o.done_date, f"两组都完成→生产单 done: {o.status}/{o.done_date}")
        # 撤销装配完成 → 生产单回 in_progress（发货闸门口径联动）
        await c.post(f"/api/produce/group/{asm_task}/done", headers=Hasm, json={"done": False})
        async with SessionLocal() as db:
            o = (await db.execute(select(models.DeptOrder).where(models.DeptOrder.id == oid))).scalar_one()
            chk(o.status == "in_progress" and o.done_date is None, f"撤销一组→生产单回退: {o.status}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
