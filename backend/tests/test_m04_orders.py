"""M04/M17 部门任务单全状态机集成测试（临时 SQLite）。

覆盖：双入口下单/B2/分派/接单回传一览/B5/接单上传推送/D1四表校验/必传产物/
通知人校验/逾期推送/完成回写/reopen/作废/换人/可见性隔离/存量imported_at回填。
"""
import asyncio, os, sys, tempfile, shutil, io

tmp = tempfile.mkdtemp(prefix="m04test")
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

        async def mk(uname, rc):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "full_name": uname.upper(), "role_id": rid[rc]})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        ids = {}
        for u, rc in [("d1","designer"),("dl","design_lead"),("e1","electrician"),
                      ("e2","electrician"),("el","electric_lead"),("a1","assembler"),
                      ("pm","pm_lead"),("lo","logistics"),("bu","buyer"),("sm","sheetmetal")]:
            ids[u] = await mk(u, rc)

        async def login(u):
            r = await c.post("/api/auth/login", json={"username":u,"password":"pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        r = await c.post("/api/projects", headers=H, json={"code":"T-040","name":"测试搅拌机"})
        pid = r.json()["id"]

        # ---- 下单：管理层带指派(设计) + 不带(电工待分派) ----
        r = await c.post("/api/orders", headers=H, json={"project_id":pid,"dept":"design","req_text":"出装配图","worker_id":ids["d1"]})
        chk(r.status_code==200 and r.json()["status"]=="assigned", f"目录下单直接指派: {r.text[:150]}")
        o_design = r.json()["id"]
        r = await c.post("/api/orders", headers=H, json={"project_id":pid,"dept":"electric","req_text":"接线"})
        chk(r.json()["status"]=="pending_assign", "电工单待分派")
        o_elec = r.json()["id"]
        # 工人收到分派消息
        Hd = await login("d1")
        msgs = (await c.get("/api/messages", headers=Hd)).json()
        chk(any("分派" in m["text"] for m in msgs), "设计工人收到分派消息")
        # 指派对象角色校验
        r = await c.post("/api/orders", headers=H, json={"project_id":pid,"dept":"design","worker_id":ids["e1"]})
        chk(r.status_code==400, "跨部门指派被拒")

        # ---- B2：非进行中项目不可下单 ----
        r = await c.put(f"/api/projects/{pid}", headers=H, json={"status":"已完成"})
        r = await c.post("/api/orders", headers=H, json={"project_id":pid,"dept":"produce"})
        chk(r.status_code==400, f"B2 已完成项目下单被拒: {r.status_code}")
        await c.put(f"/api/projects/{pid}", headers=H, json={"status":"进行中"})

        # ---- 分派：电工负责人分派 e1；非负责人被拒 ----
        r = await c.post(f"/api/orders/{o_elec}/assign", headers=Hd, json={"worker_id":ids["e1"]})
        chk(r.status_code==403, "设计师不能分派电工单")
        Hel = await login("el")
        r = await c.post(f"/api/orders/{o_elec}/assign", headers=Hel, json={"worker_id":ids["e1"]})
        chk(r.status_code==200, f"电工负责人分派: {r.text[:120]}")

        # ---- 接单：日期校验 + 回传一览 ----
        r = await c.post(f"/api/orders/{o_design}/start", headers=Hd, json={"start_date":"2026-06-10","due_date":"2026-06-01"})
        chk(r.status_code==400, "due<start 被拒")
        r = await c.post(f"/api/orders/{o_design}/start", headers=Hd, json={"start_date":"2026-06-01","due_date":"2026-06-10"})
        chk(r.status_code==200, f"设计接单: {r.text[:120]}")
        r = await c.get(f"/api/projects/{pid}", headers=H)
        om = r.json()["overview_meta"]; hm = r.json()["header_meta"]
        chk(om.get("设计师")=="D1", f"接单回传设计师: {om.get('设计师')}")
        chk(om.get("制图开始")=="2026-06-01", "接单回传制图开始")
        chk(hm.get("设计师")=="D1", "alias 双写 __h__设计师")
        He1 = await login("e1")
        r = await c.post(f"/api/orders/{o_elec}/start", headers=He1, json={"start_date":"2026-06-01","due_date":"2026-06-05"})
        chk(r.status_code==200, "电工接单")
        om = (await c.get(f"/api/projects/{pid}", headers=H)).json()["overview_meta"]
        hm = (await c.get(f"/api/projects/{pid}", headers=H)).json()["header_meta"]
        chk(om.get("电工")=="E1", f"接单回传电工: {om.get('电工')}")
        chk(hm.get("电器")=="E1", "alias 双写 __h__电器")
        # B5：重复开始被拒
        r = await c.post(f"/api/orders/{o_design}/start", headers=Hd, json={"start_date":"2026-06-02","due_date":"2026-06-11"})
        chk(r.status_code==400, "B5/状态机: 已开始不可重填时间")

        # ---- 接单上传：设计图纸包→钣金；电工清单→采购 ----
        r = await c.post(f"/api/orders/{o_design}/start-upload?kind=sheetpkg", headers=Hd,
                         files=[("files", ("总装图.pdf", io.BytesIO(b"PDF1"), "application/pdf")),
                                ("files", ("钣金件图.pdf", io.BytesIO(b"PDF2"), "application/pdf"))])
        chk(r.status_code==200 and len(r.json())==2, f"图纸包多文件上传: {r.text[:150]}")
        att_pkg = r.json()[0]["id"]
        Hsm = await login("sm")
        msgs = (await c.get("/api/messages", headers=Hsm)).json()
        chk(any("图纸包" in m["text"] for m in msgs), "钣金组收到图纸包推送")
        r = await c.post(f"/api/orders/{o_elec}/start-upload?kind=plist", headers=He1,
                         files=[("files", ("采购清单.xlsx", io.BytesIO(b"XL"), "application/vnd.ms-excel"))])
        chk(r.status_code==200, "电工采购清单上传")
        # 🆕 改口径: 电工采购清单已直接进项目详单「电工采购单」, 不再单独推送采购部
        Hbu = await login("bu")
        msgs = (await c.get("/api/messages", headers=Hbu)).json()
        chk(not any("采购清单" in m["text"] for m in msgs), "电工采购清单不再推送采购部(已进电工采购单)")
        # 移除附件
        r = await c.delete(f"/api/orders/{o_design}/attachments/{att_pkg}", headers=Hd)
        chk(r.status_code==200, "移除图纸包文件")

        # ---- 完成：D1 四表校验拦截 → 模拟导入 → 通知人校验 → 完成 ----
        r = await c.post(f"/api/orders/{o_design}/complete", headers=Hd, json={"notify_user_id":ids["lo"]})
        chk(r.status_code==400 and "四表" in r.text, f"D1 四表未导入被拦: {r.text[:150]}")
        # 模拟四表导入（直接置 imported_at）
        from sqlalchemy import update as _upd
        from app import models as M
        from datetime import datetime, timezone
        async with SessionLocal() as db:
            await db.execute(_upd(M.Datasheet).where(M.Datasheet.project_id==pid)
                             .values(imported_at=datetime.now(timezone.utc)))
            await db.commit()
        # 通知人必须在通知池
        r = await c.post(f"/api/orders/{o_design}/complete", headers=Hd, json={"notify_user_id":ids["bu"]})
        chk(r.status_code==400, "通知人不在物流池被拒")
        r = await c.post(f"/api/orders/{o_design}/complete", headers=Hd, json={"notify_user_id":ids["lo"]})
        chk(r.status_code==200, f"设计完成: {r.text[:120]}")
        om = (await c.get(f"/api/projects/{pid}", headers=H)).json()["overview_meta"]
        chk(bool(om.get("制图结束")), "完成回写制图结束")
        Hlo = await login("lo")
        msgs = (await c.get("/api/messages", headers=Hlo)).json()
        chk(any("已完成" in m["text"] for m in msgs), "通知人收到完成推送")

        # ---- 电工完成：必传电路图拦截 → 上传 → 逾期完成推送主管 ----
        r = await c.post(f"/api/orders/{o_elec}/complete", headers=He1, json={"notify_user_id":ids["bu"]})
        chk(r.status_code==400 and "电路图" in r.text, f"必传电路图被拦: {r.text[:150]}")
        r = await c.post(f"/api/orders/{o_elec}/output-upload?kind=circuit", headers=He1,
                         files=[("files", ("电路图.pdf", io.BytesIO(b"PDF"), "application/pdf"))])
        chk(r.status_code==200, "电路图上传")
        r = await c.post(f"/api/orders/{o_elec}/complete", headers=He1, json={"notify_user_id":ids["bu"]})
        chk(r.status_code==200, f"电工完成: {r.text[:150]}")
        # 🆕 #50 完成(done)单不得再上传产物（状态守卫，避免改下游资料/破坏留痕）
        r = await c.post(f"/api/orders/{o_elec}/output-upload?kind=circuit", headers=He1,
                         files=[("files", ("追加.pdf", io.BytesIO(b"PDF"), "application/pdf"))])
        chk(r.status_code==400 and "进行中" in r.text, f"#50 done单output-upload被拒: {r.status_code} {r.text[:80]}")
        # due=2026-06-05 < today → 逾期，主管+管理层收 warn
        msgs = (await c.get("/api/messages", headers=Hel)).json()
        chk(any("逾期完成" in m["text"] and "效率" in m["text"] for m in msgs), "电工主管收逾期推送")
        # 效率字段
        orders = (await c.get("/api/orders?dept=electric", headers=H)).json()
        oe = [o for o in orders if o["id"]==o_elec][0]
        chk(oe["eff_pct"] is not None and oe["on_time"] is False, f"效率口径: eff={oe['eff_pct']} on_time={oe['on_time']}")

        # ---- reopen：完成可逆，制图结束清空 ----
        r = await c.post(f"/api/orders/{o_design}/reopen", headers=Hd)
        chk(r.status_code==200, "reopen")
        om = (await c.get(f"/api/projects/{pid}", headers=H)).json()["overview_meta"]
        chk(not om.get("制图结束"), "reopen 清制图结束")

        # ---- 作废：负责人作废待分派单；已完成不可作废 ----
        r = await c.post("/api/orders", headers=H, json={"project_id":pid,"dept":"produce"})
        o_prod = r.json()["id"]
        Hpm = await login("pm")
        r = await c.post(f"/api/orders/{o_prod}/void", headers=Hpm)
        chk(r.status_code==200, "生产主管作废待分派单")
        r = await c.post(f"/api/orders/{o_elec}/void", headers=Hel)
        chk(r.status_code==400, "已完成单不可作废")

        # ---- M17 换人：进行中设计单 d1→? 需同部门；建二号设计师 ----
        ids["d2"] = await mk("d2", "designer")
        Hdl = await login("dl")
        r = await c.post(f"/api/orders/{o_design}/reassign", headers=Hdl, json={"worker_id":ids["d1"]})
        chk(r.status_code==400, "换人不能转给本人")
        r = await c.post(f"/api/orders/{o_design}/reassign", headers=Hdl, json={"worker_id":ids["e2"]})
        chk(r.status_code==400, "跨部门换人被拒")
        r = await c.post(f"/api/orders/{o_design}/reassign", headers=Hdl, json={"worker_id":ids["d2"]})
        chk(r.status_code==200, f"换人成功: {r.text[:120]}")
        om = (await c.get(f"/api/projects/{pid}", headers=H)).json()["overview_meta"]
        chk(om.get("设计师")=="D2", f"换人回传设计师: {om.get('设计师')}")
        Hd2 = await login("d2")
        msgs = (await c.get("/api/messages", headers=Hd2)).json()
        chk(any("任务转交" in m["text"] for m in msgs), "新负责人收转交推送")

        # ---- 可见性：工人只见自己；负责人见本部门；管理层全量 ----
        r = await c.get("/api/orders?dept=design", headers=Hd)   # d1 已被换走
        chk(len(r.json())==0, f"原负责人换走后看不到任务: {len(r.json())}")
        r = await c.get("/api/orders?dept=design", headers=Hd2)
        chk(len(r.json())==1, "新负责人看到任务")
        r = await c.get("/api/orders?dept=design", headers=Hdl)
        chk(len(r.json())==1, f"设计负责人看本部门全量: {len(r.json())}")
        r = await c.get("/api/orders", headers=H)
        chk(len(r.json())==3, f"管理层全量(设计+电工+作废生产): {len(r.json())}")
        # 无关角色被拒
        Ha1 = await login("a1")
        r = await c.get("/api/orders?dept=design", headers=Ha1)
        chk(all(x["dept"]!="design" for x in r.json()) or r.status_code==403 or len(r.json())==0,
            "装配工查设计部任务无数据")

        # ---- options 下拉 ----
        r = await c.get("/api/orders/options?dept=design", headers=Hdl)
        j = r.json()
        chk(len(j["workers"])==2 and j["sheet_check"] is True and j["notify_label"], f"options: {j['dept_name']}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
