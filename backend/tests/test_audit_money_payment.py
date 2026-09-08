"""金额审计(2026-09-09)·采购→收货→请款→付款 这条链上的钱不能算错。

背景：老板要求「把系统中所有涉及金额的数据进行一遍审计，确保计算逻辑完全正确」。
代码审查 + 生产库对账发现这条链上的真问题：
  · 付款金额不校验（0 元/超请款额都能「已付款」），付款/删除没有并发守卫
  · 请款金额与明细分配完全脱钩（Σ分配≠请款额、分配可超余额、可跨供应商、可空明细）
  · PurchaseItem.paid_amount 有三个互不知情的写入口（请款付款累加 / 整单维护覆盖 / 编辑明细），
    删除已付请款单反冲时 max(0,…) 静默截断；生产 TH20260724-025 收货 15 已付 30
  · 未定价收货的入库流水金额写成 0.0 而不是 NULL（生产 210 条，34 个物料加权均价被稀释）
  · 采购改数量不同步到入库流水（生产 #1350：采购 6 件、仓库 10 件）
  · 冲红过的明细在 PG 下删不掉（冲红对仍以 FK 指向明细）
本文件把这些口径全部锁死。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="auditpay")
# AUDIT_TEST_DATABASE_URL 指向一个**空的** Postgres 库时在真 PG 上跑（CLAUDE.md：涉及 SQL 的改动必须在真 PG 验证）
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


async def _item(iid):
    async with SessionLocal() as db:
        return (await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.id == iid))).scalar_one()


async def _txns(iid, valid_only=False):
    async with SessionLocal() as db:
        q = select(models.WhTxn).where(models.WhTxn.purchase_item_id == iid)
        if valid_only:
            q = q.where(models.WhTxn.is_reversal == False, models.WhTxn.reversed == False)  # noqa: E712
        return list((await db.execute(q.order_by(models.WhTxn.id))).scalars().all())


async def _pr(prid):
    async with SessionLocal() as db:
        return (await db.execute(select(models.PaymentRequest).where(models.PaymentRequest.id == prid))).scalar_one_or_none()


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

        await mkuser("pa_buyer", ["buyer"])
        await mkuser("pa_lead", ["finance", "finance_lead"])
        await mkuser("pa_cash", ["finance"])
        await mkuser("pa_wh", ["warehouse"])
        Hb, Hl, Hc, Hw = (await login("pa_buyer"), await login("pa_lead"),
                          await login("pa_cash"), await login("pa_wh"))

        sid = (await c.post("/api/purchase-mgmt/suppliers", headers=Hb, json={"name": "审计供应商A"})).json()["id"]
        sid2 = (await c.post("/api/purchase-mgmt/suppliers", headers=Hb, json={"name": "审计供应商B"})).json()["id"]

        async def order(lines, supplier=sid, **hdr):
            r = await c.post("/api/purchase-mgmt/orders", headers=Hb,
                             json={"supplier_id": supplier, "lines": lines, **hdr})
            assert r.status_code == 200, r.text
            return r.json()

        async def receive(iid, **extra):
            r = await c.put(f"/api/purchase-mgmt/items/{iid}/receive", headers=Hw,
                            json={"arrival_date": "2026-09-01", **extra})
            assert r.status_code == 200, r.text
            return r.json()

        async def new_pr(items, amount, supplier=sid):
            return await c.post("/api/purchase-mgmt/payment-requests", headers=Hb,
                                json={"supplier_id": supplier, "requested_amount": amount, "items": items})

        async def pay(prid, amount, hdr=None):
            return await c.put(f"/api/purchase-mgmt/payment-requests/{prid}/pay", headers=hdr or Hc,
                               data={"paid_amount": amount, "paid_date": "2026-09-05"})

        # ══════════════ ① 请款单：钱必须和明细对得上 ══════════════
        print("\n① 请款单校验")
        a = await order([{"item_name": "轴承", "qty": 10, "unit_price": 100},
                         {"item_name": "垫片", "qty": 5, "unit_price": 20}])
        a1, a2 = a[0]["id"], a[1]["id"]
        chk(a[0]["received_amount"] == 1000 and a[1]["received_amount"] == 100, "下单即有收货金额(订单金额) 1000/100")

        r = await new_pr([], 100)
        chk(r.status_code == 400 and "至少" in r.text, f"空明细请款单被拒 -> {r.status_code}")
        r = await new_pr([{"item_id": a1, "allocated_amount": 1000}], 1200)
        chk(r.status_code == 400 and "不一致" in r.text, f"Σ分配 1000 ≠ 请款 1200 被拒 -> {r.status_code}")
        r = await new_pr([{"item_id": a1, "allocated_amount": 1500}], 1500)
        chk(r.status_code == 400 and "超出" in r.text, f"分配 1500 > 收货 1000 被拒 -> {r.status_code}")
        b = await order([{"item_name": "B家的料", "qty": 1, "unit_price": 50}], supplier=sid2)
        r = await new_pr([{"item_id": b[0]["id"], "allocated_amount": 50}], 50, supplier=sid)
        chk(r.status_code == 400 and "供应商" in r.text, f"明细属于另一家供应商被拒 -> {r.status_code}")
        r = await new_pr([{"item_id": a1, "allocated_amount": -5}], 5)
        chk(r.status_code == 422, f"负数分配被 schema 挡下 -> {r.status_code}")
        r = await new_pr([{"item_id": a1, "allocated_amount": 0}], 0)
        chk(r.status_code == 422, f"0 元请款被 schema 挡下 -> {r.status_code}")

        r = await new_pr([{"item_id": a1, "allocated_amount": 1000}, {"item_id": a2, "allocated_amount": 100}], 1100)
        chk(r.status_code == 200, f"正常请款 1100 -> {r.status_code} {r.text[:80]}")
        pr1 = r.json()["id"]

        # ══════════════ ② 付款：0 < 实付 ≤ 请款额；回写精确到分 ══════════════
        print("\n② 付款校验与回写")
        r = await pay(pr1, 1100)
        chk(r.status_code == 400, f"未审批不能付款 -> {r.status_code}")
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{pr1}/approve", headers=Hl)
        chk(r.status_code == 200, f"财务主管审批 -> {r.status_code}")
        r = await pay(pr1, 0)
        chk(r.status_code == 400 and "大于 0" in r.text, f"付 0 元被拒 -> {r.status_code}")
        r = await pay(pr1, 1200)
        chk(r.status_code == 400 and "超过请款金额" in r.text, f"付 1200 > 请款 1100 被拒 -> {r.status_code}")
        chk((await _pr(pr1)).status == "approved", "被拒后状态仍是 approved（没有半截落库）")
        r = await pay(pr1, 1100, hdr=Hl)
        chk(r.status_code == 400 and "职责分离" in r.text, "审批人自己付款仍被拦（既有口径没坏）")
        r = await pay(pr1, 1100)
        chk(r.status_code == 200, f"出纳付款 1100 -> {r.status_code} {r.text[:80]}")
        i1, i2 = await _item(a1), await _item(a2)
        chk(abs(i1.paid_amount - 1000) < 1e-9 and abs(i2.paid_amount - 100) < 1e-9,
            f"明细已付按分配回写 1000/100 -> {i1.paid_amount}/{i2.paid_amount}")
        r = await pay(pr1, 1100)
        chk(r.status_code == 400, f"同一张再付一次被拒（不重复累加）-> {r.status_code}")
        chk(abs((await _item(a1)).paid_amount - 1000) < 1e-9, "重复付款没有把已付加成 2000")

        # 分摊精度：33.33 / 66.67 → Σ 正好 = 100.00
        d = await order([{"item_name": "精度一", "qty": 1, "unit_price": 100},
                         {"item_name": "精度二", "qty": 1, "unit_price": 100}])
        r = await new_pr([{"item_id": d[0]["id"], "allocated_amount": 33.33},
                          {"item_id": d[1]["id"], "allocated_amount": 66.67}], 100)
        pr_d = r.json()["id"]
        await c.put(f"/api/purchase-mgmt/payment-requests/{pr_d}/approve", headers=Hl)
        r = await pay(pr_d, 100)
        s = (await _item(d[0]["id"])).paid_amount + (await _item(d[1]["id"])).paid_amount
        chk(r.status_code == 200 and abs(s - 100.0) < 1e-9 and (await _item(d[0]["id"])).paid_amount == 33.33,
            f"按比例分摊 2 位小数、末行兜余，Σ精确=100 -> {s}")

        # ══════════════ ③ 删除已付请款单：反冲精确回到原值；对不上就拒绝 ══════════════
        print("\n③ 删除已付请款单反冲")
        g = await order([{"item_name": "反冲件", "qty": 1, "unit_price": 100}])
        gid = g[0]["id"]
        pr_g = (await new_pr([{"item_id": gid, "allocated_amount": 100}], 100)).json()["id"]
        await c.put(f"/api/purchase-mgmt/payment-requests/{pr_g}/approve", headers=Hl)
        await pay(pr_g, 100)
        chk(abs((await _item(gid)).paid_amount - 100) < 1e-9, "付款后明细已付 100")
        # 有人用编辑明细把已付改小 → 反冲会变负 → 拒绝而不是静默截 0
        r = await c.put(f"/api/purchase-mgmt/items/{gid}", headers=Hb, json={"paid_amount": 40})
        chk(r.status_code == 200, f"编辑明细把已付改成 40 -> {r.status_code}")
        r = await c.delete(f"/api/purchase-mgmt/payment-requests/{pr_g}", headers=Hc)
        chk(r.status_code == 400 and "无法删除" in r.text, f"反冲会变负 → 拒绝删除（原来 max(0) 静默吞掉）-> {r.status_code}")
        chk((await _pr(pr_g)) is not None and abs((await _item(gid)).paid_amount - 40) < 1e-9, "拒绝后请款单与明细都没动")
        await c.put(f"/api/purchase-mgmt/items/{gid}", headers=Hb, json={"paid_amount": 100})
        r = await c.delete(f"/api/purchase-mgmt/payment-requests/{pr_g}", headers=Hc)
        chk(r.status_code == 200 and (await _pr(pr_g)) is None, f"对得上就能删 -> {r.status_code}")
        chk(abs((await _item(gid)).paid_amount) < 1e-9, f"删除后明细已付精确回到 0 -> {(await _item(gid)).paid_amount}")

        # ══════════════ ④ 采购单级守卫：整单维护 + 请款付款 不能把同一笔钱记两遍 ══════════════
        print("\n④ 采购单级 Σ已付 ≤ Σ收货")
        f = await order([{"item_name": "方钢", "qty": 4, "unit_price": 3}, {"item_name": "圆钢", "qty": 1, "unit_price": 3}])
        f1, f2 = f[0]["id"], f[1]["id"]
        pr_f = (await new_pr([{"item_id": f1, "allocated_amount": 12}, {"item_id": f2, "allocated_amount": 3}], 15)).json()["id"]
        await c.put(f"/api/purchase-mgmt/payment-requests/{pr_f}/approve", headers=Hl)
        r = await c.post("/api/purchase-mgmt/items/set-group-summary", headers=Hb,
                         json={"item_ids": [f1, f2], "paid_amount": 30})
        chk(r.status_code == 400 and "超过" in r.text, f"整单维护已付 30 > 收货 15 被拒 -> {r.status_code}")
        r = await c.post("/api/purchase-mgmt/items/set-group-summary", headers=Hb,
                         json={"item_ids": [f1, f2], "invoice_status": "乱写"})
        chk(r.status_code == 400, f"对账状态白名单 -> {r.status_code}")
        r = await c.post("/api/purchase-mgmt/items/set-group-summary", headers=Hb,
                         json={"item_ids": [f1, f2], "paid_amount": 15, "paid_date": "2026-09-03"})
        chk(r.status_code == 200 and (await _item(f1)).paid_amount == 15 and (await _item(f2)).paid_amount == 0,
            "整单维护 15 记在首行、其余 0（既有口径）")
        r = await pay(pr_f, 15)
        chk(r.status_code == 400 and "疑似重复付款" in r.text,
            f"再走请款付款 → 整单已付 30 > 收货 15，拦下（生产 TH20260724-025 就是这么多付的）-> {r.status_code}")
        chk((await _pr(pr_f)).status == "approved" and (await _item(f1)).paid_amount == 15,
            "拦下后请款单仍 approved、明细已付未变（事务回滚）")

        # ══════════════ ⑤ 编辑明细的三道闸 ══════════════
        print("\n⑤ 编辑明细")
        r = await c.put(f"/api/purchase-mgmt/items/{a1}", headers=Hb, json={"paid_amount": 2000})
        chk(r.status_code == 400 and "超过收货金额" in r.text, f"手填已付 2000 > 收货 1000 被拒 -> {r.status_code}")
        r = await c.put(f"/api/purchase-mgmt/items/{a1}", headers=Hb, json={"supplier_id": sid2})
        chk(r.status_code == 400 and "供应商" in r.text, f"已有请款/付款的明细不能改供应商 -> {r.status_code}")
        # JSON 标准里没有 NaN，但 Python json.loads 接受 NaN 字面量，所以要手写请求体才能模拟这种脏输入
        r = await c.put(f"/api/purchase-mgmt/items/{a1}", headers={**Hb, "Content-Type": "application/json"},
                        content='{"unit_price": NaN}')
        chk(r.status_code == 422, f"NaN 单价被 schema 挡下（原来落库后所有 SUM 变 NaN → 报表 500）-> {r.status_code}")

        # ══════════════ ⑥ 未定价收货：流水金额 NULL 不是 0；补价/改数量同步 ══════════════
        print("\n⑥ 收货流水与采购明细同步")
        cc = await order([{"item_name": "未定价料", "qty": 4}])
        cid = cc[0]["id"]
        chk(cc[0]["received_amount"] == 0 and cc[0]["unit_price"] is None, "下单没填价：收货金额 0、单价空")
        await receive(cid)
        tx = await _txns(cid, valid_only=True)
        chk(len(tx) == 1 and tx[0].amount is None and tx[0].unit_price is None,
            f"未定价收货 → 流水 amount=NULL（原来写 0.0 稀释加权均价）-> {tx and tx[0].amount}")
        r = await c.put(f"/api/purchase-mgmt/items/{cid}", headers=Hb, json={"unit_price": 2.5})
        tx = await _txns(cid, valid_only=True)
        chk(r.status_code == 200 and abs((await _item(cid)).received_amount - 10) < 1e-9 and abs(tx[0].amount - 10) < 1e-9,
            f"补价 2.5 → 收货金额 10、流水金额 10 -> {tx[0].amount}")
        r = await c.put(f"/api/purchase-mgmt/items/{cid}", headers=Hb, json={"qty": 3})
        tx = await _txns(cid, valid_only=True)
        chk(r.status_code == 200 and tx[0].qty == 3 and abs(tx[0].amount - 7.5) < 1e-9,
            f"改数量 4→3 → 流水数量/金额同步 3/7.5（生产 #1350 采购 6 仓库 10 就是没同步）-> {tx[0].qty}/{tx[0].amount}")
        # 领走 2 个再想改到 1 → 库存会负 → 拦
        mid = tx[0].material_id
        r = await c.post("/api/wh/txns", headers=Hw, json={
            "material_id": mid, "biz_date": "2026-09-02", "direction": "out", "qty": 2,
            "non_project": True, "non_project_reason": "审计测试领用"})
        chk(r.status_code == 200, f"仓库领走 2 个 -> {r.status_code} {r.text[:80]}")
        r = await c.put(f"/api/purchase-mgmt/items/{cid}", headers=Hb, json={"qty": 1})
        chk(r.status_code == 400 and "已领用" in r.text, f"改到 1 会让库存变负 → 拦 -> {r.status_code}")
        r = await c.put(f"/api/purchase-mgmt/items/{cid}", headers=Hb, json={"qty": 2})
        chk(r.status_code == 200 and (await _txns(cid, valid_only=True))[0].qty == 2, "改到 2（刚好领完）允许")

        # ══════════════ ⑦ 冲红收货 = 撤销收货；冲红后能删明细 ══════════════
        print("\n⑦ 冲红与删除")
        e = await order([{"item_name": "冲红件", "qty": 2, "unit_price": 5}])
        eid = e[0]["id"]
        await receive(eid)
        tx = await _txns(eid, valid_only=True)
        r = await c.post(f"/api/wh/txns/{tx[0].id}/reverse", headers=Hw)
        chk(r.status_code == 200 and "待收货" in r.text, f"冲红采购入库单 -> {r.status_code} {r.text[:80]}")
        chk((await _item(eid)).arrival_date is None, "冲红后明细退回待收货（arrival_date 清空）")
        chk(len(await _txns(eid, valid_only=True)) == 0 and len(await _txns(eid)) == 2, "有效流水 0 张，冲红对 2 张都挂着明细")
        await receive(eid)
        chk(len(await _txns(eid, valid_only=True)) == 1, "重新收货生成新的有效入库单")
        r = await c.delete(f"/api/purchase-mgmt/items/{eid}", headers=Hb)
        chk(r.status_code == 400 and "冲红" in r.text, f"有有效入库单不能删 -> {r.status_code}")
        tx = await _txns(eid, valid_only=True)
        await c.post(f"/api/wh/txns/{tx[0].id}/reverse", headers=Hw)
        r = await c.delete(f"/api/purchase-mgmt/items/{eid}", headers=Hb)
        chk(r.status_code == 200, f"全部冲红后可删（PG 下原来撞 FK 400）-> {r.status_code} {r.text[:80]}")
        async with SessionLocal() as db:
            left = list((await db.execute(select(models.WhTxn).where(models.WhTxn.purchase_item_id == eid))).scalars().all())
            n_rows = (await db.execute(select(models.WhTxn).where(models.WhTxn.party == "审计供应商A"))).scalars().all()
        chk(len(left) == 0 and len(n_rows) >= 2, "流水保留（审计追溯），只解除对已删明细的关联")
        # 已付款的明细不能删
        r = await c.delete(f"/api/purchase-mgmt/items/{f1}", headers=Hb)
        chk(r.status_code == 400, f"已记付款/已请款的明细不能删 -> {r.status_code}")

        # ══════════════ ⑧ 批量开票默认金额 & 重提防重 ══════════════
        print("\n⑧ 开票与重提")
        h = await order([{"item_name": "开票件", "qty": 2, "unit_price": 25}])
        hid = h[0]["id"]
        await receive(hid)
        r = await c.post("/api/purchase-mgmt/items/batch-invoice", headers=Hb, json={"item_ids": [hid], "invoice_date": "2026-09-04"})
        it = await _item(hid)
        chk(r.status_code == 200 and it.invoice_status == "已开票" and abs(it.invoice_amount - 50) < 1e-9,
            f"批量开票不给金额 → 开票金额默认=收货金额 50（原来停在 0）-> {it.invoice_amount}")

        k = await order([{"item_name": "重提件", "qty": 1, "unit_price": 80}])
        kid = k[0]["id"]
        pr_x = (await new_pr([{"item_id": kid, "allocated_amount": 80}], 80)).json()["id"]
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{pr_x}/reject", headers=Hl, json={"reason": "发票未到"})
        chk(r.status_code == 200, f"驳回 X -> {r.status_code}")
        r = await new_pr([{"item_id": kid, "allocated_amount": 80}], 80)
        chk(r.status_code == 200, f"X 驳回后可另开 Y -> {r.status_code}")
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{pr_x}/resubmit", headers=Hb)
        chk(r.status_code == 400 and ("未完成" in r.text or "超出" in r.text),
            f"Y 在途时重提 X 被拒（原来两张同时待审→都付款→已付翻倍）-> {r.status_code}")

    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败")
        sys.exit(1)
    print("✅ 全部通过")


asyncio.run(main())
