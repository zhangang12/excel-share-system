"""🆕 修复「电工采购单」重复表：
1. 导入含「电工采购单」sheet 的 Excel 时跳过该表(受保护、由电工部上传生成)，不与原表重复。
2. dedupe_elec_po_sheets 清理存量重复(保留记录最多的一张)。
"""
import asyncio, os, sys, tempfile, shutil
from pathlib import Path

tmp = tempfile.mkdtemp(prefix="elecpodup")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select, func
from httpx import AsyncClient, ASGITransport
from openpyxl import Workbook
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns, dedupe_elec_po_sheets
from app import models
from app.sheet_templates import ELEC_PO_SHEET_NAME, ELEC_PO_COLUMNS

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)


async def elec_po_count(pid):
    async with SessionLocal() as db:
        return (await db.execute(select(func.count()).select_from(models.Datasheet).where(
            models.Datasheet.project_id == pid,
            models.Datasheet.name == ELEC_PO_SHEET_NAME))).scalar()


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    # ===== 准备：项目 + 1 张受保护的电工采购单(带1行数据) =====
    async with SessionLocal() as db:
        p = models.Project(code="PO-DUP-1", name="重复电工采购单项目")
        db.add(p); await db.flush()
        pid = p.id
        ds = models.Datasheet(project_id=pid, name=ELEC_PO_SHEET_NAME, sort_order=100)
        db.add(ds); await db.flush()
        f = models.Field(datasheet_id=ds.id, name="项目", type="text", sort_order=0)
        db.add(f); await db.flush()
        db.add(models.Record(datasheet_id=ds.id, sort_order=0, values={str(f.id): "PO-DUP-1"}))
        await db.commit()
    chk(await elec_po_count(pid) == 1, "导入前 1 张电工采购单")

    # ===== 构造含「电工采购单」sheet 的 xlsx 并导入 =====
    wb = Workbook()
    ws1 = wb.active; ws1.title = "钣金装配"
    ws1.append(["名称", "图纸名称", "备注"]); ws1.append(["件A", "图1", "x"])
    ws2 = wb.create_sheet(ELEC_PO_SHEET_NAME)
    ws2.append(ELEC_PO_COLUMNS); ws2.append(["导入项", "x"])
    xlsx = Path(tmp) / "imp.xlsx"; wb.save(xlsx)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'admin','password':'admin123'})).json()['access_token']}"}
        with open(xlsx, "rb") as fh:
            r = await c.post(f"/api/projects/{pid}/import-excel", headers=H,
                             files={"file": ("imp.xlsx", fh.read(),
                                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        chk(r.status_code == 200, f"导入成功: {r.status_code} {r.text[:120]}")

    chk(await elec_po_count(pid) == 1, f"导入后仍只有 1 张电工采购单(跳过导入的同名表): {await elec_po_count(pid)}")
    # 钣金装配 已按导入创建
    async with SessionLocal() as db:
        sm = (await db.execute(select(func.count()).select_from(models.Datasheet).where(
            models.Datasheet.project_id == pid, models.Datasheet.name == "钣金装配"))).scalar()
    chk(sm == 1, f"钣金装配按导入创建: {sm}")

    # ===== dedupe 迁移：人为造 2 张，跑 dedupe 保留记录多的 =====
    async with SessionLocal() as db:
        p2 = models.Project(code="PO-DUP-2", name="存量重复")
        db.add(p2); await db.flush()
        pid2 = p2.id
        d_empty = models.Datasheet(project_id=pid2, name=ELEC_PO_SHEET_NAME, sort_order=100)
        d_data = models.Datasheet(project_id=pid2, name=ELEC_PO_SHEET_NAME, sort_order=101)
        db.add_all([d_empty, d_data]); await db.flush()
        fd = models.Field(datasheet_id=d_data.id, name="项目", type="text", sort_order=0)
        db.add(fd); await db.flush()
        db.add(models.Record(datasheet_id=d_data.id, sort_order=0, values={str(fd.id): "X"}))
        await db.commit()
        keep_id = d_data.id
    chk(await elec_po_count(pid2) == 2, "造了 2 张重复")
    async with SessionLocal() as db:
        res = await dedupe_elec_po_sheets(db)
    chk(res["removed"] == 1, f"dedupe 删 1 张: {res}")
    chk(await elec_po_count(pid2) == 1, "dedupe 后剩 1 张")
    async with SessionLocal() as db:
        left = (await db.execute(select(models.Datasheet.id).where(
            models.Datasheet.project_id == pid2,
            models.Datasheet.name == ELEC_PO_SHEET_NAME))).scalar_one()
    chk(left == keep_id, "保留了有数据的那张")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
