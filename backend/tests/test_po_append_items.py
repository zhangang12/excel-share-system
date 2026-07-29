"""🆕 反馈#322：向已有采购单追加零件行（POST /api/purchase-mgmt/orders/{po_no}/items）：
1. 追加成功：行落同一 po_no，表头（供应商/下单日期/付款方式）沿用原单，buyer_id=追加人；
   行不填预计到货 → 沿用原单值；逐行填了 → 用行值；
2. 采购单不存在 → 404；lines 全空 → 400；
3. 受限采购员(_buyer_restricted)只能给自己的单追加：别人的单 → 403；admin 不受限；
4. 清单回写：行带来源清单 → 回写 采购负责人/下单日期(沿用原单)/预计到货，与从清单下单同口径；
   已手填的采购负责人不被覆盖（#255 口径）；
5. 按清单分工(_allowed_sheet_keys)：lixinxin 追加来源为「外协加工」表的行 → 403（同 from-list 口径）。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="poappend")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models
from sqlalchemy import select

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)

URL = "/api/purchase-mgmt/orders"


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

        async def mk(u, rc, full=None):
            r = await c.post("/api/admin/users", headers=H,
                             json={"username": u, "password": "pass123", "full_name": full or u, "role_id": rid[rc]})
            assert r.status_code == 200, r.text
            return r.json()["id"]
        async def login(u):
            return {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': u, 'password': 'pass123'})).json()['access_token']}"}

        b1id = await mk("b1", "buyer", "采购员甲")
        b2id = await mk("b2", "buyer", "采购员乙")
        await mk("lixinxin", "buyer", "李新新")   # 分工表内：只管 标准件清单/电工采购单
        Hb1, Hb2, Hlxx = await login("b1"), await login("b2"), await login("lixinxin")

        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb1, json={"name": "追加测试供应商"})
        chk(r.status_code == 200, f"建供应商: {r.text[:120]}")
        sid = r.json()["id"]

        # ===== 造来源清单：标准件清单（含 订购日期/采购负责人/预计到货 列） + 外协加工 =====
        async with SessionLocal() as db:
            p = models.Project(code="T-AP1", name="追加测试项目", status="进行中")
            db.add(p); await db.flush()
            ds = models.Datasheet(project_id=p.id, name="标准件清单")
            db.add(ds); await db.flush()
            f_nm = models.Field(datasheet_id=ds.id, name="项目", type="text", sort_order=1)
            f_sp = models.Field(datasheet_id=ds.id, name="规格型号", type="text", sort_order=2)
            f_od = models.Field(datasheet_id=ds.id, name="订购日期", type="date", sort_order=3)
            f_by = models.Field(datasheet_id=ds.id, name="采购负责人", type="text", sort_order=4)
            f_ea = models.Field(datasheet_id=ds.id, name="预计到货", type="date", sort_order=5)
            db.add_all([f_nm, f_sp, f_od, f_by, f_ea]); await db.flush()
            rec = models.Record(datasheet_id=ds.id, values={str(f_nm.id): "内六角螺丝", str(f_sp.id): "M6×16"})
            db.add(rec); await db.flush()
            # 已手填采购负责人的行（#255：不被下单人覆盖）
            rec2 = models.Record(datasheet_id=ds.id, values={str(f_nm.id): "弹垫", str(f_sp.id): "M6",
                                                             str(f_by.id): "手填负责人"})
            db.add(rec2); await db.flush()
            ds_o = models.Datasheet(project_id=p.id, name="外协加工")
            db.add(ds_o); await db.flush()
            f_on = models.Field(datasheet_id=ds_o.id, name="名称", type="text", sort_order=1)
            db.add(f_on); await db.flush()
            rec_o = models.Record(datasheet_id=ds_o.id, values={str(f_on.id): "外协件A"})
            db.add(rec_o); await db.flush()
            await db.commit()
            ds_id, rec_id, rec2_id, ds_o_id, rec_o_id = ds.id, rec.id, rec2.id, ds_o.id, rec_o.id
            f_by_id, f_od_id, f_ea_id = str(f_by.id), str(f_od.id), str(f_ea.id)

        # ===== b1 建采购单（2 行，带整单预计到货/付款方式） =====
        r = await c.post(URL, headers=Hb1, json={
            "supplier_id": sid, "delivery_date": "2026-07-20", "expected_arrival": "2026-08-01",
            "project_code": "T-AP1", "payment_method": "对公全款",
            "lines": [
                {"item_name": "六角螺栓", "spec": "M8×20", "qty": 10, "unit_price": 1.5},
                {"item_name": "平垫圈", "spec": "M8", "qty": 20, "unit_price": 0.2},
            ]})
        chk(r.status_code == 200, f"建采购单: {r.text[:200]}")
        po_no = r.json()[0]["po_no"]
        chk(po_no and len(r.json()) == 2, f"初始2行+单号: {po_no}")

        APP = f"{URL}/{po_no}/items"

        # ===== 1. 追加成功：无来源行（漏下的螺丝），表头沿用原单 =====
        r = await c.post(APP, headers=Hb1, json={"lines": [
            {"item_name": "弹簧垫圈", "spec": "M8", "qty": 20, "unit_price": 0.3}]})
        chk(r.status_code == 200, f"追加 200: {r.status_code} {r.text[:200]}")
        rows = r.json()
        chk(len(rows) == 3 and all(x["po_no"] == po_no for x in rows), f"整单变3行同单号: {len(rows)}")
        new = [x for x in rows if x["item_name"] == "弹簧垫圈"]
        chk(len(new) == 1, "新行在内")
        if new:
            x = new[0]
            chk(x["supplier_id"] == sid and x["delivery_date"] == "2026-07-20"
                and x["payment_method"] == "对公全款" and x["project_code"] == "T-AP1",
                f"表头沿用原单: {x}")
            chk(x["expected_arrival"] == "2026-08-01", f"预计到货沿用原单: {x['expected_arrival']}")
            chk(x["buyer_id"] == b1id, f"buyer_id=追加人: {x['buyer_id']}")
            chk(abs(x["received_amount"] - 6.0) < 1e-6, f"金额=数量×单价: {x['received_amount']}")

        # ===== 4a. 带来源清单行追加 → 回写 采购负责人/下单日期/预计到货 =====
        r = await c.post(APP, headers=Hb1, json={"lines": [
            {"item_name": "内六角螺丝", "spec": "M6×16", "qty": 5, "unit_price": 0.8,
             "expected_arrival": "2026-08-10",
             "source_sheet_id": ds_id, "source_record_id": rec_id}]})
        chk(r.status_code == 200, f"带来源追加 200: {r.status_code} {r.text[:200]}")
        async with SessionLocal() as db:
            rec_now = (await db.execute(select(models.Record).where(models.Record.id == rec_id))).scalar_one()
            v = rec_now.values or {}
            chk(v.get(f_by_id) == "采购员甲", f"回写采购负责人: {v.get(f_by_id)}")
            chk(v.get(f_od_id) == "2026-07-20", f"回写下单日期=原单下单日期: {v.get(f_od_id)}")
            chk(v.get(f_ea_id) == "2026-08-10", f"回写逐行预计到货: {v.get(f_ea_id)}")
        # 追加行本身落库 source 引用 + 行级预计到货
        rows = (await c.get(f"{URL}/{po_no}", headers=Hb1)).json()
        src = [x for x in rows if x["item_name"] == "内六角螺丝"]
        chk(len(src) == 1 and src[0]["expected_arrival"] == "2026-08-10", f"来源行落库: {src}")

        # ===== 4b. 已手填采购负责人不被覆盖（#255 口径），下单日期照常回写 =====
        r = await c.post(APP, headers=Hb1, json={"lines": [
            {"item_name": "弹垫", "spec": "M6", "qty": 5,
             "source_sheet_id": ds_id, "source_record_id": rec2_id}]})
        chk(r.status_code == 200, f"手填行追加 200: {r.status_code}")
        async with SessionLocal() as db:
            v = ((await db.execute(select(models.Record).where(models.Record.id == rec2_id))).scalar_one()).values or {}
            chk(v.get(f_by_id) == "手填负责人", f"手填负责人不覆盖: {v.get(f_by_id)}")
            chk(v.get(f_od_id) == "2026-07-20", f"下单日期仍回写: {v.get(f_od_id)}")

        # ===== 2. 404 / 400 =====
        r = await c.post(f"{URL}/TH19000101-999/items", headers=Hb1,
                         json={"lines": [{"item_name": "x"}]})
        chk(r.status_code == 404, f"单不存在 404: {r.status_code}")
        r = await c.post(APP, headers=Hb1, json={"lines": [{"item_name": "  "}]})
        chk(r.status_code == 400, f"全空行 400: {r.status_code}")

        # ===== 3. 越权：b2(受限采购员) 追加 b1 的单 → 403；admin → 200 =====
        r = await c.post(APP, headers=Hb2, json={"lines": [{"item_name": "越权件"}]})
        chk(r.status_code == 403, f"他人单追加 403: {r.status_code} {r.text[:120]}")
        r = await c.post(APP, headers=H, json={"lines": [{"item_name": "管理员补件", "qty": 1}]})
        chk(r.status_code == 200, f"admin 追加放行: {r.status_code} {r.text[:120]}")

        # ===== 5. 按清单分工：lixinxin 自己的单，追加来源=外协加工 → 403；来源=标准件清单 → 200 =====
        r = await c.post(URL, headers=Hlxx, json={
            "supplier_id": sid, "delivery_date": "2026-07-21", "project_code": "T-AP1",
            "lines": [{"item_name": "李的螺丝", "qty": 2}]})
        chk(r.status_code == 200, f"李新新建单: {r.text[:150]}")
        po2 = r.json()[0]["po_no"]
        r = await c.post(f"{URL}/{po2}/items", headers=Hlxx, json={"lines": [
            {"item_name": "外协件A", "source_sheet_id": ds_o_id, "source_record_id": rec_o_id}]})
        chk(r.status_code == 403, f"外协来源 403: {r.status_code} {r.text[:120]}")
        r = await c.post(f"{URL}/{po2}/items", headers=Hlxx, json={"lines": [
            {"item_name": "内六角螺丝2", "source_sheet_id": ds_id, "source_record_id": rec_id}]})
        chk(r.status_code == 200, f"标准件来源放行: {r.status_code} {r.text[:120]}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
