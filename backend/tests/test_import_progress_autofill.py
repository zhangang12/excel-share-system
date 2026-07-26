"""🆕 2026-07-27：详单表 Excel 导入后，进度列空白自动填「进行中」。
1. 导入的行中进度列空白 → 自动「进行中」；
2. Excel 里已填值（完成）→ 保留不覆盖；
3. 无进度列的表不受影响，正常导入。
"""
import asyncio, os, sys, tempfile
from io import BytesIO

tmp = tempfile.mkdtemp(prefix="impprog")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from openpyxl import Workbook
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


def make_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "钣金装配"
    # 与模板列序一致（生产上设计师用的是系统模板/系统导出的表）：进度在第 10 列
    ws.append(['名称', '图纸名称', '编号', '工艺1', '工艺1发出日期', '工艺1完成日期',
               '工艺2', '工艺2发出日期', '工艺2完成日期', '进度', '负责人签字', '备注', '库位'])
    ws.append(['A件', 'B01', 'X1', '', '', '', '', '', '', '完成', '', '', ''])
    ws.append(['B件', 'B02', 'X2', '', '', '', '', '', '', '', '', '', ''])
    ws.append(['C件', 'B03', 'X3', '', '', '', '', '', '', None, '', '', ''])
    ws2 = wb.create_sheet("测试零件表")   # 非模板表且无进度列
    ws2.append(["名称", "数量"])
    ws2.append(["螺栓", 10])
    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)
        p = models.Project(code="IMP-T01", name="导入进度测试", status="进行中")
        db.add(p)
        await db.commit()
        pid = p.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'admin','password':'admin123'})).json()['access_token']}"}
        r = await c.post(f"/api/projects/{pid}/import-excel", headers=H,
                         files={"file": ("详单.xlsx", make_xlsx(),
                                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        chk(r.status_code == 200, f"导入成功: {r.status_code} {r.text[:200]}")

        async with SessionLocal() as db:
            ds = (await db.execute(select(models.Datasheet).where(
                models.Datasheet.project_id == pid, models.Datasheet.name == "钣金装配"))).scalar_one()
            pf = (await db.execute(select(models.Field).where(
                models.Field.datasheet_id == ds.id, models.Field.name == "进度"))).scalar_one()
            nf = (await db.execute(select(models.Field).where(
                models.Field.datasheet_id == ds.id, models.Field.name == "名称"))).scalar_one()
            recs = list((await db.execute(select(models.Record).where(
                models.Record.datasheet_id == ds.id).order_by(models.Record.sort_order))).scalars().all())
            by_name = {r.values.get(str(nf.id)): (r.values.get(str(pf.id)) or "") for r in recs}
            chk(by_name.get("A件") == "完成", f"已填值保留不覆盖: {by_name.get('A件')!r}")
            chk(by_name.get("B件") == "进行中", f"空白填进行中(B): {by_name.get('B件')!r}")
            chk(by_name.get("C件") == "进行中", f"空白填进行中(C): {by_name.get('C件')!r}")
            # 无进度列的表正常导入
            ds2 = (await db.execute(select(models.Datasheet).where(
                models.Datasheet.project_id == pid, models.Datasheet.name == "测试零件表"))).scalar_one()
            n2 = len(list((await db.execute(select(models.Record).where(
                models.Record.datasheet_id == ds2.id))).scalars().all()))
            chk(n2 == 1, f"无进度列表导入正常: {n2}")

    await engine.dispose()
    if FAIL:
        print(f"\n{len(FAIL)} 项失败"); sys.exit(1)
    print("PASSED")

asyncio.run(main())
