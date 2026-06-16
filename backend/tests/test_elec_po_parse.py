"""电工采购单自动入表回归：电工上传采购清单 Excel → 解析写入项目第5表「电工采购单」。

覆盖此前生产 bug：上传 .xls 时 openpyxl 读不了 → 静默失败 → 第5表空。
现 _read_excel_rows 兼容 .xls/.xlsx；表头与列名交集映射；已有数据不覆盖；表头不匹配返回0(并记日志)。
注：xlwt 不可用,无法本地生成 .xls 夹具;.xls 读取复用主导入器成熟 xlrd 路径,这里以 .xlsx 验证映射+入表逻辑。
"""
import asyncio, os, sys, tempfile, shutil
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
tmp = tempfile.mkdtemp(prefix="elecpo")
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
from app.routers.orders_router import _populate_elec_po_from_excel
from app.sheet_templates import ELEC_PO_SHEET_NAME

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)

def write_xlsx(name, headers, rows):
    Path(settings.files_dir).mkdir(parents=True, exist_ok=True)
    wb = Workbook(); ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    wb.save(Path(settings.files_dir) / name)
    return SimpleNamespace(path=name, name=name)

async def sheet_and_records(db, pid):
    ds = (await db.execute(select(models.Datasheet).where(
        models.Datasheet.project_id == pid, models.Datasheet.name == ELEC_PO_SHEET_NAME))).scalar_one()
    recs = (await db.execute(select(models.Record).where(models.Record.datasheet_id == ds.id)
                             .order_by(models.Record.sort_order))).scalars().all()
    fields = (await db.execute(select(models.Field).where(models.Field.datasheet_id == ds.id))).scalars().all()
    fid_to_name = {f.id: f.name for f in fields}
    return ds, recs, fid_to_name

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as db:
        # 建两个带第5表的项目
        p1 = models.Project(code="2026-056", name="100L反应釜", status="进行中"); db.add(p1)
        p2 = models.Project(code="2026-099", name="表头不匹配项目", status="进行中"); db.add(p2)
        await db.flush()
        await create_default_template_sheets(db, p1.id)
        await create_default_template_sheets(db, p2.id)
        await db.commit()
        p1id, p1code, p2id, p2code = p1.id, p1.code, p2.id, p2.code

    # 1) 表头匹配的采购清单(.xlsx) → 应写入2行
    att = write_xlsx("plist_ok.xlsx",
                     ["规格型号", "数量", "品牌", "订购日期", "备注"],
                     [["DN50 法兰", 10, "上海阀门", "2026-06-16", "急件"],
                      ["M8x30 螺栓", 200, "晋亿", "2026-06-17", ""]])
    async with SessionLocal() as db:
        n = await _populate_elec_po_from_excel(db, p1id, p1code, att)
        await db.commit()
        chk(n == 2, f"表头匹配应写2行, 实际 {n}")
        ds, recs, f2n = await sheet_and_records(db, p1id)
        chk(len(recs) == 2, f"第5表应有2条记录, 实际 {len(recs)}")
        # 校验首行内容 + 项目列自动填编号
        row0 = {f2n[int(k)]: v for k, v in recs[0].values.items()}
        chk(row0.get("项目") == p1code, f"项目列自动填编号: {row0.get('项目')!r}")
        chk(row0.get("规格型号") == "DN50 法兰" and row0.get("数量") == "10", f"列映射正确: {row0}")

    # 2) 幂等：已有数据再调用应返回0(不覆盖)
    async with SessionLocal() as db:
        n = await _populate_elec_po_from_excel(db, p1id, p1code, att)
        chk(n == 0, f"已有数据应不覆盖, 实际 {n}")

    # 3) 表头完全不匹配 → 返回0(不报错,记日志)
    att_bad = write_xlsx("plist_bad.xlsx",
                         ["foo", "bar", "baz"],
                         [["a", "b", "c"]])
    async with SessionLocal() as db:
        n = await _populate_elec_po_from_excel(db, p2id, p2code, att_bad)
        await db.commit()
        chk(n == 0, f"表头不匹配应返回0, 实际 {n}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
