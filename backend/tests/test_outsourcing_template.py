"""🆕 外协外购模板收窄为 9 列（按客户 287 提升式压料机模板）+ 存量清空重建。

口径(用户 2026-06-18)：外协外购表按模板改为 名称/图纸名称/采购负责人/供应商/
发出日期/到货日期/进度/仓库/备注；存量数据直接清掉(不迁移)。

覆盖：
- 新建项目的外协外购=新 9 列、进度=select、日期=date
- 存量旧 16 列外协外购(含数据)经 cleanup 清空并按新模板重建(records 清零、类型正确)
- 再次运行幂等(已对齐→cleared=0)
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="outsrc")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select, func
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import (run_all, ensure_schema_columns,
                                cleanup_misaligned_known_sheets, rename_known_sheets_v3)
from app.sheet_templates import SHEET_TEMPLATES
from app.routers.projects_router import create_default_template_sheets
from app import models

NEW_COLS = ['名称', '图纸名称', '采购负责人', '供应商', '发出日期', '到货日期', '进度', '仓库', '备注']
OLD_COLS = ['名称', '图纸名称', '采购负责人', '供应商', '原料编号', '发出日期', '到料日期',
            '工艺1', '工艺1发出日期', '工艺1完成日期', '工艺2', '工艺2发出日期', '工艺2完成日期',
            '进度', '仓库负责人签字', '备注']

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)


async def fields_of(db, dsid):
    r = await db.execute(select(models.Field).where(models.Field.datasheet_id == dsid)
                         .order_by(models.Field.sort_order))
    return list(r.scalars().all())


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    # 模板常量已是新 9 列（表名 2026-06-19 改为「外协加工」）
    chk(SHEET_TEMPLATES['外协加工'] == NEW_COLS, f"模板=新9列: {SHEET_TEMPLATES['外协加工']}")

    async with SessionLocal() as db:
        # 1) 新建项目 → 外协外购 = 新 9 列、进度 select、日期 date
        p = models.Project(code="T-OUT-1", name="新项目")
        db.add(p); await db.flush()
        await create_default_template_sheets(db, p.id)
        await db.commit()
        r = await db.execute(select(models.Datasheet).where(
            models.Datasheet.project_id == p.id, models.Datasheet.name == '外协加工'))
        ds_new = r.scalar_one()
        fs = await fields_of(db, ds_new.id)
        chk([f.name for f in fs] == NEW_COLS, f"新项目外协加工=9列: {[f.name for f in fs]}")
        tmap = {f.name: f.type for f in fs}
        chk(tmap.get('进度') == 'select', f"进度=select: {tmap.get('进度')}")
        chk(tmap.get('发出日期') == 'date' and tmap.get('到货日期') == 'date', f"日期=date: {tmap}")

        # 2) 造一个存量「旧 16 列」外协外购 + 数据行
        p2 = models.Project(code="T-OUT-2", name="存量项目")
        db.add(p2); await db.flush()
        ds_old = models.Datasheet(project_id=p2.id, name='外协外购', sort_order=2)
        db.add(ds_old); await db.flush()
        fids = []
        for i, nm in enumerate(OLD_COLS):
            f = models.Field(datasheet_id=ds_old.id, name=nm, type='text', sort_order=i)
            db.add(f); await db.flush(); fids.append(f.id)
        db.add(models.Record(datasheet_id=ds_old.id, sort_order=0,
                             values={str(fids[0]): "传动外协", str(fids[4]): "X-001"}))
        db.add(models.Record(datasheet_id=ds_old.id, sort_order=1, values={str(fids[0]): "传动外协2"}))
        await db.commit()
        old_dsid = ds_old.id
        cnt = (await db.execute(select(func.count()).select_from(models.Record)
                                .where(models.Record.datasheet_id == old_dsid))).scalar()
        chk(cnt == 2, f"存量旧表有2行数据: {cnt}")

    # 3) 先 rename(外协外购→外协加工) → 再 cleanup 旧表清空并按新模板重建
    async with SessionLocal() as db:
        rn = await rename_known_sheets_v3(db)
    chk(rn["renamed"] >= 1, f"存量外协外购已改名外协加工: {rn}")
    async with SessionLocal() as db:
        d = (await db.execute(select(models.Datasheet).where(models.Datasheet.id == old_dsid))).scalar_one()
        chk(d.name == '外协加工', f"改名后表名=外协加工: {d.name}")
    async with SessionLocal() as db:
        res = await cleanup_misaligned_known_sheets(db)
    chk(res["cleared"] >= 1, f"cleanup 清空了错位表: {res}")
    async with SessionLocal() as db:
        fs = await fields_of(db, old_dsid)
        chk([f.name for f in fs] == NEW_COLS, f"存量表重建为9列: {[f.name for f in fs]}")
        tmap = {f.name: f.type for f in fs}
        chk(tmap.get('进度') == 'select' and tmap.get('到货日期') == 'date', f"重建类型正确: {tmap}")
        cnt = (await db.execute(select(func.count()).select_from(models.Record)
                                .where(models.Record.datasheet_id == old_dsid))).scalar()
        chk(cnt == 0, f"存量数据已清空: {cnt}")

    # 4) 幂等：再次 cleanup → 不再清空(已对齐)
    async with SessionLocal() as db:
        res2 = await cleanup_misaligned_known_sheets(db)
    chk(res2["cleared"] == 0, f"再次运行幂等(cleared=0): {res2}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
