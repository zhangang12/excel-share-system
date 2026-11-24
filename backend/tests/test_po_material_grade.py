"""#463：采购单材质快照、历史回查及采购员的单据隔离。"""
import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

temp = TemporaryDirectory(prefix="po-material-grade-")
os.environ["DATABASE_URL"] = os.environ.get("PMS_FB463_TEST_DB", f"sqlite+aiosqlite:///{temp.name}/app.db")
os.environ["FILES_DIR"] = f"{temp.name}/files"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import models as m
from app.data_migration import ensure_schema_columns, run_all
from app.database import Base, SessionLocal, engine
from app.main import app
from app.seed import seed
from app.sheet_templates import SHEET_TEMPLATES


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async def request(method, path, *, headers=None, **kwargs):
            response = await client.request(method, f"/api{path}", headers=headers, **kwargs)
            assert response.is_success, f"{method} {path}: {response.status_code} {response.text}"
            return response

        async def login(username, password):
            result = await request("POST", "/auth/login", json={"username": username, "password": password})
            return {"Authorization": f"Bearer {result.json()['access_token']}"}

        admin = await login("admin", "admin123")
        roles = {row["code"]: row["id"] for row in (await request("GET", "/admin/roles", headers=admin)).json()}

        async def create_user(username, role):
            result = await request("POST", "/admin/users", headers=admin, json={
                "username": username, "password": "test1234", "role_id": roles[role],
            })
            return result.json()["id"], await login(username, "test1234")

        buyer_id, buyer = await create_user("test-463-buyer", "buyer")
        _, other_buyer = await create_user("test-463-other", "buyer")
        _, warehouse = await create_user("test-463-wh", "warehouse")
        old_po = "TH-463-HISTORY"
        plain_po = "TH-463-MANUAL"

        async with SessionLocal() as db:
            project = m.Project(code="2026-T463", name="采购材质测试设备")
            supplier = m.Supplier(name="示例材料供应商")
            db.add_all([project, supplier])
            await db.flush()
            sheet = m.Datasheet(project_id=project.id, name="不锈钢原料下料单")
            other_sheet = m.Datasheet(project_id=project.id, name="外协加工")
            db.add_all([sheet, other_sheet])
            await db.flush()
            fields = {}
            for ds in (sheet, other_sheet):
                for index, name in enumerate(SHEET_TEMPLATES[ds.name]):
                    field = m.Field(datasheet_id=ds.id, name=name, sort_order=index)
                    db.add(field)
                    fields[ds.id, name] = field
            await db.flush()
            grade_key = str(fields[sheet.id, "材质"].id)
            new_line = m.Record(datasheet_id=sheet.id, values={grade_key: "304.0"})
            blank_line = m.Record(datasheet_id=sheet.id, values={})
            append_line = m.Record(datasheet_id=sheet.id, values={grade_key: 316.0})
            old_line = m.Record(datasheet_id=sheet.id, values={grade_key: "304.0"})
            db.add_all([new_line, blank_line, append_line, old_line])
            await db.flush()
            db.add_all([
                m.PurchaseItem(po_no=old_po, supplier_id=supplier.id, buyer_id=buyer_id,
                               project_code=project.code, source_sheet_id=sheet.id, source_record_id=old_line.id,
                               item_name="钢板", spec="J04-密封盖 · Φ305×Φ70×28", qty=2, unit_price=20, received_amount=40),
                m.PurchaseItem(po_no=old_po, supplier_id=supplier.id, buyer_id=buyer_id,
                               project_code=project.code, source_sheet_id=other_sheet.id, source_record_id=old_line.id,
                               item_name="不锈钢激光件", spec="无来源材质", qty=1),
                m.PurchaseItem(po_no=plain_po, supplier_id=supplier.id, buyer_id=buyer_id,
                               item_name="手工录入", spec="M12", qty=1, unit_price=8, received_amount=8),
            ])
            await db.commit()

        def line(rec, name, spec):
            return {"source_sheet_id": sheet.id, "source_record_id": rec.id,
                    "item_name": name, "spec": spec, "qty": 3, "unit_price": 12.5}

        path = "/purchase-mgmt/orders"
        result = await request("POST", f"{path}/from-list", headers=admin, json={
            "supplier_id": supplier.id, "delivery_date": "2026-11-25",
            "lines": [line(new_line, "新单钢板", "Φ120×8"), line(blank_line, "无材质行", "Φ45×10")],
        })
        created = result.json()
        po = created[0]["po_no"]
        assert [(r["material_grade"], r["spec"], r["received_amount"]) for r in created] == [
            ("304.0", "Φ120×8", 37.5), (None, "Φ45×10", 37.5),
        ]
        appended = (await request("POST", f"{path}/{po}/items", headers=admin,
                                  json={"lines": [line(append_line, "追加圆钢", "Φ30×80")]})).json()
        assert appended[-1]["material_grade"] == "316"
        async with SessionLocal() as db:
            for rid in (new_line.id, blank_line.id, append_line.id):
                rec = await db.get(m.Record, rid)
                rec.values = {**(rec.values or {}), grade_key: "换成其他材质"}
            await db.commit()
            await run_all(db)

        printed = (await request("GET", f"{path}/{po}", headers=admin)).json()
        assert [r["material_grade"] for r in printed] == ["304.0", None, "316"]
        legacy = (await request("GET", f"{path}/{old_po}", headers=buyer)).json()
        assert [r["material_grade"] for r in legacy] == ["304.0", None]
        assert legacy[0]["spec"] == "J04-密封盖 · Φ305×Φ70×28"
        plain = (await request("GET", f"{path}/{plain_po}", headers=buyer)).json()
        assert plain[0]["material_grade"] is None
        for suffix in ("", "/pdf"):
            denied = await client.get(f"/api{path}/{old_po}{suffix}", headers=other_buyer)
            assert denied.status_code == 404
        for target, actor in ((old_po, warehouse), (po, admin), (plain_po, buyer)):
            pdf = await request("GET", f"{path}/{target}/pdf", headers=actor)
            assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
        async with SessionLocal() as db:
            stored_legacy = await db.scalar(select(m.PurchaseItem.material_grade).where(
                m.PurchaseItem.po_no == old_po, m.PurchaseItem.source_sheet_id == sheet.id))
            assert stored_legacy is None

    await engine.dispose()
    print(f"PASSED ({engine.dialect.name})")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        temp.cleanup()
