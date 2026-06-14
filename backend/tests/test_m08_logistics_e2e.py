"""M08 + 端到端全业务流程测试：
销售下单 → 设计/电工分派→接单→完成 → D5 闸门逐步放开 → 物流确认发货 → 发货日期回传销售台账。
"""
import asyncio, os, sys, tempfile, shutil, io

tmp = tempfile.mkdtemp(prefix="m08test")
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
                          ("lo1","logistics","马师傅"),("bu1","buyer","林采购"),("sm1","sheetmetal","何师傅")]:
            ids[u] = await mk(u, rc, fn)

        async def login(u):
            r = await c.post("/api/auth/login", json={"username":u,"password":"pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        Hs1, Hdl, Hd1, Hel, He1, Hlo = (await login("s1"), await login("dl"), await login("d1"),
                                         await login("el"), await login("e1"), await login("lo1"))

        # ========== ① 销售下单（设计+电工） ==========
        r = await c.post("/api/sales/orders", headers=Hs1, json={
            "code_suffix": "", "name": "300L真空乳化机", "customer": "华东制药",
            "cust_type": "终端客户", "contract": "有", "amount": 128000, "tax_rate": "13%",
            "prepay": 38400, "before_ship": 0, "ship_receivable": 0, "balance": 12800,
            "balance_date": "", "depts": ["design", "electric"],
            "receiver": {"name": "王主管", "phone": "139-0000-1234", "addr": "无锡惠山区工业园8号"}})
        chk(r.status_code == 200, f"销售下单: {r.text[:200]}")
        pid, code = r.json()["project_id"], r.json()["code"]

        # ========== ② 物流看板：待发货行已生成，闸门禁止（进行中） ==========
        r = await c.get("/api/logistics/board", headers=Hlo)
        chk(len(r.json()) == 1, "看板1行")
        row = r.json()[0]
        chk(row["status"] == "pending" and not row["can_ship"], "初始不可发货")
        chk(row["receiver_name"] == "王主管", "收货信息来自销售下单(E2)")
        chk(row["design_state"]["label"] == "进行中" and row["produce_state"]["label"] == "未下单",
            f"部门状态: 设计{row['design_state']['label']} 生产{row['produce_state']['label']}")
        sid = row["id"]
        r = await c.get("/api/logistics/pending-count", headers=Hlo)
        chk(r.json()["count"] == 1, "待发货角标=1")

        # 闸门未过时强行发货被拒（非管理层 force 也无效）
        r = await c.post(f"/api/logistics/{sid}/ship", headers=Hlo, data={"force": "true"},
                         files={"file": ("发货单.pdf", io.BytesIO(b"P"), "application/pdf")})
        chk(r.status_code == 400 and "闸门" in r.text, f"闸门拦截: {r.text[:150]}")

        # ========== ③ 设计链路：分派→接单→导入四表→完成 ==========
        orders = (await c.get("/api/orders?dept=design", headers=Hdl)).json()
        od = orders[0]["id"]
        await c.post(f"/api/orders/{od}/assign", headers=Hdl, json={"worker_id": ids["d1"]})
        await c.post(f"/api/orders/{od}/start", headers=Hd1,
                     json={"start_date": "2026-06-01", "due_date": "2026-12-31"})
        # 模拟四表导入
        from sqlalchemy import update as _upd
        from app import models as M
        from datetime import datetime, timezone
        async with SessionLocal() as db:
            await db.execute(_upd(M.Datasheet).where(M.Datasheet.project_id == pid)
                             .values(imported_at=datetime.now(timezone.utc)))
            await db.commit()
        # 上传完成产物（说明书→物流资料列）
        await c.post(f"/api/orders/{od}/output-upload?kind=manual", headers=Hd1,
                     files=[("files", ("说明书.docx", io.BytesIO(b"DOC"), "application/msword"))])
        r = await c.post(f"/api/orders/{od}/complete", headers=Hd1, json={"notify_user_id": ids["lo1"]})
        chk(r.status_code == 200, f"设计完成: {r.text[:120]}")

        # 闸门：设计done 电工doing → 仍不可发，缺口=电工部
        row = (await c.get("/api/logistics/board", headers=Hlo)).json()[0]
        chk(not row["can_ship"] and row["gate_missing"] == ["电工部"], f"缺口: {row['gate_missing']}")
        chk(len(row["design_files"]) == 1 and row["design_files"][0]["name"] == "说明书.docx",
            "设计产物进看板资料列")

        # ========== ④ 电工链路：分派→接单→电路图→完成 ==========
        orders = (await c.get("/api/orders?dept=electric", headers=Hel)).json()
        oe = orders[0]["id"]
        await c.post(f"/api/orders/{oe}/assign", headers=Hel, json={"worker_id": ids["e1"]})
        await c.post(f"/api/orders/{oe}/start", headers=He1,
                     json={"start_date": "2026-06-01", "due_date": "2026-12-31"})
        await c.post(f"/api/orders/{oe}/output-upload?kind=circuit", headers=He1,
                     files=[("files", ("电路图.pdf", io.BytesIO(b"PDF"), "application/pdf"))])
        r = await c.post(f"/api/orders/{oe}/complete", headers=He1, json={"notify_user_id": ids["bu1"]})
        chk(r.status_code == 200, f"电工完成: {r.text[:120]}")

        # ========== ⑤ 闸门放开 → 收货信息修正（留痕）→ 确认发货 ==========
        row = (await c.get("/api/logistics/board", headers=Hlo)).json()[0]
        chk(row["can_ship"] is True, "两部门完成→可发货(生产未下单不阻塞 D5)")
        chk(len(row["electric_files"]) == 1, "电工电路图进看板")

        r = await c.put(f"/api/logistics/{sid}/receiver", headers=Hlo,
                        json={"name": "王主管", "phone": "139-0000-9999", "addr": "无锡惠山区工业园8号"})
        chk(r.status_code == 200, "物流修正收货信息")

        r = await c.post(f"/api/logistics/{sid}/ship", headers=Hlo,
                         files={"file": ("发货单.pdf", io.BytesIO(b"PDF"), "application/pdf")})
        chk(r.status_code == 200, f"确认发货: {r.text[:150]}")

        # E1：重复发货拒绝
        r = await c.post(f"/api/logistics/{sid}/ship", headers=Hlo,
                         files={"file": ("x.pdf", io.BytesIO(b"P"), "application/pdf")})
        chk(r.status_code == 400, "E1 重复发货被拒")

        # ========== ⑥ 回传验证：销售台账发货日期 + 推送 ==========
        from datetime import date
        rows = (await c.get("/api/sales/ledger", headers=Hs1)).json()["rows"]
        chk(rows[0]["ship_date"] == date.today().isoformat(), f"发货日期回传台账: {rows[0]['ship_date']}")
        msgs = (await c.get("/api/messages", headers=Hs1)).json()
        chk(any("已发货" in m["text"] and "回传" in m["text"] for m in msgs), "销售收发货推送")
        r = await c.get("/api/logistics/pending-count", headers=Hlo)
        chk(r.json()["count"] == 0, "发货后角标=0")

        # ========== ⑦ 存量零任务单兜底：管理层 force ==========
        r = await c.post("/api/projects", headers=H, json={"code": "2025-088", "name": "存量项目"})
        from app.data_migration import backfill_shipments
        async with SessionLocal() as db:
            await backfill_shipments(db)
        board = (await c.get("/api/logistics/board", headers=Hlo)).json()
        legacy = [x for x in board if x["code"] == "2025-088"][0]
        chk(not legacy["can_ship"], "存量零任务单不可发")
        r = await c.post(f"/api/logistics/{legacy['id']}/ship", headers=Hlo, data={"force": "true"},
                         files={"file": ("d.pdf", io.BytesIO(b"P"), "application/pdf")})
        chk(r.status_code == 400, "物流 force 无效(仅管理层)")
        r = await c.post(f"/api/logistics/{legacy['id']}/ship", headers=H, data={"force": "true"},
                         files={"file": ("d.pdf", io.BytesIO(b"P"), "application/pdf")})
        chk(r.status_code in (200, 403), f"管理层 force: {r.status_code}")
        # 注：管理层非 logistics 角色，require_roles 放行 admin/manager → 200
        chk(r.status_code == 200, f"管理层 force 发货: {r.text[:120]}")

        # ========== 🆕 #35 多部门闸门(含生产部)：未完成时多缺口拼接 ==========
        r = await c.post("/api/sales/orders", headers=Hs1, json={
            "name": "三部门机", "customer": "y", "cust_type": "经销商", "contract": "有", "amount": 50000,
            "tax_rate": "13%", "prepay": 0, "before_ship": 0, "ship_receivable": 0, "balance": 0,
            "balance_date": "", "depts": ["design", "electric", "produce"],
            "receiver": {"name": "z", "phone": "1", "addr": "b"}})
        code3 = r.json()["code"]
        board = (await c.get("/api/logistics/board", headers=Hlo)).json()
        row3 = [x for x in board if x["code"] == code3][0]
        chk(not row3["can_ship"], "#35 三部门未完成不可发货")
        chk(set(row3["gate_missing"]) == {"设计部", "电工部", "生产部"},
            f"#35 多缺口拼接含生产部: {row3['gate_missing']}")

        # ========== 🆕 #36 收货信息端点：不存在404；销售角色可改(E2权威初值可被销售改) ==========
        r = await c.put("/api/logistics/999999/receiver", headers=Hlo,
                        json={"name": "x", "phone": "1", "addr": "y"})
        chk(r.status_code == 404, f"#36 不存在shipment改收货信息404: {r.status_code}")
        r = await c.put(f"/api/logistics/{row3['id']}/receiver", headers=Hs1,
                        json={"name": "销售改", "phone": "2", "addr": "z"})
        chk(r.status_code == 200, f"#36 销售可改收货信息: {r.text[:80]}")

        # ========== 🆕 #37 非物流/非管理层确认发货 403 ==========
        r = await c.post(f"/api/logistics/{row3['id']}/ship", headers=Hd1,
                         files={"file": ("x.pdf", io.BytesIO(b"P"), "application/pdf")})
        chk(r.status_code == 403, f"#37 设计师(非物流)确认发货被拒: {r.status_code}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
