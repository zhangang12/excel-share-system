"""反馈 #329 / #330：收货价改动的流水回写 + 待收货不再被截断/搜得到老单

#329（王芹：仓库收货后填的价格不对，采购需有更改的权限）
  - 采购 b1 下单(后填价格,单价留空) → 仓库 w1 收货填价 100 → b1 改价 200：
    应 200，明细单价/收货金额更新，且已生成的入库流水金额同步回写(盈利改善1b)
  - ★本次修的真 bug：仓库自己在「已收货」再收一次改价 300 → WhTxn.amount 也要跟着变
    （_auto_stock_in 幂等直接 return，此前流水永久停在旧价，库存与项目材料成本跟着错）
  - 合并收货按总价分摊时，流水金额跟「收货金额」走（单价是反算的，qty×单价有分位差）
  - 另一采购 b2 改同一条 → 403（行级隔离不破）

#330（王利利：待收货超过 300 就搜不到工期久的供货商和物料）
  - /receiving 默认上限从 300 放到 2000，且 keyword/item_name/order_month 下沉 SQL：
    造 320 条待收货，最老的那条(排序上被 300 截断的位置)必须能被物料名/单号搜出来
  - /receiving/meta 返回真实 count(*) 与全量供应商下拉（不受列表 limit 影响）
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb329")
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


async def _txns(iid):
    async with SessionLocal() as db:
        return (await db.execute(select(models.WhTxn).where(
            models.WhTxn.purchase_item_id == iid,
            models.WhTxn.is_reversal == False))).scalars().all()   # noqa: E712


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
        async def login(u, p):
            r = await c.post('/api/auth/login', json={'username': u, 'password': p})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login('admin', 'admin123')
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(uname, role_code):
            r = await c.post("/api/admin/users", headers=H,
                             json={"username": uname, "password": "pass123",
                                   "full_name": uname, "role_id": rid[role_code]})
            assert r.status_code == 200, r.text
            return r.json().get("id")

        await mkuser("b1", "buyer"); await mkuser("b2", "buyer"); await mkuser("w1", "warehouse")
        Hb1 = await login('b1', 'pass123')
        Hb2 = await login('b2', 'pass123')
        Hw1 = await login('w1', 'pass123')

        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb1, json={"name": "供应商甲"})
        sid = r.json()["id"]
        # 后填价格流程：下单不填单价
        r = await c.post("/api/purchase-mgmt/items", headers=Hb1,
                         json={"supplier_id": sid, "item_name": "高速轴", "qty": 2})
        chk(r.status_code in (200, 201), f"采购下单: {r.status_code} {r.text[:150]}")
        iid = r.json()["id"]

        # 仓库收货，填价 100（填错了，应为 200）
        r = await c.put(f"/api/purchase-mgmt/items/{iid}/receive", headers=Hw1,
                         json={"arrival_date": "2026-07-30", "delivery_note_no": "SH001",
                               "unit_price": 100})
        chk(r.status_code == 200, f"仓库收货: {r.status_code} {r.text[:150]}")
        chk(r.json().get("received_amount") == 200, f"收货金额=2*100=200: {r.json().get('received_amount')}")

        # 采购 b1 在收货后改价 200
        r = await c.put(f"/api/purchase-mgmt/items/{iid}", headers=Hb1,
                        json={"unit_price": 200, "received_amount": 400})
        chk(r.status_code == 200, f"收货后采购改价: {r.status_code} {r.text[:200]}")
        if r.status_code == 200:
            chk(r.json().get("unit_price") == 200, f"单价已改200: {r.json().get('unit_price')}")

        txns = await _txns(iid)
        chk(len(txns) >= 1, f"已生成入库流水: {len(txns)}")
        if txns:
            chk(txns[0].amount == 400, f"采购改价→流水金额回写400: {txns[0].amount}")

        # ★ 本次修的真 bug：仓库自己在「已收货」页签改价，流水也要跟着变
        r = await c.put(f"/api/purchase-mgmt/items/{iid}/receive", headers=Hw1,
                        json={"arrival_date": "2026-07-30", "delivery_note_no": "SH001",
                              "unit_price": 300})
        chk(r.status_code == 200, f"仓库改价: {r.status_code} {r.text[:150]}")
        txns = await _txns(iid)
        chk(len(txns) == 1, f"仓库改价不应重复过账: {len(txns)}")
        if txns:
            chk(txns[0].unit_price == 300, f"★仓库改价→流水单价回写300: {txns[0].unit_price}")
            chk(txns[0].amount == 600, f"★仓库改价→流水金额回写600: {txns[0].amount}")

        # 行级隔离：b2 改同一条 → 403
        r = await c.put(f"/api/purchase-mgmt/items/{iid}", headers=Hb2, json={"unit_price": 999})
        chk(r.status_code == 403, f"他人改价应403: {r.status_code} {r.text[:120]}")

        # 合并收货按总价分摊：流水金额跟收货金额走（不是 qty×反算单价，那样有分位差）
        mids = []
        for n, q in (("法兰A", 3), ("法兰B", 7)):
            rr = await c.post("/api/purchase-mgmt/items", headers=Hb1,
                              json={"supplier_id": sid, "item_name": n, "qty": q})
            mids.append(rr.json()["id"])
        r = await c.post("/api/purchase-mgmt/items/receive-batch", headers=Hw1,
                         json={"item_ids": mids, "arrival_date": "2026-07-30",
                               "delivery_note_no": "SH002", "total_amount": 100})
        chk(r.status_code == 200, f"合并收货: {r.status_code} {r.text[:150]}")
        got = {x["id"]: x for x in r.json()} if r.status_code == 200 else {}
        for mid in mids:
            tx = await _txns(mid)
            if tx and mid in got:
                chk(tx[0].amount == got[mid]["received_amount"],
                    f"合并收货流水金额=收货金额({mid}): {tx[0].amount} vs {got[mid]['received_amount']}")
        s = sum(t[0].amount for t in [await _txns(m) for m in mids] if t)
        chk(abs(s - 100) < 0.001, f"合并收货流水合计=总价100: {s}")

        # ==================== #330 ====================
        # 造 320 条待收货：第 1 条是「最老的长周期单」（po_no 最小 → 排序垫底，旧实现被 300 截断）
        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb1, json={"name": "长周期供应商乙"})
        old_sid = r.json()["id"]
        r = await c.post("/api/purchase-mgmt/items", headers=Hb1,
                         json={"supplier_id": old_sid, "item_name": "进口伺服减速机", "qty": 1,
                               "project_code": "2026-001", "delivery_date": "2026-01-05"})
        chk(r.status_code in (200, 201), f"老单下单: {r.status_code} {r.text[:150]}")
        old_iid = r.json()["id"]
        new_ids = []
        for i in range(320):
            rr = await c.post("/api/purchase-mgmt/items", headers=Hb1,
                              json={"supplier_id": sid, "item_name": f"新件{i}", "qty": 1,
                                    "delivery_date": "2026-07-30"})
            new_ids.append(rr.json()["id"])
        # po_no 由采购单流程生成、不在 Create/Update schema 里，这里直接落库补号，
        # 复刻生产排序（po_no 倒序 → 老单垫底，正是旧实现被 limit(300) 丢掉的那批）
        async with SessionLocal() as db:
            o = (await db.execute(select(models.PurchaseItem)
                                  .where(models.PurchaseItem.id == old_iid))).scalar_one()
            o.po_no = "TH20260101-001"
            for idx, nid in enumerate(new_ids):
                it = (await db.execute(select(models.PurchaseItem)
                                       .where(models.PurchaseItem.id == nid))).scalar_one()
                it.po_no = f"TH20260730-{idx:03d}"
            await db.commit()

        r = await c.get("/api/purchase-mgmt/receiving", headers=Hw1, params={"received": False})
        chk(r.status_code == 200, f"待收货列表: {r.status_code} {r.text[:150]}")
        rows = r.json() if r.status_code == 200 else []
        chk(len(rows) > 300, f"★不再被 300 截断: {len(rows)}")
        chk(any(x["id"] == old_iid for x in rows), "★最老的长周期单出现在列表里")

        # 物料名 / 单号 / 项目编号 三个框都得能把老单搜出来（后端过滤，穿透 limit）
        r = await c.get("/api/purchase-mgmt/receiving", headers=Hw1,
                        params={"received": False, "item_name": "伺服减速机"})
        chk([x["id"] for x in r.json()] == [old_iid], f"★按物料名搜到老单: {r.json() and len(r.json())}")
        r = await c.get("/api/purchase-mgmt/receiving", headers=Hw1,
                        params={"received": False, "keyword": "TH20260101"})
        chk([x["id"] for x in r.json()] == [old_iid], f"★按采购单号搜到老单: {len(r.json())}")
        r = await c.get("/api/purchase-mgmt/receiving", headers=Hw1,
                        params={"received": False, "keyword": "2026-001"})
        chk([x["id"] for x in r.json()] == [old_iid], f"★按项目编号搜到老单: {len(r.json())}")
        r = await c.get("/api/purchase-mgmt/receiving", headers=Hw1,
                        params={"received": False, "order_month": "2026-01"})
        chk([x["id"] for x in r.json()] == [old_iid], f"★按下单月份搜到老单: {len(r.json())}")

        # #253 那批：明细自己 project_code 为空，页面上的项目编号是从来源清单回溯出来的，
        # 关键字下沉 SQL 后也必须搜得到（否则相对 #315 的前端过滤是回归）
        async with SessionLocal() as db:
            p = models.Project(code="2026-999", name="回溯项目")
            db.add(p); await db.flush()
            ds = models.Datasheet(project_id=p.id, name="外协件")
            db.add(ds); await db.flush()
            it = (await db.execute(select(models.PurchaseItem)
                                   .where(models.PurchaseItem.id == new_ids[0]))).scalar_one()
            it.source_sheet_id = ds.id
            it.project_code = None
            sheet_iid = it.id
            await db.commit()
        r = await c.get("/api/purchase-mgmt/receiving", headers=Hw1,
                        params={"received": False, "keyword": "2026-999"})
        chk([x["id"] for x in r.json()] == [sheet_iid], f"★回溯项目编号也搜得到: {len(r.json())}")

        # meta：真实条数 + 全量供应商（长周期供应商必须在下拉里）
        r = await c.get("/api/purchase-mgmt/receiving/meta", headers=Hw1)
        chk(r.status_code == 200, f"meta: {r.status_code} {r.text[:150]}")
        meta = r.json() if r.status_code == 200 else {}
        chk(meta.get("pending_count") == 321, f"★待收货真实条数=321: {meta.get('pending_count')}")
        chk(meta.get("received_count") == 3, f"已收货真实条数=3: {meta.get('received_count')}")
        sup_ids = [s["id"] for s in meta.get("pending_suppliers", [])]
        chk(old_sid in sup_ids, "★长周期供应商在待收货下拉里")
        chk(sid in [s["id"] for s in meta.get("received_suppliers", [])], "已收货下拉含供应商甲")

        # limit 参数仍可显式收窄（旧客户端不传即取默认 2000）
        r = await c.get("/api/purchase-mgmt/receiving", headers=Hw1,
                        params={"received": False, "limit": 10})
        chk(len(r.json()) == 10, f"limit 显式生效: {len(r.json())}")

        # 权限：采购角色不能读收货清单/meta（收货归仓库）
        r = await c.get("/api/purchase-mgmt/receiving/meta", headers=Hb1)
        chk(r.status_code == 403, f"采购读 meta 应403: {r.status_code}")

    print("PASSED" if not FAIL else f"FAILED {len(FAIL)}")

asyncio.run(main())
