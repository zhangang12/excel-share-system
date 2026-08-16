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
        Hbl = await login("bl")   # 🆕 #378 之后改期只有采购主管/管理层能做
        adm = (await c.get("/api/auth/me", headers=H)).json()["id"]

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
            # 🆕 #378 后三行都从**空**开始：批量改期的主流程验的是"首次维护"。
            #   已填过的行由普通采购再改会被 #378 的锁拦下（本文件末尾单独验），
            #   在这里预填等于让主流程一开始就撞锁，#297 的覆盖全丢。
            it_a = models.PurchaseItem(supplier_id=sid, item_name="零件甲", buyer_id=b1,
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
            # 三行都是空→填，changed=3
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

        # ===== 3. 改期留痕通知 =====
        # 🆕 #378 之后**首次填不推**：留痕针对的是"改期消音"，第一次填是正常下单动作。
        #   历史数据 195 条通知里 84 条是「由 未填 改为」（43% 纯噪音），且 #378 之后
        #   普通采购能做的只剩首次填 —— 不滤掉等于每建一条明细就 ping 一次主管+管理层。
        async with SessionLocal() as db:
            first = list((await db.execute(select(models.Message).where(
                models.Message.biz_type == "po_expected_changed",
                models.Message.biz_id == a))).scalars().all())
            chk(len(first) == 0, f"首次填**不**推留痕通知: {[m.text for m in first]}")

        # 真正改期（由主管操作，普通采购已被 #378 拦住）才推
        r = await c.put("/api/purchase-mgmt/items/batch-expected-arrival", headers=Hbl,
                        json={"ids": [a], "expected_arrival": "2026-09-05"})
        chk(r.status_code == 200, f"主管改期: {r.status_code} {r.text[:120]}")
        async with SessionLocal() as db:
            msgs = list((await db.execute(select(models.Message).where(
                models.Message.to_user_id == adm,
                models.Message.biz_type == "po_expected_changed",
                models.Message.biz_id == a))).scalars().all())
            chk(len(msgs) == 1 and "2026-09-01" in msgs[0].text and "2026-09-05" in msgs[0].text,
                f"真正改期才推留痕，且写明由哪天改到哪天: {[m.text for m in msgs]}")
            own = list((await db.execute(select(models.Message).where(
                models.Message.to_user_id == bl,
                models.Message.biz_type == "po_expected_changed",
                models.Message.biz_id == a))).scalars().all())
            chk(len(own) == 0, "操作人本人（本次是主管）不收自己的改期通知")
        # 复位，后面几段仍按 2026-09-01 断言
        r = await c.put("/api/purchase-mgmt/items/batch-expected-arrival", headers=Hbl,
                        json={"ids": [a], "expected_arrival": "2026-09-01"})
        chk(r.status_code == 200, f"复位到 2026-09-01: {r.status_code}")

        # ===== 5. 值未变的行不算 changed =====
        r = await c.put("/api/purchase-mgmt/items/batch-expected-arrival", headers=Hb1,
                        json={"ids": [a, c_], "expected_arrival": "2026-09-01"})
        chk(r.status_code == 200 and r.json().get("updated") == 2 and r.json().get("changed") == 0,
            f"重复同值批量改 changed=0: {r.text[:150]}")

        # ===== 🆕 #378：填过之后普通采购不能再改（改期/清空都算改） =====
        r = await c.put("/api/purchase-mgmt/items/batch-expected-arrival", headers=Hb1,
                        json={"ids": [a], "expected_arrival": "2026-10-01"})
        chk(r.status_code == 403 and "只能维护一次" in r.text,
            f"#378 普通采购改已填过的预计到货被拦（且拦的是这把锁）: {r.status_code} {r.text[:110]}")
        async with SessionLocal() as db:
            still = (await db.execute(select(models.PurchaseItem).where(
                models.PurchaseItem.id == a))).scalar_one()
            chk(still.expected_arrival == "2026-09-01",
                f"#378 被拦下后日期没动: {still.expected_arrival}")

        # ===== 4. null = 批量清空（详单单元格同步清空） =====
        # ⚠️ #378 之后清空也算"改"，普通采购做不了，这里换管理层操作
        r = await c.put("/api/purchase-mgmt/items/batch-expected-arrival", headers=H,
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
