"""反馈#393（超级管理员）+ #395（计梦蝶）。

#393「需采购有问题，以及领用出库了一个了」
    物料需求表里，料一旦领用出库、现存归 0，「建议采购」又按 `需求 − 现存` 算，
    就再叫人把需求量重买一遍——可这批料明明已经领到项目上用了。
    正确口径：先扣掉已领用的，`(需求 − 已领) − 现存`。
    （同一行的「库存」标签也从"需采购"改成"已领完"，那是前端的事，这里只锁后端口径。）

#395「建议加一个备注，方便财务打完款上传回单」
    OA「标记已付款」原来什么都填不了。加 pay_note + 付款回单附件。
    ⚠️ 备注必须写 pay_note，不能复用 settle_note——后者是核定金额时写的
       （为什么只批这么多），挤一个字段会把当初核减的理由冲掉。
"""
import asyncio, io, json, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb393")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
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


def _f(name="回单.pdf"):
    return {"file": (name, io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")}


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

        # ================= #393 建议采购要扣掉已领用 =================
        pj = (await c.post("/api/projects", headers=H, json={"code": "N-001", "name": "需采购口径"})).json()
        sup = (await c.post("/api/purchase-mgmt/suppliers", headers=H, json={"name": "供应商"})).json()["id"]
        # 建一张标准件清单，一行需求 5 个
        from app.routers.purchase_mgmt_router import _PURCHASABLE_SHEETS
        conf = _PURCHASABLE_SHEETS["standard"]
        sheet_name, item_col, spec_col, qty_col = conf[0], conf[1], conf[2], conf[3]
        async with SessionLocal() as db:
            ds = models.Datasheet(project_id=pj["id"], name=sheet_name)
            db.add(ds)
            await db.flush()
            fmap = {}
            for i, cn in enumerate([item_col, spec_col, qty_col]):
                f = models.Field(datasheet_id=ds.id, name=cn, type="text", sort_order=i)
                db.add(f)
                await db.flush()
                fmap[cn] = str(f.id)
            db.add(models.Record(datasheet_id=ds.id, sort_order=0, values={
                fmap[item_col]: "方矩管", fmap[spec_col]: "75*45*2.4", fmap[qty_col]: "5"}))
            await db.commit()

        async def demand_row():
            rows = (await c.get(f"/api/wh/demand/{pj['id']}", headers=H)).json()
            return next((x for x in rows if x["item_name"] == "方矩管"), None)

        row = await demand_row()
        chk(row is not None and row["demand_qty"] == 5 and row["stock"] == 0
            and row["suggest_purchase"] == 5,
            f"起点：需求 5、库里 0、建议采购 5: {row and (row['demand_qty'], row['stock'], row['suggest_purchase'])}")

        # 采购 5 个并收货 → 有货了，就不该再建议采购
        it = (await c.post("/api/purchase-mgmt/items", headers=H, json={
            "supplier_id": sup, "item_name": "方矩管", "spec": "75*45*2.4",
            "qty": 5, "unit_price": 30, "project_code": "N-001"})).json()
        rr = await c.put(f"/api/purchase-mgmt/items/{it['id']}/receive", headers=H, json={
            "arrival_date": "2026-08-13", "unit_price": 30, "received_amount": 150})
        chk(rr.status_code == 200, f"收货 5 个: {rr.status_code} {rr.text[:80]}")
        row = await demand_row()
        chk(row["stock"] == 5 and row["suggest_purchase"] == 0,
            f"收货后：现存 5、建议采购 0: 现存{row['stock']} 建议{row['suggest_purchase']}")

        # 全部领用出库 → 现存归 0，但需求已经满足，**不能又叫人再买 5 个**
        mats = (await c.get("/api/wh/materials", headers=H)).json()["materials"]
        mid = next(m["id"] for m in mats if m["name"] == "方矩管")
        rr = await c.post("/api/wh/txns", headers=H, json={
            "material_id": mid, "biz_date": "2026-08-13", "direction": "out",
            "qty": 5, "project_id": pj["id"], "source": "领料出库"})
        chk(rr.status_code == 200, f"领用出库 5 个: {rr.status_code} {rr.text[:80]}")
        row = await demand_row()
        chk(row["stock"] == 0 and row["issued_qty"] == 5,
            f"领完：现存 0、已领用 5: 现存{row['stock']} 已领{row['issued_qty']}")
        chk(row["suggest_purchase"] == 0,
            f"#393 **领完之后不能再叫人买一遍**（旧口径 需求5−现存0 = 又要买 5 个）: "
            f"建议采购 {row['suggest_purchase']}")

        # 只领一部分：需求 5、已领 2、现存 0 → 还差 3
        async with SessionLocal() as db:
            rec = (await db.execute(select(models.Record))).scalars().first()
            v = dict(rec.values)
            v[fmap[qty_col]] = "9"      # 需求改成 9
            rec.values = v
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(rec, "values")
            await db.commit()
        row = await demand_row()
        chk(row["demand_qty"] == 9 and row["issued_qty"] == 5 and row["suggest_purchase"] == 4,
            f"需求 9、已领 5、现存 0 → 还要买 4: 建议采购 {row['suggest_purchase']}")

        # ================= #395 OA 付款备注 + 回单 =================
        depts = (await c.get("/api/oa/departments", headers=H)).json()
        docs = (await c.get("/api/oa/doc-types", headers=H)).json()
        reim = next(d for d in docs if d["category"] == "reimbursement")
        dept = depts[0]
        # ⚠️ 提单前必须先配审批链：没配的话建单直接 400，整段 #395 都验不到。
        #   只配一步（财务），批完就进「待付款」，正好是要验的那一步。
        rc = await c.post("/api/oa/chains", headers=H, json={
            "department_id": dept["id"], "doc_type": reim["key"], "step_order": 1,
            "approver_role": "finance", "step_label": "财务部", "enabled": True})
        chk(rc.status_code == 200, f"配一步审批链（财务）: {rc.status_code} {rc.text[:110]}")
        rq = await c.post("/api/oa/requests", headers=H, json={
            "category": "reimbursement", "doc_type": reim["key"],
            "department_id": dept["id"], "title": "差旅报销", "amount": 500,
            "detail": {}})
        chk(rq.status_code == 200, f"提 OA 报销单: {rq.status_code} {rq.text[:110]}")
        rid = rq.json()["id"]

        # 一路批到待付款
        for _ in range(8):
            cur = (await c.get(f"/api/oa/requests/{rid}", headers=H)).json()
            if cur["status"] != "pending":
                break
            ar = await c.put(f"/api/oa/requests/{rid}/approve", headers=H, json={"note": "同意"})
            if ar.status_code != 200:
                break
        cur = (await c.get(f"/api/oa/requests/{rid}", headers=H)).json()
        chk(cur["status"] in ("pending_payment", "approved"),
            f"审批走完（状态 {cur['status']}）")

        if cur["status"] == "pending_payment":
            r = await c.put(f"/api/oa/requests/{rid}/mark-paid", headers=H,
                            data={"pay_note": "8/13 转账 建行尾号 6688"}, files=_f())
            chk(r.status_code == 200, f"#395 带备注+回单标记已付款: {r.status_code} {r.text[:120]}")
            j = r.json()
            chk(j.get("pay_note") == "8/13 转账 建行尾号 6688",
                f"#395 付款备注存下来了: {j.get('pay_note')}")
            chk(j.get("pay_at"), f"#395 付款时间记下来了: {j.get('pay_at')}")
            chk(not j.get("settle_note"),
                "#395 **没有污染 settle_note**（那是核定金额的理由，挤一个字段会把它冲掉）")
            async with SessionLocal() as db:
                atts = list((await db.execute(select(models.Attachment).where(
                    models.Attachment.biz_type == "oa_request",
                    models.Attachment.biz_id == rid))).scalars().all())
            recs = [a for a in atts if a.kind == "pay_receipt"]
            chk(len(recs) == 1, f"#395 回单进了申请的附件列表（kind=pay_receipt）: {len(recs)}")
            # 发起人收到的通知里带上备注
            async with SessionLocal() as db:
                msgs = [m.text for m in (await db.execute(select(models.Message).where(
                    models.Message.biz_type == "oa_request",
                    models.Message.biz_id == rid))).scalars().all()]
            chk(any("已付款" in m and "6688" in m for m in msgs),
                f"#395 通知里带上备注，发起人不用点进来看: {[m for m in msgs if '已付款' in m]}")
            # 不是待付款状态时要拦
            r = await c.put(f"/api/oa/requests/{rid}/mark-paid", headers=H, data={})
            chk(r.status_code == 400, f"#395 重复标记被拦: {r.status_code}")
        else:
            chk(False, f"这条链路没走到待付款（status={cur['status']}），#395 没验到")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
