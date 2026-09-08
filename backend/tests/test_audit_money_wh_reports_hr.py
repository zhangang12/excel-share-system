"""金额审计(2026-09-09)·仓库库存/项目材料成本、毛利榜与销售报表口径、工资导入。

审查发现（详见 交接文档「金额审计」一节）：
  · 一键领用同一物料多行时各行都按"全部现存"领 → 负库存、负库存金额
  · 「库位调项目物料」可把别的项目已收货的料（收货时已计入原项目成本）调给新项目 → 成本双计
  · 项目材料成本腿A 用 SQL SUM 跳过 NULL：同一(项目,物料)部分行无价时那部分按 0 计、也不亮"缺价"
  · 毛利榜直发腿把**未到货**的采购（received_amount 下单时就=订单金额）也算成项目成本（生产 90 行 ¥62.7k）
  · 待主管审批/被退回的销售订单合同额进了销售总额/业绩榜/毛利榜
  · 工资导入：非空但解析不了的格子静默写 0 并整行覆盖；同工号两行撞唯一约束 500
"""
import asyncio, io, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="auditwh")
os.environ["DATABASE_URL"] = os.environ.get("AUDIT_TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp}/test.db"
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


async def _stock(mid):
    from app.routers.warehouse_router import _stock_map
    async with SessionLocal() as db:
        return (await _stock_map(db, [mid])).get(mid, 0)


async def _project(code):
    async with SessionLocal() as db:
        p = models.Project(code=code, name=f"项目{code}", status="进行中")
        db.add(p)
        await db.commit()
        return p.id


def _xlsx(rows):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return {"file": ("工资.xlsx", bio, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        async def login(u, p="pass123"):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(name, codes):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123", "full_name": name,
                "role_ids": [rid[x] for x in codes]})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        await mkuser("aw_wh", ["warehouse"])
        await mkuser("aw_fin", ["finance", "finance_lead"])
        await mkuser("aw_sl", ["sales_lead"])
        await mkuser("aw_hr", ["hr"])
        await mkuser("aw_buyer", ["buyer"])
        Hw, Hf, Hs, Hh, Hb = (await login("aw_wh"), await login("aw_fin"), await login("aw_sl"),
                              await login("aw_hr"), await login("aw_buyer"))

        async def material(name, **kw):
            r = await c.post("/api/wh/materials", headers=Hw, json={"name": name, "unit": "个", **kw})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        async def txn(mid, direction, qty, **kw):
            body = {"material_id": mid, "biz_date": "2026-09-01", "direction": direction, "qty": qty, **kw}
            r = await c.post("/api/wh/txns", headers=Hw, json=body)
            assert r.status_code == 200, r.text
            return r.json()

        # ══════════════ ① 一键领用：同物料多行不能打穿负库存 ══════════════
        print("\n① 一键领用")
        p1 = await _project("AW-001")
        m1 = await material("M8 螺栓", init_stock=10, unit_price=2)
        r = await c.post(f"/api/wh/demand/{p1}/issue", headers=Hw,
                         json={"lines": [{"material_id": m1, "qty": 10}, {"material_id": m1, "qty": 10}]})
        chk(r.status_code == 200, f"两行同物料一键领用 -> {r.status_code} {r.text[:80]}")
        st = await _stock(m1)
        chk(abs(st) < 1e-9, f"现存 10 领两行各 10 → 现存 0（原来 -10）-> {st}")

        # ══════════════ ② 调拨只能动通用池 ══════════════
        print("\n② 调拨")
        p2, p3 = await _project("AW-002"), await _project("AW-003")
        m2 = await material("行星箱", unit_price=100)
        await txn(m2, "in", 5, project_id=p2, unit_price=100)       # 项目物料：收货即计 p2 成本
        await txn(m2, "in", 3, unit_price=100)                      # 通用入库
        r = await c.post("/api/wh/transfer-to-project", headers=Hw,
                         json={"project_id": p3, "lines": [{"material_id": m2, "qty": 5}]})
        chk(r.status_code == 400 and "通用库存仅 3" in r.text,
            f"总现存 8 但通用池只有 3 → 调 5 被拒（否则 p2 的料算两遍）-> {r.status_code} {r.text[:90]}")
        r = await c.post("/api/wh/transfer-to-project", headers=Hw,
                         json={"project_id": p3, "lines": [{"material_id": m2, "qty": 3}]})
        chk(r.status_code == 200, f"调 3 个通用料可以 -> {r.status_code}")
        cost = (await c.get("/api/wh/project-cost", headers=Hf)).json()
        by = {x["project_id"]: x["cost"] for x in cost["rows"]}
        chk(abs(by.get(p2, 0) - 500) < 0.01 and abs(by.get(p3, 0) - 300) < 0.01,
            f"项目成本 p2=500 / p3=300，合计 800 = 8 个 × 100，没有双计 -> {by.get(p2)}/{by.get(p3)}")

        # ══════════════ ③ 腿A 部分无价行按均价补 ══════════════
        print("\n③ 收货腿部分无价")
        p4 = await _project("AW-004")
        m3 = await material("卷圆")
        await txn(m3, "in", 2, project_id=p4, unit_price=10)        # 有价 20
        await txn(m3, "in", 3, project_id=p4)                       # 无价（原来这 3 个按 0 计）
        cost = (await c.get("/api/wh/project-cost", headers=Hf)).json()
        by = {x["project_id"]: x["cost"] for x in cost["rows"]}
        chk(abs(by.get(p4, 0) - 50) < 0.01, f"2×10 + 3×均价10 = 50（原来只算 20）-> {by.get(p4)}")

        # ══════════════ ④ 毛利榜：未到货不算成本；待审批订单不算收入 ══════════════
        print("\n④ 毛利榜与销售报表")
        p5 = await _project("AW-005")
        sid = (await c.post("/api/purchase-mgmt/suppliers", headers=Hb, json={"name": "审计供应商"})).json()["id"]
        r = await c.post("/api/purchase-mgmt/orders", headers=Hb, json={
            "supplier_id": sid, "project_code": "AW-005",
            "lines": [{"item_name": "外协件", "qty": 1, "unit_price": 100}]})
        iid = r.json()[0]["id"]
        async with SessionLocal() as db:
            db.add(models.SalesLedger(project_id=p5, amount=1000, customer="待审客户", order_state="pending"))
            await db.commit()
        pnl = (await c.get("/api/reports/project-pnl", headers=Hf)).json()
        row = next((x for x in pnl["rows"] if x["project_id"] == p5), None)
        chk(row is None or (row["direct_cost"] == 0 and row["amount"] == 0),
            f"未到货的采购不进直发腿、待审批订单不算收入 -> {row and (row['direct_cost'], row['amount'], row['flags'])}")
        await c.put(f"/api/purchase-mgmt/items/{iid}/receive", headers=Hw, json={"arrival_date": "2026-09-02"})
        pnl = (await c.get("/api/reports/project-pnl", headers=Hf)).json()
        row = next((x for x in pnl["rows"] if x["project_id"] == p5), None)
        chk(row is not None and abs(row["mat_cost"] - 100) < 0.01 and row["direct_cost"] == 0,
            f"收货后走材料腿 100、直发腿 0（两腿互斥）-> {row and (row['mat_cost'], row['direct_cost'])}")
        chk(row is not None and "待审批" in "".join(row["flags"]), f"待审批订单打标 -> {row and row['flags']}")
        sr = (await c.get("/api/reports/sales", headers=Hs)).json()
        chk(sr["total_amount"] == 0, f"销售报表总额不含待审批订单的 1000 -> {sr['total_amount']}")
        async with SessionLocal() as db:
            led = (await db.execute(select(models.SalesLedger).where(models.SalesLedger.project_id == p5))).scalar_one()
            led.order_state = None
            led.tax_rate = "/"
            await db.commit()
        sr = (await c.get("/api/reports/sales", headers=Hs)).json()
        chk(sr["total_amount"] == 1000 and sr["uninvoiced_amount"] == 0 and sr["invoice_rate"] is None,
            f"生效后计入总额；税票 '/' 不开票 → 不算未开票、不进开票率分母 -> {sr['total_amount']}/{sr['uninvoiced_amount']}/{sr['invoice_rate']}")

        # ══════════════ ⑤ 工资导入 ══════════════
        print("\n⑤ 工资导入")
        r = await c.post("/api/hr/employees", headers=Hh, json={"name": "张三"})
        assert r.status_code == 200, r.text
        no = r.json()["emp_no"]
        head = ["工号*", "姓名", "基本工资", "绩效/奖金", "加班费", "补贴", "社保公积金", "个税", "其他扣款", "备注"]
        r = await c.post("/api/hr/salary/2026-13/import", headers=Hh, files=_xlsx([head, [no, "张三", 6000]]))
        chk(r.status_code == 400 and "01–12" in r.text, f"月份 2026-13 被拒 -> {r.status_code}")
        r = await c.post("/api/hr/salary/2026-08/import", headers=Hh,
                         files=_xlsx([head, [no, "张三", "6000元", 500, 0, 0, 0, 0, 0, ""]]))
        chk(r.status_code == 400 and "第2行" in r.text and "基本工资" in r.text,
            f"「6000元」解析不了 → 整批拒绝并指出行列（原来静默当 0 写库）-> {r.status_code} {r.text[:100]}")
        r = await c.post("/api/hr/salary/2026-08/import", headers=Hh,
                         files=_xlsx([head, [no, "张三", 6000], [no, "张三", 7000]]))
        chk(r.status_code == 400 and "重复" in r.text, f"同工号两行 → 400 提示（原来撞唯一约束 500）-> {r.status_code}")
        r = await c.post("/api/hr/salary/2026-08/import", headers=Hh,
                         files=_xlsx([head, [no, "张三", "6,000", "1，200", 0, 0, "¥800", 0, 0, ""]]))
        chk(r.status_code == 200, f"千分位/全角逗号/货币符可解析 -> {r.status_code} {r.text[:80]}")
        async with SessionLocal() as db:
            s = (await db.execute(select(models.EmployeeSalaryMonthly).where(
                models.EmployeeSalaryMonthly.period == "2026-08"))).scalar_one()
        chk(s.base == 6000 and s.merit == 1200 and s.social_deduct == 800, f"落库 6000/1200/800 -> {s.base}/{s.merit}/{s.social_deduct}")
        r = await c.put("/api/hr/salary/2026-08", headers=Hh, json={"rows": [
            {"employee_id": s.employee_id, "base": -1}]})
        chk(r.status_code == 400, f"手工保存负数工资被拒 -> {r.status_code}")

    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败")
        sys.exit(1)
    print("✅ 全部通过")


asyncio.run(main())
