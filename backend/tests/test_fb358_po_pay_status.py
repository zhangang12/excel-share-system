"""反馈#358（李新新）：「我这个是现金帐，已经是全额付款的，怎么显示部分付款？」

现场：TH20260808-019 共 13 行零件，收货合计 12.92、已付合计 12.92 —— 钱一分不差付清了，
但只有**1 行**带付款额（「整单维护」为了让汇总合计等于所填总额，故意把整单付款额记在首行、
其余置 0）。付款状态却是按行判的，于是另外 12 行都是「未付款」，
前端合并父行 every(已付款) 不成立 → 整单显示「部分付款」。
生产上 129 张已付清的采购单里，35 张这样显示错。

要锁死的：
  1. 整单付清 → 该单**每一行**都是已付款（前端父行才会显示已付款）
  2. 真·部分付款（Σ付 < Σ收）**一行都不许**被提升 —— 否则等于把「还欠钱」显示成
     「已付清」，比原来的错误严重得多：采购会漏付，供应商来对账才发现
  3. 请款单只覆盖多行里的一部分时，逐行状态要保留 —— 财务靠这个看哪几行付了
  4. 合计必须回数据库按整单算：分页/筛选时接口只返回该单的一部分，
     拿残缺的合计去判会把没付清的单标成已付款
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb358")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app.routers.purchase_mgmt_router import _attach_pay_status, _item_out
from app import models
from sqlalchemy import select

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        sup = (await c.post("/api/purchase-mgmt/suppliers", headers=H,
                            json={"name": "顺鑫机电"})).json()

        async def make_po(lines, method="现金全款"):
            r = await c.post("/api/purchase-mgmt/orders", headers=H, json={
                "supplier_id": sup["id"], "delivery_date": "2026-08-08",
                "payment_method": method, "lines": lines})
            assert r.status_code == 200, r.text
            return r.json()

        def line(name, qty, price):
            return {"item_name": name, "qty": qty, "unit_price": price,
                    "received_amount": round(qty * price, 2)}

        async def statuses(po_no):
            r = await c.get(f"/api/purchase-mgmt/orders/{po_no}", headers=H)
            assert r.status_code == 200, r.text
            return [x["pay_status"] for x in r.json()]

        # ---- 1) 复现现场：整单维护把整单付款额记在首行 ----
        rows = await make_po([line("密封圈", 2, 3.0), line("垫片", 4, 1.0), line("螺栓", 10, 0.5)])
        po = rows[0]["po_no"]
        total = sum(x["received_amount"] for x in rows)          # 6 + 4 + 5 = 15
        r = await c.post("/api/purchase-mgmt/items/set-group-summary", headers=H, json={
            "item_ids": [x["id"] for x in rows], "paid_amount": total, "paid_date": "2026-08-08"})
        chk(r.status_code == 200, f"整单维护记整单付款: {r.status_code} {r.text[:80]}")

        async with SessionLocal() as db:
            pr = (await db.execute(select(models.PurchaseItem.paid_amount)
                                   .where(models.PurchaseItem.po_no == po)
                                   .order_by(models.PurchaseItem.id))).scalars().all()
        chk(sum(1 for p in pr if (p or 0) > 0) == 1,
            f"复现前提：整单维护确实只把钱记在首行 {list(pr)}")

        st = await statuses(po)
        chk(st == ["已付款"] * 3, f"1) 整单付清 → 每行都是已付款: {st}")

        # 前端合并父行的判定：every(已付款) 才显示「已付款」
        chk(all(s == "已付款" for s in st), "1) 合并父行会显示「已付款」而不是「部分付款」")

        # ---- 2) 真·部分付款不许被提升 ----
        rows2 = await make_po([line("轴承", 1, 100.0), line("联轴器", 1, 200.0)])
        po2 = rows2[0]["po_no"]
        await c.post("/api/purchase-mgmt/items/set-group-summary", headers=H, json={
            "item_ids": [x["id"] for x in rows2], "paid_amount": 100.0})   # 只付了 100/300
        st2 = await statuses(po2)
        chk(not all(s == "已付款" for s in st2), f"2) 只付了 1/3 的单不许整单标已付款: {st2}")
        chk("已付款" not in st2[1:], f"2) 没付到的行仍不是已付款: {st2}")

        # 差一分钱也算没付清 —— 财务对账就是在抠这一分钱
        rows3 = await make_po([line("法兰", 1, 50.0), line("弯头", 1, 50.0)])
        po3 = rows3[0]["po_no"]
        await c.post("/api/purchase-mgmt/items/set-group-summary", headers=H, json={
            "item_ids": [x["id"] for x in rows3], "paid_amount": 99.99})
        st3 = await statuses(po3)
        chk(not all(s == "已付款" for s in st3), f"2) 差 1 分不算付清: {st3}")

        # 正好付清（含浮点误差）要算付清
        await c.post("/api/purchase-mgmt/items/set-group-summary", headers=H, json={
            "item_ids": [x["id"] for x in rows3], "paid_amount": 99.999})
        chk(all(s == "已付款" for s in await statuses(po3)), "1) 只差 0.001 视为付清（浮点容差）")

        # ---- 3) 逐行付款的单，行级明细要保住 ----
        rows4 = await make_po([line("电机", 1, 500.0), line("减速机", 1, 500.0)], method="账期")
        po4 = rows4[0]["po_no"]
        async with SessionLocal() as db:
            it = (await db.execute(select(models.PurchaseItem)
                                   .where(models.PurchaseItem.id == rows4[0]["id"]))).scalar_one()
            it.paid_amount = 500.0      # 只有第一行真的付了（请款单按行分摊的场景）
            await db.commit()
        st4 = await statuses(po4)
        chk(st4[0] == "已付款" and st4[1] == "未付款",
            f"3) 半单付款时逐行状态保留（哪行付了看得出来）: {st4}")

        # ---- 4) 只拿到该单的一部分时（分页/筛选）不能按残缺合计判 ----
        async with SessionLocal() as db:
            first = (await db.execute(select(models.PurchaseItem)
                                      .where(models.PurchaseItem.id == rows4[0]["id"]))).scalar_one()
            # outs 里只有已付清的那一行，整单其实还欠 500
            outs = await _attach_pay_status(db, [_item_out(first)])
        chk(outs[0].pay_status == "已付款", "4) 分页片段里本来就付清的行仍是已付款")

        async with SessionLocal() as db:
            second = (await db.execute(select(models.PurchaseItem)
                                       .where(models.PurchaseItem.id == rows4[1]["id"]))).scalar_one()
            outs = await _attach_pay_status(db, [_item_out(second)])
        chk(outs[0].pay_status != "已付款",
            f"4) 只返回未付的那行时，不会因为「这页合计=0」之类的算法把它标成已付款: {outs[0].pay_status}")

        # 整单付清的单，即使只返回其中一行（分页切开）也要显示已付款
        async with SessionLocal() as db:
            mid = (await db.execute(select(models.PurchaseItem)
                                    .where(models.PurchaseItem.po_no == po)
                                    .order_by(models.PurchaseItem.id))).scalars().all()[1]
            outs = await _attach_pay_status(db, [_item_out(mid)])
        chk(outs[0].pay_status == "已付款",
            f"4) 付清的单被分页切开，单独一行也显示已付款: {outs[0].pay_status}")

        # ---- 没付过钱的单不受影响 ----
        rows5 = await make_po([line("油封", 2, 5.0)])
        chk((await statuses(rows5[0]["po_no"]))[0] == "未付款", "没付过的单还是未付款")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
