"""🆕 反馈#297 采购明细「批量修改预计到货」回归测试：
1. PUT /api/purchase-mgmt/items/batch-expected-arrival 多行一次更新（含散单）；
2. 来源于项目清单的明细同步回写详单「预计到货」列（复用 _writeback_sheet_row，缺列自动补建）；
3. 改期留痕通知照常推送给采购主管/管理层（与单条编辑一致），操作人本人不收；
4. expected_arrival 传 null → 批量清空（详单单元格同步清空）；
5. 空 ids → 400；全部 id 不存在 → 404；值未变的行不算 changed。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="batchea")
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

        async def mk(u, rc):
            r = await c.post("/api/admin/users", headers=H,
                             json={"username": u, "password": "pass123", "full_name": u, "role_id": rid[rc]})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        async def login(u):
            return {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': u, 'password': 'pass123'})).json()['access_token']}"}

        b1 = await mk("b1", "buyer")
        bl = await mk("bl", "buyer_lead")
        Hb1 = await login("b1")

        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb1, json={"name": "批量改期供应商"})
        chk(r.status_code == 200, f"建供应商: {r.text[:120]}")
        sid = r.json()["id"]

        # 项目清单（标准件清单，故意不建「预计到货」列，验证缺列自动补建）+ 两行记录
        async with SessionLocal() as db:
            proj = models.Project(code="T-BEA", name="批量改期测试项目")
            db.add(proj); await db.flush()
            ds = models.Datasheet(project_id=proj.id, name="标准件清单")
            db.add(ds); await db.flush()
            f1 = models.Field(datasheet_id=ds.id, name="项目", type="text", sort_order=1)
            db.add(f1); await db.flush()
            rec_a = models.Record(datasheet_id=ds.id, values={str(f1.id): "零件甲"})
            rec_b = models.Record(datasheet_id=ds.id, values={str(f1.id): "零件乙"})
            db.add_all([rec_a, rec_b]); await db.flush()
            it_a = models.PurchaseItem(supplier_id=sid, item_name="零件甲", buyer_id=b1,
                                       expected_arrival="2026-08-01",
                                       source_sheet_id=ds.id, source_record_id=rec_a.id)
            it_b = models.PurchaseItem(supplier_id=sid, item_name="零件乙", buyer_id=b1,
                                       source_sheet_id=ds.id, source_record_id=rec_b.id)
            it_c = models.PurchaseItem(supplier_id=sid, item_name="散单件", buyer_id=b1)   # 无来源清单
            db.add_all([it_a, it_b, it_c]); await db.commit()
            ds_id, ra_id, rb_id = ds.id, rec_a.id, rec_b.id
            a, b_, c_ = it_a.id, it_b.id, it_c.id

        # ===== 1+2. 批量改期：多行更新 + 详单回写（缺列自动补建） =====
        r = await c.put("/api/purchase-mgmt/items/batch-expected-arrival", headers=Hb1,
                        json={"ids": [a, b_, c_], "expected_arrival": "2026-09-01"})
        chk(r.status_code == 200, f"批量改期 200: {r.text[:200]}")
        if r.status_code == 200:
            chk(r.json().get("updated") == 3, f"updated=3: {r.json()}")
            # it_a 由 2026-08-01 改、it_b 由空改 → changed=2；it_c 也是空→改，共 3
            chk(r.json().get("changed") == 3, f"changed=3: {r.json()}")
        async with SessionLocal() as db:
            its = {i.id: i for i in (await db.execute(select(models.PurchaseItem).where(
                models.PurchaseItem.id.in_([a, b_, c_])))).scalars().all()}
            chk(all(i.expected_arrival == "2026-09-01" for i in its.values()),
                f"三行预计到货均已改: {[i.expected_arrival for i in its.values()]}")
            flds = {f.name: str(f.id) for f in (await db.execute(
                select(models.Field).where(models.Field.datasheet_id == ds_id))).scalars().all()}
            ea_fid = flds.get("预计到货")
            chk(ea_fid is not None, "清单缺列时已自动补建「预计到货」字段")
            va = (await db.execute(select(models.Record).where(models.Record.id == ra_id))).scalar_one().values
            vb = (await db.execute(select(models.Record).where(models.Record.id == rb_id))).scalar_one().values
            chk(va.get(ea_fid) == "2026-09-01", f"零件甲行详单已回写: {va.get(ea_fid)!r}")
            chk(vb.get(ea_fid) == "2026-09-01", f"零件乙行详单已回写: {vb.get(ea_fid)!r}")

        # ===== 3. 改期留痕通知（与单条编辑同口径：推主管/管理层，排除操作人） =====
        async with SessionLocal() as db:
            msgs = list((await db.execute(select(models.Message).where(
                models.Message.to_user_id == bl,
                models.Message.biz_type == "po_expected_changed",
                models.Message.biz_id == a))).scalars().all())
            chk(len(msgs) == 1 and "2026-09-01" in msgs[0].text,
                f"采购主管收到改期留痕: {[m.text for m in msgs]}")
            own = list((await db.execute(select(models.Message).where(
                models.Message.to_user_id == b1,
                models.Message.biz_type == "po_expected_changed",
                models.Message.biz_id == a))).scalars().all())
            chk(len(own) == 0, "操作人本人不收改期通知")

        # ===== 5. 值未变的行不算 changed =====
        r = await c.put("/api/purchase-mgmt/items/batch-expected-arrival", headers=Hb1,
                        json={"ids": [a, c_], "expected_arrival": "2026-09-01"})
        chk(r.status_code == 200 and r.json().get("updated") == 2 and r.json().get("changed") == 0,
            f"重复同值批量改 changed=0: {r.text[:150]}")

        # ===== 4. null = 批量清空（详单单元格同步清空） =====
        r = await c.put("/api/purchase-mgmt/items/batch-expected-arrival", headers=Hb1,
                        json={"ids": [a, b_], "expected_arrival": None})
        chk(r.status_code == 200 and r.json().get("changed") == 2, f"批量清空: {r.text[:150]}")
        async with SessionLocal() as db:
            its = {i.id: i for i in (await db.execute(select(models.PurchaseItem).where(
                models.PurchaseItem.id.in_([a, b_, c_])))).scalars().all()}
            chk(its[a].expected_arrival is None and its[b_].expected_arrival is None,
                f"两行已清空: {its[a].expected_arrival!r}/{its[b_].expected_arrival!r}")
            chk(its[c_].expected_arrival == "2026-09-01", "未勾选的散单行不受影响")
            flds = {f.name: str(f.id) for f in (await db.execute(
                select(models.Field).where(models.Field.datasheet_id == ds_id))).scalars().all()}
            ea_fid = flds.get("预计到货")
            va = (await db.execute(select(models.Record).where(models.Record.id == ra_id))).scalar_one().values
            chk(va.get(ea_fid) == "", f"清空后详单单元格同步清空: {va.get(ea_fid)!r}")

        # ===== 5b. 参数校验：空 ids → 400；id 全不存在 → 404 =====
        r = await c.put("/api/purchase-mgmt/items/batch-expected-arrival", headers=Hb1,
                        json={"ids": [], "expected_arrival": "2026-09-01"})
        chk(r.status_code == 400, f"空 ids 400: {r.status_code}")
        r = await c.put("/api/purchase-mgmt/items/batch-expected-arrival", headers=Hb1,
                        json={"ids": [999991, 999992], "expected_arrival": "2026-09-01"})
        chk(r.status_code == 404, f"明细不存在 404: {r.status_code}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
