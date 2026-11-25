"""生产三组的图纸与倒计时、装配交接质检、项目/备机目录及整单作废。"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from httpx import ASGITransport, AsyncClient
from openpyxl import load_workbook
from sqlalchemy import func, select

tmp = TemporaryDirectory(prefix="quality-directory-")
os.environ["DATABASE_URL"] = os.environ.get("PMS_QUALITY_TEST_DB", f"sqlite+aiosqlite:///{tmp.name}/app.db")
os.environ["FILES_DIR"] = f"{tmp.name}/files"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import models
from app.data_migration import backfill_quality_queue, ensure_schema_columns, run_all
from app.database import Base, SessionLocal, engine
from app.main import app
from app.overdue import _CN_TZ
from app.seed import seed


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
        roles = {r["code"]: r["id"] for r in (await request("GET", "/admin/roles", headers=admin)).json()}

        async def create_user(username, role, name):
            user = await request("POST", "/admin/users", headers=admin, json={
                "username": username, "password": "test1234", "full_name": name, "role_id": roles[role],
            })
            return user.json()["id"], await login(username, "test1234")

        assembler_id, assembler = await create_user("test-assembly", "assembler", "装配测试员")
        inspector_id, inspector = await create_user("test-quality", "quality", "质检测试员")
        _, logistics = await create_user("test-logistics", "logistics", "物流测试员")
        menus = (await request("GET", "/auth/menus", headers=inspector)).json()["menus"]
        assert "quality" in {m["key"] for m in menus}

        today = datetime.now(_CN_TZ).date()
        year = str(today.year)
        upload = datetime(today.year, 8, 3, 3, 30, tzinfo=timezone.utc)

        async with SessionLocal() as db:
            project = models.Project(code=f"{year}-T458", name="试验设备", extra={
                "__o__交货日期": (today + timedelta(days=7)).isoformat(),
            })
            spare = models.Project(code=f"{year}-T461A", name="备机测试", extra={"__spare__": True, "__o__销售": "改过姓名"})
            old_spare = models.Project(code=f"{year}-T461B", name="存量备机", extra={"__o__销售": "备机·旧账号"})
            shipped = models.Project(code=f"{year}-T460A", name="历史已发货", status="进行中")
            unshipped = models.Project(code=f"{year}-T460B", name="历史已装配", status="已完成")
            deleted = models.Project(code=f"{year}-T460C", name="历史已作废", is_deleted=True)
            db.add_all([project, spare, old_spare, shipped, unshipped, deleted])
            await db.flush()
            produce = models.DeptOrder(project_id=project.id, dept="produce", status="in_progress")
            design = models.DeptOrder(project_id=project.id, dept="design", status="done")
            shipment = models.Shipment(project_id=project.id, status="pending")
            db.add_all([produce, design, shipment, models.Shipment(project_id=shipped.id, status="shipped"),
                        models.Shipment(project_id=unshipped.id, status="pending")])
            await db.flush()
            tasks = {
                group: models.ProduceGroupTask(order_id=produce.id, project_id=project.id, group=group,
                                               worker_id=assembler_id if group == "assembly" else None)
                for group in ("sheetmetal", "assembly", "sealing")
            }
            db.add_all(tasks.values())
            for legacy in (shipped, unshipped, deleted):
                order = models.DeptOrder(project_id=legacy.id, dept="produce", status="done")
                db.add(order)
                await db.flush()
                db.add(models.ProduceGroupTask(order_id=order.id, project_id=legacy.id, group="assembly", status="done"))
            for kind, hour, pushed in (("coldwork_pkg", 0, True), ("coldwork_pkg", 1, False),
                                       ("fitter_pkg", 2, True), ("sheetpkg", 3, True), ("sealing_pkg", 4, True)):
                db.add(models.Attachment(biz_type="order_start_output", biz_id=design.id, project_id=project.id,
                                         kind=kind, name=f"{kind}.pdf", path=f"{kind}.pdf", pushed=pushed,
                                         created_at=upload + timedelta(hours=hour), pushed_at=upload + timedelta(days=5)))
            await db.commit()
            await backfill_quality_queue(db)
            await backfill_quality_queue(db)

        async def overview(headers=admin):
            result = await request("GET", "/overview", headers=headers, params={"year": year})
            return {r["code"]: r for r in result.json()["rows"]}

        async def quality_tasks():
            return {r["code"]: r for r in (await request("GET", "/quality/tasks", headers=inspector)).json()}

        async def finish_assembly(done):
            return await request("POST", f"/produce/group/{tasks['assembly'].id}/done",
                                 headers=assembler, json={"done": done})

        assert set(await quality_tasks()) == {unshipped.code}
        assert (await overview())[shipped.code]["production_status"] == "未记录"
        assert (await overview())[unshipped.code]["shipment_status"] == "未发货"

        for group, hours in (("sheetmetal", 0), ("assembly", 2), ("sealing", 4)):
            result = await request("GET", f"/produce/{group}-projects", headers=admin, params={"year": year})
            row = next(row for row in result.json() if row["code"] == project.code)
            assert datetime.fromisoformat(row["drawing_uploaded_at"]) == upload + timedelta(hours=hours)
            assert row["device_countdown_days"] == 7

        quality_path = f"/api/quality/tasks/{tasks['assembly'].id}/done"
        assert (await client.post(quality_path, headers=inspector, json={"done": True})).status_code == 404
        if engine.dialect.name == "postgresql":
            await asyncio.gather(finish_assembly(True), finish_assembly(True))
        else:
            await finish_assembly(True)
            await finish_assembly(True)
        assert project.code in await quality_tasks()
        assert (await overview())[project.code]["production_status"] == "待质检"
        assert (await client.post(quality_path, headers=assembler, json={"done": True})).status_code == 403
        async with SessionLocal() as db:
            count = await db.scalar(select(func.count(models.Message.id)).where(
                models.Message.to_user_id == inspector_id, models.Message.biz_type == "quality_task",
                models.Message.biz_id == tasks["assembly"].id,
            ))
            assert count == 1
            assert (await db.get(models.DeptOrder, produce.id)).status == "in_progress"

        async def complete_quality():
            result = await client.post(quality_path, headers=inspector, json={"done": True})
            assert result.status_code == 200, result.text
            assert (await quality_tasks())[project.code]["quality_done_by"] == "质检测试员"
            row = (await overview())[project.code]
            assert (row["production_status"], row["shipment_status"], row["status"]) == ("已完成", "未发货", "进行中")

        await complete_quality()
        result = await client.post(quality_path, headers=inspector, json={"done": False})
        assert result.status_code == 200
        assert (await overview())[project.code]["production_status"] == "待质检"
        await complete_quality()
        await finish_assembly(False)
        assert project.code not in await quality_tasks()
        assert (await overview())[project.code]["production_status"] == "进行中"
        await finish_assembly(True)
        assert (await quality_tasks())[project.code]["quality_done_at"] is None
        await complete_quality()
        for group in ("sheetmetal", "sealing"):
            await request("POST", f"/produce/group/{tasks[group].id}/done", headers=admin, json={"done": True})
        await request("POST", f"/logistics/{shipment.id}/ship", headers=logistics,
                      files={"file": ("test.pdf", b"%PDF-1.4 test", "application/pdf")})
        row = (await overview())[project.code]
        assert (row["production_status"], row["shipment_status"]) == ("已完成", "已发货")

        # 一览新增质检状态后仍只向装配员展示生产部派给自己的项目。
        assigned = (await request("GET", "/produce/assembly-projects", headers=assembler, params={"year": year})).json()
        assert set(await overview(assembler)) == {r["code"] for r in assigned} == {project.code}
        assert {code for code, row in (await overview()).items() if row["is_spare"]} == {spare.code, old_spare.code}
        for kind, who, expected in (("spare", admin, {spare.code, old_spare.code}),
                                     ("project", assembler, {project.code}), ("spare", assembler, set())):
            result = await request("GET", "/overview/export", headers=who, params={"year": year, "project_kind": kind})
            data = list(load_workbook(BytesIO(result.content), read_only=True).active.values)
            assert data[0][2:4] == ("生产状态", "发货状态")
            assert {r[0] for r in data[1:]} == expected

        # 单独作废设计任务后，生产和电气仍待分派，备机仍应出现在目录。
        orders = {}
        for dept in ("design", "produce", "electric"):
            order = await request("POST", "/orders", headers=admin, json={"project_id": spare.id, "dept": dept})
            orders[dept] = order.json()["id"]
        await request("POST", f"/orders/{orders['design']}/void", headers=admin)
        await request("DELETE", f"/orders/{orders['design']}", headers=admin)
        assert (await overview())[spare.code]["production_status"] == "进行中"
        async with SessionLocal() as db:
            sheet = models.Datasheet(project_id=spare.id, name="外协加工")
            db.add_all([sheet, models.Shipment(project_id=old_spare.id, status="shipped")])
            await db.flush()
            record = models.Record(datasheet_id=sheet.id, values={})
            db.add(record)
            await db.commit()

        void_path = f"/projects/{spare.id}/void-spare"
        assert (await client.post(f"/api{void_path}", headers=assembler)).status_code == 403
        assert (await client.post(f"/api/projects/{project.id}/void-spare", headers=admin)).status_code == 400
        assert (await client.post(f"/api/projects/{old_spare.id}/void-spare", headers=admin)).status_code == 409
        await request("POST", void_path, headers=admin)
        await request("POST", void_path, headers=admin)
        assert set(await overview()) >= {project.code, old_spare.code}
        assert spare.code not in await overview()
        for dept in ("produce", "electric"):
            queue = (await request("GET", "/orders", headers=admin, params={"dept": dept})).json()
            assert orders[dept] not in {o["id"] for o in queue}
            stale = await client.post(f"/api/orders/{orders[dept]}/void", headers=admin)
            assert stale.status_code == 409
        async with SessionLocal() as db:
            archived = await db.get(models.Project, spare.id)
            assert (archived.code, archived.status, archived.is_deleted) == (spare.code, "已作废", True)
            assert await db.get(models.Record, record.id) is not None
            for dept in ("produce", "electric"):
                assert (await db.get(models.DeptOrder, orders[dept])).status == "voided"
            audit_count = await db.scalar(select(func.count(models.AuditLog.id)).where(
                models.AuditLog.action == "void_spare", models.AuditLog.target_id == spare.id,
            ))
            assert audit_count == 1

    await engine.dispose()
    print(f"PASSED ({engine.dialect.name})")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        tmp.cleanup()
