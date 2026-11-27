"""#468 采购单批量对账、逐单维护发票的接口回归。可通过 FB468_TEST_DATABASE_URL 指向空的测试库。"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sandbox = tempfile.TemporaryDirectory(prefix="fb468-")
os.environ["DATABASE_URL"] = os.getenv("FB468_TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{sandbox.name}/test.db"
os.environ["FILES_DIR"] = f"{sandbox.name}/files"
os.chdir(Path(__file__).resolve().parents[1])
sys.path.insert(0, os.getcwd())

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app import models
from app.auth import create_access_token
from app.database import Base, SessionLocal, engine
from app.main import app
from app.seed import seed

BASE = "/api/purchase-mgmt/items/"
PI = models.PurchaseItem
DATE = "2026-11-28"


async def fixtures():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as db:
        await seed(db)
        roles = {role.code: role.id for role in (await db.scalars(select(models.Role))).all()}
        prefix = f"468-{uuid4().hex[:7]}"
        users = [models.User(username=f"{prefix}-{name}", role_id=roles[role], password_hash="test")
                 for name, role in (("采购", "buyer"), ("仓库", "warehouse"), ("主管", "buyer_lead"))]
        suppliers = [models.Supplier(name=f"{prefix}-{name}") for name in ("甲厂", "乙厂")]
        db.add_all(users + suppliers)
        await db.flush()
        buyer, warehouse, lead = users
        db.add(models.UserRole(user_id=buyer.id, role_id=roles["warehouse"]))
        first, second = suppliers

        def item(name, po_no, supplier, amount, **kwargs):
            return PI(item_name=name, po_no=po_no, supplier_id=supplier.id, buyer_id=buyer.id,
                      arrival_date=DATE, qty=1, received_amount=amount, **kwargs)

        rows = [
            item("轴承", "A", first, 120.15, paid_amount=16.31, paid_date="2026-10-01"),
            item("随单附件", "A", first, 0),
            item("历史已开票", "A", first, 24.06, invoice_no="OLD", invoice_date="2026-10-20",
                 invoice_amount=24.06, invoice_status="已开票"),
            item("其他采购员", "A", first, 30),
            item("另一厂同号单", "A", second, 49.99),
            item("无单号甲", None, first, 5),
            item("无单号乙", None, first, 6),
            item("同厂第二单", "B", first, 33.21),
        ]
        rows[3].buyer_id = lead.id
        db.add_all(rows)
        await db.commit()
        headers = [{"Authorization": "Bearer " + create_access_token(u.id)} for u in users]
        return [r.id for r in rows], [s.id for s in suppliers], headers, buyer.id


async def stored(ids):
    fields = [PI.invoice_status, PI.invoice_no, PI.invoice_date, PI.invoice_amount,
              PI.received_amount, PI.paid_amount, PI.paid_date]
    async with SessionLocal() as db:
        rows = await db.execute(select(PI.id, *fields).where(PI.id.in_(ids)))
        return {row.id: dict(row._mapping) for row in rows}


async def audit_count(user_id):
    async with SessionLocal() as db:
        return await db.scalar(select(func.count(models.AuditLog.id)).where(models.AuditLog.user_id == user_id))


def reference(group):
    return {"item_ids": group["item_ids"], "revision": group["revision"]}


def invoice(group, number, amount, date=DATE):
    return {**reference(group), "invoice_no": number, "invoice_date": date, "invoice_amount": amount}


async def run():
    ids, suppliers, headers, buyer_id = await fixtures()
    primary, zero, old, foreign, other, loose1, loose2, another = ids
    first, second = suppliers
    buyer, warehouse, lead = headers
    both = [{"supplier_id": sid, "po_no": "A"} for sid in suppliers]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async def post(path, data, who=buyer, expected=200):
            r = await client.post(BASE + path, json=data, headers=who)
            assert r.status_code == expected, (path, expected, r.status_code, r.text)
            return r.json()

        async def preview(*, items=(), orders=(), who=buyer):
            result = await post("batch-maintenance-preview", {"item_ids": list(items), "orders": list(orders)}, who)
            return {tuple(sorted(g["item_ids"])): g for g in result["groups"]}

        async def current():
            return await preview(orders=both)

        groups = await current()
        assert set(groups) == {(primary, zero), (old,), (other,)}, groups
        assert groups[primary, zero]["received_amount"] == 120.15
        assert groups[old,]["invoice_no"] == "OLD" and groups[old,]["pending_count"] == 0
        assert set(await preview(items=[zero, loose1, loose2])) == {(zero,), (loose1,), (loose2,)}
        assert set(await preview(items=[zero], orders=[both[0]])) == {(primary, zero), (old,)}
        await post("batch-maintenance-preview", {"item_ids": [primary, foreign]}, expected=404)
        await post("batch-maintenance-preview", {"orders": both}, warehouse, 403)
        foreign_group = (await preview(items=[foreign], who=lead))[foreign,]
        before = await stored(ids)
        for path, good, bad in [
            ("batch-reconcile", reference(groups[primary, zero]), reference(foreign_group)),
            ("batch-maintain-invoices", invoice(groups[primary, zero], "甲票", 120.15), invoice(foreign_group, "越权", 30)),
        ]:
            await post(path, {"groups": [good, bad]}, expected=404)
            await post(path, {"groups": [good]}, warehouse, 403)
        assert await stored(ids) == before
        print("PASS 整单/部分/无单号选择、历史发票分组、本人范围和整批权限")

        body = {"groups": [reference(g) for g in groups.values()]}
        result = await post("batch-reconcile", body)
        assert (result["updated"], result["skipped"]) == (3, 1), result
        reconciled = await stored(ids)
        for iid in (primary, zero, other):
            assert reconciled[iid] == {**before[iid], "invoice_status": "已对账"}
        for iid in (old, foreign, loose1, loose2, another):
            assert reconciled[iid] == before[iid]
        count = await audit_count(buyer_id)
        assert count > 0
        await post("batch-reconcile", body, expected=409)
        assert await stored(ids) == reconciled and await audit_count(buyer_id) == count
        print("PASS 多单对账、已开票跳过、付款与金额不变、重复提交无重复审计")

        groups = await current()
        a = invoice(groups[primary, zero], "FP-A", 120.15)
        b = invoice(groups[other,], "FP-B", 52.41, "2026-11-26")
        await post("batch-maintain-invoices", {"groups": [a, {**b, "invoice_no": "FP-A"}]}, expected=400)
        for bad_fields in ({"invoice_amount": -1}, {"invoice_amount": 5.123}, {"invoice_date": "2026-02-30"}):
            await post("batch-maintain-invoices", {"groups": [a, {**b, **bad_fields}]}, expected=422)
        assert await stored(ids) == reconciled and await audit_count(buyer_id) == count
        await post("batch-maintain-invoices", {"groups": [a, b]})
        invoices = await stored(ids)
        for iid, entry, amount in [(primary, a, 120.15), (zero, a, 0), (other, b, 52.41)]:
            assert invoices[iid] == {**reconciled[iid], "invoice_status": "已开票",
                                     "invoice_no": entry["invoice_no"], "invoice_date": entry["invoice_date"],
                                     "invoice_amount": amount}
        for iid in (old, foreign, loose1, loose2, another):
            assert invoices[iid] == reconciled[iid]
        count = await audit_count(buyer_id)
        await post("batch-maintain-invoices", {"groups": [a, b]}, expected=409)
        assert await stored(ids) == invoices and await audit_count(buyer_id) == count
        print("PASS 跨供应商分票、同票号冲突整批撤销、整单零元子行、含税费金额差额")

        # 第二个分组已被别人改动，必须连同第一个未改动的分组一起拒绝。
        groups = await current()
        a = invoice(groups[primary, zero], "FP-A2", 130.12)
        b = invoice(groups[other,], "FP-B2", 55.88)
        await post("batch-maintain-invoices", {"groups": [{**b, "invoice_no": "先保存"}]}, lead)
        before = await stored(ids)
        await post("batch-maintain-invoices", {"groups": [a, b]}, expected=409)
        assert await stored(ids) == before and await audit_count(buyer_id) == count

        # 整单预览后新增明细需要重看范围；明确勾选原明细仍可正常操作。
        old_group = (await current())[primary, zero]
        explicit = (await preview(items=[primary, zero]))[primary, zero]
        async with SessionLocal() as db:
            db.add(PI(item_name="新增零件", po_no="A", supplier_id=first, buyer_id=buyer_id,
                      arrival_date=DATE, received_amount=60))
            await db.commit()
        await post("batch-maintain-invoices", {"groups": [invoice(old_group, "FP-A3", 150.01)]}, expected=409)
        await post("batch-maintain-invoices", {"groups": [invoice(explicit, "FP-A3", 150.01)]})
        after = await stored([primary, zero])
        assert after[primary]["invoice_amount"] == 150.01 and after[zero]["invoice_amount"] == 0

        # 同一家供应商的两张采购单分别登记，未改变的数据用最新预览重交应直接跳过。
        groups = await preview(orders=[both[0], {"supplier_id": first, "po_no": "B"}])
        batch = [invoice(groups[primary, zero], "甲单新票", 150.01), invoice(groups[another,], "乙单新票", 33.21)]
        result = await post("batch-maintain-invoices", {"groups": batch})
        assert (result["updated"], result["skipped"]) == (3, 0)
        after = await stored([primary, zero, another])
        assert [after[i]["invoice_no"] for i in (primary, zero, another)] == ["甲单新票", "甲单新票", "乙单新票"]
        groups = await preview(items=[primary, zero, another])
        unchanged = [invoice(g, g["invoice_no"], g["invoice_amount"], g["invoice_date"]) for g in groups.values()]
        count = await audit_count(buyer_id)
        result = await post("batch-maintain-invoices", {"groups": unchanged})
        assert (result["updated"], result["skipped"]) == (0, 3) and await audit_count(buyer_id) == count
        print("PASS 同供应商多张采购单分别维护不同发票、原值无重复写入")

        # 手工登记的历史分摊可以不同于实收。改票号、日期时不得重新分摊。
        async with SessionLocal() as db:
            (await db.get(PI, primary)).invoice_amount = 109.12
            (await db.get(PI, zero)).invoice_amount = 40.89
            await db.commit()
        explicit = (await preview(items=[primary, zero]))[primary, zero]
        await post("batch-maintain-invoices", {"groups": [invoice(explicit, "FP-A4", 150.01, "2026-11-27")]})
        after = await stored([primary, zero])
        assert [after[i]["invoice_amount"] for i in (primary, zero)] == [109.12, 40.89]
        print("PASS 过期整批回滚、整单新增明细、部分明细保存、保留历史金额分配")

        async with SessionLocal() as db:
            missing = await db.get(PI, loose1)
            missing.arrival_date = None
            missing.received_amount = 0
            historical = await db.get(PI, old)
            historical.arrival_date = None
            historical.received_amount = 0
            await db.commit()
        groups = await preview(items=[loose1, old])
        historical_edit = invoice(groups[old,], "OLD-更正", 24.06)
        before = await stored([loose1, old])
        error = await post("batch-maintain-invoices", {"groups": [
            historical_edit, invoice(groups[loose1,], "新票", 5)
        ]}, expected=400)
        assert "无单号甲" in error["detail"], error
        assert await stored([loose1, old]) == before
        await post("batch-maintain-invoices", {"groups": [historical_edit]})
        assert (await stored([old]))[old] == {**before[old], "invoice_no": "OLD-更正", "invoice_date": DATE}
        print("PASS 未收货新票指出问题明细并整批撤销、旧发票缺收货资料可更正")

        if engine.dialect.name == "postgresql":
            group = (await preview(items=[other]))[other,]
            requests = [{"groups": [invoice(group, no, 55.88)]} for no in ("同时保存一", "同时保存二")]
            count = await audit_count(buyer_id)
            replies = await asyncio.gather(*(client.post(BASE + "batch-maintain-invoices", json=body, headers=buyer)
                                             for body in requests))
            assert sorted(r.status_code for r in replies) == [200, 409], [r.text for r in replies]
            winner = requests[next(i for i, r in enumerate(replies) if r.status_code == 200)]["groups"][0]
            assert (await stored([other]))[other]["invoice_no"] == winner["invoice_no"]
            assert await audit_count(buyer_id) == count + 1
            print("PASS PostgreSQL 并发维护同一张采购单，仅首次保存生效")


async def main():
    try:
        await run()
        print(f"PASSED #468 ({engine.dialect.name})")
    finally:
        await engine.dispose()
        sandbox.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
