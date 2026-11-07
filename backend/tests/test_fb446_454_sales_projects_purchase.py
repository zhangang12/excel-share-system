"""反馈 #446–#454（赵仁辉/王芹，2026-11-05~07）后端回归：

#447 客户单位联想不再带出作废项目的客户名（生产上的 1 / 11 / XXXX 来自软删项目）
#448 整单只填总价（金额在其中一行、其余 0）也能维护开票号；没到货也没金额的仍拦
#452 项目状态只能由物流确认发货自动更新，手工改状态被拒（同值提交放行，兼容老客户端）
#454 上传合同允许不传文件，先回写签订/交货日期
#453 收款批注附件：通用 attachments（biz_type=sales_note）可上传/列出/删除
"""
import asyncio, io, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb446")
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
    if not c: FAIL.append(m)


def _f(name="x.pdf"):
    return {"file": (name, io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")}


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': 'admin', 'password': 'admin123'})).json()['access_token']}"}

        async with SessionLocal() as db:
            me = (await db.execute(select(models.User).where(models.User.username == "admin"))).scalar_one()

            # ===== #447 作废项目的客户名不进联想 =====
            p_ok = models.Project(code="2026-T447A", name="正常项目")
            p_del = models.Project(code="_deleted_999_2026-T447B", name="作废项目", is_deleted=True)
            db.add_all([p_ok, p_del]); await db.flush()
            db.add_all([
                models.SalesLedger(project_id=p_ok.id, customer="正常客户有限公司", sales_uid=me.id),
                models.SalesLedger(project_id=p_del.id, customer="XXXX", sales_uid=me.id),
            ])
            p452 = models.Project(code="2026-T452", name="状态锁定测试")
            db.add(p452); await db.flush()
            led452 = models.SalesLedger(project_id=p452.id, customer="合同测试客户", sales_uid=me.id)
            db.add(led452); await db.flush()
            sup = models.Supplier(name="反馈448供应商", created_by=me.id)
            db.add(sup); await db.flush()
            items = [
                models.PurchaseItem(po_no="TH-T448", supplier_id=sup.id, item_name="圆钢",
                                    qty=1, unit_price=None, received_amount=189.80,
                                    arrival_date="2026-11-06", project_code="2026-101", buyer_id=me.id),
                models.PurchaseItem(po_no="TH-T448", supplier_id=sup.id, item_name="钢板",
                                    qty=1, unit_price=None, received_amount=0,
                                    arrival_date="2026-11-06", project_code="2026-101", buyer_id=me.id),
                models.PurchaseItem(po_no="TH-T448", supplier_id=sup.id, item_name="压盘",
                                    qty=1, unit_price=None, received_amount=0,
                                    arrival_date="2026-11-06", project_code="2026-101", buyer_id=me.id),
                models.PurchaseItem(po_no="TH-T448", supplier_id=sup.id, item_name="压盘2",
                                    qty=1, unit_price=None, received_amount=0,
                                    arrival_date="2026-11-06", project_code="2026-101", buyer_id=me.id),
            ]
            db.add_all(items)
            not_arrived = models.PurchaseItem(po_no="TH-T448", supplier_id=sup.id, item_name="未到货件",
                                              qty=1, received_amount=0, project_code="2026-101", buyer_id=me.id)
            db.add(not_arrived)
            await db.commit()
            item_ids = [i.id for i in items]
            led452_id, p452_id, not_arrived_id = led452.id, p452.id, not_arrived.id

        r = await c.get("/api/sales/customers", headers=H)
        names = r.json()
        chk(r.status_code == 200 and "正常客户有限公司" in names and "XXXX" not in names,
            f"#447 作废项目客户名被过滤: {r.status_code} {names}")

        # ===== #452 状态只能物流发货改 =====
        r = await c.put(f"/api/projects/{p452_id}", headers=H, json={"status": "已完成"})
        chk(r.status_code == 400 and "物流" in r.text, f"#452 手工改状态被拒: {r.status_code} {r.text[:80]}")
        r = await c.get(f"/api/projects/{p452_id}", headers=H)
        chk(r.json()["status"] == "进行中", f"#452 状态未被改动: {r.json()['status']}")
        r = await c.put(f"/api/projects/{p452_id}", headers=H, json={"status": "进行中"})
        chk(r.status_code == 200, f"#452 同值提交放行（老客户端兼容）: {r.status_code} {r.text[:80]}")

        # ===== #454 合同文件可后补 =====
        r = await c.post(f"/api/sales/ledger/{led452_id}/contract", headers=H,
                         data={"sign_date": "2026-11-08", "deliver_date": "2026-12-01"})
        chk(r.status_code == 200 and "可稍后补传" in r.text, f"#454 不传文件可保存日期: {r.status_code} {r.text[:80]}")
        async with SessionLocal() as db:
            led = (await db.execute(select(models.SalesLedger).where(models.SalesLedger.id == led452_id))).scalar_one()
            chk(led.contract == "无" and led.contract_file_id is None, "#454 未上传文件不冒充「合同有」")
            p = (await db.execute(select(models.Project).where(models.Project.id == p452_id))).scalar_one()
            extra = p.extra or {}
            chk(any("签订日期" in k and v == "2026-11-08" for k, v in extra.items()),
                f"#454 签订日期已回写: {extra}")
        r = await c.post(f"/api/sales/ledger/{led452_id}/contract", headers=H,
                         data={"sign_date": "2026-11-08", "deliver_date": "2026-12-01"}, files=_f("合同.pdf"))
        chk(r.status_code == 200, f"#454 带文件上传仍正常: {r.status_code} {r.text[:80]}")
        async with SessionLocal() as db:
            led = (await db.execute(select(models.SalesLedger).where(models.SalesLedger.id == led452_id))).scalar_one()
            chk(led.contract == "有" and led.contract_file_id, "#454 带文件时合同状态=有、附件已记")

        # ===== #448 整单总价（其余行 0）可维护开票号 =====
        r = await c.post("/api/purchase-mgmt/items/set-invoice-no", headers=H,
                         json={"item_ids": item_ids, "invoice_no": "INV-T448",
                               "invoice_date": "2026-11-07", "invoice_amount": 189.80})
        chk(r.status_code == 200 and r.json().get("updated") == 4,
            f"#448 0 金额子行可随整单开票: {r.status_code} {r.text[:120]}")
        async with SessionLocal() as db:
            got = (await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.id.in_(item_ids)))).scalars().all()
            amts = sorted(float(i.invoice_amount or 0) for i in got)
            chk(all(i.invoice_no == "INV-T448" and i.invoice_status == "已开票" for i in got),
                "#448 4 行都写上开票号/已开票")
            chk(amts == [0.0, 0.0, 0.0, 189.8], f"#448 开票金额按各行收货金额: {amts}")
        r = await c.post("/api/purchase-mgmt/items/set-invoice-no", headers=H,
                         json={"item_ids": item_ids + [not_arrived_id], "invoice_no": "INV-T448B",
                               "invoice_amount": 189.80})
        chk(r.status_code == 400 and "还没收货" in r.text,
            f"#448 没到货也没金额仍拦: {r.status_code} {r.text[:100]}")

        # ===== #453 收款批注附件 =====
        r = await c.post("/api/attachments", headers=H,
                         data={"biz_type": "sales_note", "biz_id": str(led452_id), "kind": "prepay"},
                         files=_f("收款截图.png"))
        chk(r.status_code == 200, f"#453 批注附件可上传: {r.status_code} {r.text[:80]}")
        att_id = r.json()["id"]
        r = await c.get("/api/attachments", headers=H,
                        params={"biz_type": "sales_note", "biz_id": led452_id})
        chk(r.status_code == 200 and any(a["id"] == att_id and a["kind"] == "prepay" for a in r.json()),
            f"#453 批注附件可按台账+字段列出: {r.status_code}")
        r = await c.delete(f"/api/attachments/{att_id}", headers=H)
        chk(r.status_code == 200, f"#453 批注附件可删除: {r.status_code}")

    print("\n" + ("ALL PASS" if not FAIL else f"FAILED {len(FAIL)}: {FAIL}"))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
