"""🆕 采购「预计到货」修复回归测试：
1. 到期(含当天)未到货每日提醒：推采购员本人 + 采购主管 + 管理层，当日幂等；
   采购员兼有管理角色时同日只收一条（角色扇出排除本人）。
2. PUT /items/{iid} 不再接受 arrival_date（只能仓库收货写），传了也被忽略。
3. 改 expected_arrival：回写来源清单「预计到货」列（缺列自动补建）+ 通知主管/管理层(排除操作人)。
4. Excel 一键导入支持「预计到货」列。
5. overdue_scheduler 的 flock：同一时刻只有一个进程能拿到（多 worker 防重复推送）。
"""
import asyncio, os, sys, tempfile, shutil
from datetime import datetime, timezone, timedelta
from io import BytesIO

tmp = tempfile.mkdtemp(prefix="poarr")
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
from app.overdue import scan_po_arrival_overdue, _try_acquire_scheduler_lock

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)

CN = timezone(timedelta(hours=8))
TODAY = datetime.now(CN).date().isoformat()
YESTERDAY = (datetime.now(CN).date() - timedelta(days=1)).isoformat()


async def msgs_for(db, uid, biz_type=None, biz_id=None):
    q = select(models.Message).where(models.Message.to_user_id == uid)
    if biz_type:
        q = q.where(models.Message.biz_type == biz_type)
    if biz_id:
        q = q.where(models.Message.biz_id == biz_id)
    return list((await db.execute(q)).scalars().all())


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

        async def mk(u, rc):
            r = await c.post("/api/admin/users", headers=H,
                             json={"username": u, "password": "pass123", "full_name": u, "role_id": rid[rc]})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        async def login(u):
            return {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': u, 'password': 'pass123'})).json()['access_token']}"}

        admin_id = (await c.get("/api/auth/me", headers=H)).json()["id"]
        b1 = await mk("b1", "buyer")
        bl = await mk("bl", "buyer_lead")
        m1 = await mk("m1", "manager")
        bm = await mk("bm", "buyer")
        Hb1, Hbm = await login("b1"), await login("bm")
        # bm 兼任 manager 副角色（验证角色扇出排除本人，同日只收一条）
        async with SessionLocal() as db:
            db.add(models.UserRole(user_id=bm, role_id=rid["manager"]))
            await db.commit()

        # 供应商
        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb1, json={"name": "测试供应商A"})
        chk(r.status_code == 200, f"建供应商: {r.text[:120]}")
        sid = r.json()["id"]

        # ===== 1. 到期当天即提醒 + 收件人齐全 + 当日幂等 =====
        r = await c.post("/api/purchase-mgmt/items", headers=Hb1, json={
            "supplier_id": sid, "item_name": "轴承", "qty": 2, "unit_price": 10,
            "expected_arrival": TODAY})
        chk(r.status_code == 200, f"建采购明细(预计今天到货): {r.text[:150]}")
        it1 = r.json()["id"]
        async with SessionLocal() as db:
            res = await scan_po_arrival_overdue(db)
            chk(res["notified"] == 1, f"预计今天到货且未收 → 提醒1条: {res}")
            chk(len(await msgs_for(db, b1, "po_arrival_overdue", it1)) == 1, "采购员本人收到提醒")
            chk(len(await msgs_for(db, bl, "po_arrival_overdue", it1)) == 1, "采购主管收到提醒")
            chk(len(await msgs_for(db, m1, "po_arrival_overdue", it1)) == 1, "管理层收到提醒")
            chk(len(await msgs_for(db, admin_id, "po_arrival_overdue", it1)) == 1, "admin 收到提醒")
            res2 = await scan_po_arrival_overdue(db)
            chk(res2["notified"] == 0, f"当日重扫幂等不重复: {res2}")

        # 双重身份（采购+管理副角色）只收一条
        r = await c.post("/api/purchase-mgmt/items", headers=Hbm, json={
            "supplier_id": sid, "item_name": "电机", "expected_arrival": YESTERDAY})
        it2 = r.json()["id"]
        async with SessionLocal() as db:
            await scan_po_arrival_overdue(db)
            chk(len(await msgs_for(db, bm, "po_arrival_overdue", it2)) == 1,
                "采购员兼管理角色同日同明细只收一条")

        # ===== 2. 编辑接口不能改到货日期 =====
        r = await c.put(f"/api/purchase-mgmt/items/{it1}", headers=Hb1, json={"arrival_date": TODAY})
        chk(r.status_code == 200, f"PUT 忽略 arrival_date 不报错: {r.status_code}")
        async with SessionLocal() as db:
            it = (await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.id == it1))).scalar_one()
            chk(it.arrival_date is None, f"arrival_date 未被编辑接口写入(仍为 None): {it.arrival_date!r}")

        # ===== 3. 改预计到货：回写清单(缺列自动补建) + 通知留痕 =====
        async with SessionLocal() as db:
            proj = models.Project(code="T-EA1", name="预计到货测试项目")
            db.add(proj); await db.flush()
            ds = models.Datasheet(project_id=proj.id, name="标准件清单")
            db.add(ds); await db.flush()
            f1 = models.Field(datasheet_id=ds.id, name="项目", type="text", sort_order=1)  # 故意不建「预计到货」列
            db.add(f1); await db.flush()
            rec = models.Record(datasheet_id=ds.id, values={str(f1.id): "轴承"})
            db.add(rec); await db.flush()
            itm = models.PurchaseItem(supplier_id=sid, item_name="轴承", buyer_id=b1,
                                      expected_arrival="2026-08-01",
                                      source_sheet_id=ds.id, source_record_id=rec.id)
            db.add(itm); await db.commit()
            ds_id, rec_id, it3, f1_id = ds.id, rec.id, itm.id, str(f1.id)
        r = await c.put(f"/api/purchase-mgmt/items/{it3}", headers=Hb1, json={"expected_arrival": "2026-08-10"})
        chk(r.status_code == 200 and r.json()["expected_arrival"] == "2026-08-10",
            f"改预计到货成功: {r.text[:150]}")
        async with SessionLocal() as db:
            rec = (await db.execute(select(models.Record).where(models.Record.id == rec_id))).scalar_one()
            flds = {f.name: str(f.id) for f in (await db.execute(
                select(models.Field).where(models.Field.datasheet_id == ds_id))).scalars().all()}
            chk("预计到货" in flds, "缺列时已自动补建「预计到货」字段")
            chk(rec.values.get(flds.get("预计到货")) == "2026-08-10",
                f"清单行已回写新预计到货: {rec.values}")
            chk(rec.values.get(f1_id) == "轴承", "原有单元格不受影响")
            chk(len(await msgs_for(db, bl, "po_expected_changed", it3)) == 1, "改期通知采购主管")
            chk(len(await msgs_for(db, m1, "po_expected_changed", it3)) == 1, "改期通知管理层")
            chk(len(await msgs_for(db, admin_id, "po_expected_changed", it3)) == 1, "改期通知 admin")
            chk(len(await msgs_for(db, b1, "po_expected_changed", it3)) == 0, "操作人本人不重复收改期通知")

        # ===== 3b. 预计到货跟着零件走：从清单下单逐行日期（整单值兜底），逐行回写清单 =====
        async with SessionLocal() as db:
            proj2 = models.Project(code="T-EA2", name="逐行预计到货测试")
            db.add(proj2); await db.flush()
            ds2 = models.Datasheet(project_id=proj2.id, name="标准件清单")
            db.add(ds2); await db.flush()
            f2 = models.Field(datasheet_id=ds2.id, name="项目", type="text", sort_order=1)
            db.add(f2); await db.flush()
            rec_a = models.Record(datasheet_id=ds2.id, values={str(f2.id): "零件甲"})
            rec_b = models.Record(datasheet_id=ds2.id, values={str(f2.id): "零件乙"})
            db.add_all([rec_a, rec_b]); await db.commit()
            ds2_id, ra_id, rb_id = ds2.id, rec_a.id, rec_b.id
        r = await c.post("/api/purchase-mgmt/orders/from-list", headers=Hb1, json={
            "supplier_id": sid, "delivery_date": TODAY,
            "expected_arrival": "2026-08-01",   # 供应商级默认
            "lines": [
                {"source_sheet_id": ds2_id, "source_record_id": ra_id,
                 "item_name": "零件甲", "qty": 1, "expected_arrival": "2026-08-05"},   # 逐行覆盖
                {"source_sheet_id": ds2_id, "source_record_id": rb_id,
                 "item_name": "零件乙", "qty": 2},                                       # 兜底用整单值
            ]})
        chk(r.status_code == 200, f"从清单下单(逐行预计到货): {r.text[:200]}")
        async with SessionLocal() as db:
            its = {i.item_name: i for i in (await db.execute(select(models.PurchaseItem).where(
                models.PurchaseItem.item_name.in_(["零件甲", "零件乙"])))).scalars().all()}
            chk(its["零件甲"].expected_arrival == "2026-08-05", f"逐行日期优先: {its['零件甲'].expected_arrival!r}")
            chk(its["零件乙"].expected_arrival == "2026-08-01", f"整单值兜底: {its['零件乙'].expected_arrival!r}")
            flds2 = {f.name: str(f.id) for f in (await db.execute(
                select(models.Field).where(models.Field.datasheet_id == ds2_id))).scalars().all()}
            ea_fid = flds2.get("预计到货")
            chk(ea_fid is not None, "清单已补建「预计到货」列")
            va = (await db.execute(select(models.Record).where(models.Record.id == ra_id))).scalar_one().values
            vb = (await db.execute(select(models.Record).where(models.Record.id == rb_id))).scalar_one().values
            chk(va.get(ea_fid) == "2026-08-05", f"零件甲行回写自己的日期: {va.get(ea_fid)!r}")
            chk(vb.get(ea_fid) == "2026-08-01", f"零件乙行回写兜底日期: {vb.get(ea_fid)!r}")

        # ===== 3c. 新建采购单：逐行预计到货覆盖表头默认 =====
        r = await c.post("/api/purchase-mgmt/orders", headers=Hb1, json={
            "supplier_id": sid, "expected_arrival": "2026-08-10",
            "lines": [
                {"item_name": "逐行件A", "qty": 1, "expected_arrival": "2026-08-20"},
                {"item_name": "逐行件B", "qty": 1},
            ]})
        chk(r.status_code == 200, f"新建采购单(逐行预计到货): {r.text[:200]}")
        async with SessionLocal() as db:
            its = {i.item_name: i for i in (await db.execute(select(models.PurchaseItem).where(
                models.PurchaseItem.item_name.in_(["逐行件A", "逐行件B"])))).scalars().all()}
            chk(its["逐行件A"].expected_arrival == "2026-08-20", f"新建单逐行优先: {its['逐行件A'].expected_arrival!r}")
            chk(its["逐行件B"].expected_arrival == "2026-08-10", f"新建单表头兜底: {its['逐行件B'].expected_arrival!r}")

        # ===== 4. Excel 导入带「预计到货」列 =====
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active
        ws.append(["供应商名称*", "采购单号", "下单日期", "订单编号", "名称*", "规格型号",
                   "数量", "单价", "合计金额", "送货单号", "预计到货", "到货日期", "付款方式",
                   "开票日期", "开票金额", "税率", "对账状态", "备注"])
        ws.append(["导入供应商B", "", "2026-07-01", "", "导轨", "HIWIN-15", 3, 50, 150, "",
                   "2026-09-15", "", "转账", "", "", "13%", "待对账", ""])
        bio = BytesIO(); wb.save(bio); bio.seek(0)
        r = await c.post("/api/purchase-mgmt/items/import", headers=Hb1,
                         files={"file": ("imp.xlsx", bio.getvalue(),
                                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        chk(r.status_code == 200 and r.json()["created"] == 1, f"导入1条: {r.text[:150]}")
        async with SessionLocal() as db:
            it = (await db.execute(select(models.PurchaseItem).where(
                models.PurchaseItem.item_name == "导轨"))).scalar_one()
            chk(it.expected_arrival == "2026-09-15", f"导入落预计到货: {it.expected_arrival!r}")

    # ===== 5. scheduler flock 单实例 =====
    h1 = _try_acquire_scheduler_lock()
    chk(bool(h1), "第一个进程拿到 scheduler 锁")
    h2 = _try_acquire_scheduler_lock()
    chk(h2 is None, "锁被占用时第二个进程拿不到(跳过)")
    if h1 and h1 != "no-lock":
        h1.close()
        h3 = _try_acquire_scheduler_lock()
        chk(bool(h3), "锁释放后可重新拿到")
        if h3 and h3 != "no-lock":
            h3.close()

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
