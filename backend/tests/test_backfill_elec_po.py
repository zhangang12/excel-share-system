"""启动迁移回归：backfill_elec_po_from_uploaded —— 用已上传的采购清单 Excel 自动回填空的电工采购单第5表。
模拟生产场景：电工早先上传了采购清单(附件已存)，但当时解析失败第5表为空；升级后启动自动补回填，无需重传。
"""
import asyncio, os, sys, tempfile, shutil

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
tmp = tempfile.mkdtemp(prefix="bfelecpo")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from pathlib import Path
from openpyxl import Workbook
from sqlalchemy import select, func
from app.database import engine, SessionLocal, Base
from app import models
from app.config import settings
from app.routers.projects_router import create_default_template_sheets
from app.data_migration import backfill_elec_po_from_uploaded
from app.sheet_templates import ELEC_PO_SHEET_NAME

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)

def write_plist(name):
    Path(settings.files_dir).mkdir(parents=True, exist_ok=True)
    wb = Workbook(); ws = wb.active
    ws.append(["规格型号", "数量", "品牌", "备注"])
    ws.append(["DN50 法兰", 10, "上海阀门", "急件"])
    ws.append(["M8x30 螺栓", 200, "晋亿", ""])
    wb.save(Path(settings.files_dir) / name)

async def count_recs(db, pid):
    ds = (await db.execute(select(models.Datasheet).where(
        models.Datasheet.project_id == pid, models.Datasheet.name == ELEC_PO_SHEET_NAME))).scalar_one()
    return (await db.execute(select(func.count(models.Record.id)).where(
        models.Record.datasheet_id == ds.id))).scalar() or 0

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as db:
        # P1: 有第5表 + 有已上传采购清单附件(第5表当前空) → 应被回填
        p1 = models.Project(code="2026-056", name="100L反应釜", status="进行中"); db.add(p1)
        # P2: 有第5表但无附件 → 不动
        p2 = models.Project(code="2026-099", name="无附件项目", status="进行中"); db.add(p2)
        await db.flush()
        await create_default_template_sheets(db, p1.id)
        await create_default_template_sheets(db, p2.id)
        write_plist("plist_056.xlsx")
        db.add(models.Attachment(biz_type="order_start_output", kind="plist", project_id=p1.id,
                                 name="电器采购上传模板.xlsx", path="plist_056.xlsx", ext="xlsx", size=1))
        await db.commit()
        p1id, p2id = p1.id, p2.id
        chk(await count_recs(db, p1id) == 0, "回填前P1第5表为空")

    # 跑回填
    async with SessionLocal() as db:
        r = await backfill_elec_po_from_uploaded(db)
        chk(r["filled"] == 1, f"应回填1个项目, 实际 {r}")
        chk(await count_recs(db, p1id) == 2, f"P1第5表回填2行, 实际 {await count_recs(db, p1id)}")
        chk(await count_recs(db, p2id) == 0, "无附件的P2不受影响")

    # 幂等：再跑一次, 第5表已非空 → filled=0, 行数不变
    async with SessionLocal() as db:
        r = await backfill_elec_po_from_uploaded(db)
        chk(r["filled"] == 0, f"幂等: 二次回填0个, 实际 {r}")
        chk(await count_recs(db, p1id) == 2, "幂等: P1行数不变")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
