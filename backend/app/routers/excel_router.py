"""Excel 导入导出"""
import json
import re
import tempfile
from datetime import datetime, date
from io import BytesIO
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from openpyxl import load_workbook, Workbook

from ..database import get_db
from .. import models, schemas
from ..deps import (
    get_current_user, require_not_viewer,
    user_can_view_project, user_can_edit_project,
)

router = APIRouter(prefix="/api", tags=["Excel 导入导出"])


# ============== 类型推断 ==============
DATE_PATTERN = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")
NUMBER_PATTERN = re.compile(r"^-?\d+(\.\d+)?$")


def _infer_field_type(samples: list[Any]) -> str:
    """推断字段类型。只保留 text / number / date 三种（不做 select，全部可自由输入）。"""
    non_null = [s for s in samples if s is not None and s != ""]
    if not non_null:
        return "text"
    # 全是 datetime / date
    if all(isinstance(s, (datetime, date)) for s in non_null):
        return "date"
    # 全是 number
    if all(isinstance(s, (int, float)) and not isinstance(s, bool) for s in non_null):
        return "number"
    strs = [str(s).strip() for s in non_null]
    if all(DATE_PATTERN.match(s) for s in strs):
        return "date"
    if all(NUMBER_PATTERN.match(s) for s in strs):
        return "number"
    # 其它一律 text（可输入编辑）
    return "text"


def _normalize_value(v: Any, ftype: str) -> Any:
    """根据字段类型规范化值，用于入库。"""
    if v is None or v == "":
        return None
    if ftype == "number":
        try:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return v
            return float(v) if "." in str(v) else int(v)
        except Exception:
            return str(v)
    if ftype == "date":
        if isinstance(v, datetime):
            return v.strftime("%Y-%m-%d")
        if isinstance(v, date):
            return v.strftime("%Y-%m-%d")
        s = str(v).replace("/", "-")
        if DATE_PATTERN.match(s):
            return s
        return s
    if ftype in ("select", "multi_select"):
        return str(v).strip()
    return str(v).strip()


def _fmt_preamble_cell(v: Any, is_date: bool = False, datemode: int = 0) -> str:
    """格式化 preamble 单元格：日期序列号转日期、整数浮点去 .0"""
    if v is None or v == "":
        return ""
    if is_date:
        try:
            from xlrd.xldate import xldate_as_datetime
            dt = xldate_as_datetime(v, datemode)
            if dt.hour or dt.minute:
                return dt.strftime("%Y-%m-%d %H:%M")
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    if isinstance(v, datetime):
        if v.hour or v.minute:
            return v.strftime("%Y-%m-%d %H:%M")
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


# 固定格式：第 5 行（index=4）是数据列名，第 6 行（index=5）起是数据
# 第 1-4 行是公司标题 / 项目信息区，导入时保留但不作为数据
HEADER_ROW_INDEX = 3   # 0-indexed；第 4 行是数据表列头，前 3 行是公司标题/项目信息


def _find_data_header_row(rows: list) -> int:
    """固定返回 HEADER_ROW_INDEX。如果不足 5 行则用第 1 行兜底。"""
    if len(rows) > HEADER_ROW_INDEX:
        return HEADER_ROW_INDEX
    return 0


# 表格自带 # 行号列，这些字段名导入时会跳过（避免视觉冗余）
ROWNUM_FIELD_NAMES = {"序号", "#", "no", "no.", "序", "行号", "index"}


def is_rownum_field_name(name: str) -> bool:
    """判断字段名是否明显是"行号"性质（表格 # 列已经有了，重复）"""
    if not name:
        return False
    return name.strip().lower() in ROWNUM_FIELD_NAMES


def _is_rownum_column(values: list[Any]) -> bool:
    """判断一列的所有值是否是连续整数 1, 2, 3, ...（允许空）。"""
    non_null = [v for v in values if v not in (None, "")]
    if not non_null:
        return False
    seq = []
    for v in non_null:
        try:
            if isinstance(v, bool):
                return False
            if isinstance(v, (int, float)):
                if isinstance(v, float) and not v.is_integer():
                    return False
                seq.append(int(v))
            else:
                s = str(v).strip()
                if not s.isdigit():
                    return False
                seq.append(int(s))
        except Exception:
            return False
    # 全是 1,2,3,...,n
    return seq == list(range(1, len(seq) + 1))


# ============== 导入 ==============
@router.post("/projects/{pid}/import-excel", response_model=schemas.Msg)
async def import_excel(
    pid: int, file: UploadFile = File(...),
    current: models.User = Depends(require_not_viewer),
    db: AsyncSession = Depends(get_db),
):
    """上传 Excel：每个 sheet → 一个 datasheet。自动识别表头 + 字段类型。"""
    res = await db.execute(
        select(models.Project).where(models.Project.id == pid, models.Project.is_deleted == False)
    )
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")
    if not await user_can_edit_project(db, current, p):
        raise HTTPException(403, "无权导入")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".xlsx", ".xlsm", ".xls"):
        raise HTTPException(400, "仅支持 .xlsx/.xlsm/.xls")
    content = await file.read()
    tmp = Path(tempfile.gettempdir()) / f"_imp_{pid}_{file.filename}"
    tmp.write_bytes(content)

    try:
        sheets_meta: list[tuple[str, list[str], list[list[Any]]]] = []
        if suffix == ".xls":
            import xlrd
            # 老 .xls 多为 cp936/GBK；先尝试 cp936，失败则用默认
            try:
                wb = xlrd.open_workbook(str(tmp), formatting_info=False, encoding_override="cp936")
            except Exception:
                wb = xlrd.open_workbook(str(tmp), formatting_info=False)
            for si in range(wb.nsheets):
                ws = wb.sheet_by_index(si)
                if ws.nrows < 1:
                    continue
                # 取前 10 行用于识别表头
                preview = [[ws.cell_value(r, c) for c in range(ws.ncols)]
                           for r in range(min(10, ws.nrows))]
                header_idx = _find_data_header_row(preview)
                # 保留 preamble（前几行），日期序列号转成日期文字
                preamble = []
                for ri in range(header_idx):
                    line = []
                    for c in range(ws.ncols):
                        v = ws.cell_value(ri, c)
                        ct = ws.cell_type(ri, c)
                        line.append(_fmt_preamble_cell(v, is_date=(ct == 3), datemode=wb.datemode))
                    preamble.append(line)
                headers = [str(ws.cell_value(header_idx, c)).strip() or f"列{c+1}"
                           for c in range(ws.ncols)]
                rows = []
                for r in range(header_idx + 1, ws.nrows):
                    row = []
                    for c in range(ws.ncols):
                        v = ws.cell_value(r, c)
                        ct = ws.cell_type(r, c)
                        if ct == 3:
                            try:
                                from xlrd.xldate import xldate_as_datetime
                                v = xldate_as_datetime(v, wb.datemode)
                            except Exception:
                                pass
                        row.append(v)
                    if any(v not in (None, "") for v in row):
                        rows.append(row)
                sheets_meta.append((ws.name, headers, rows, preamble))
        else:
            wb = load_workbook(tmp, data_only=True)
            for sn in wb.sheetnames:
                ws = wb[sn]
                rows_iter = list(ws.iter_rows(values_only=True))
                if not rows_iter:
                    continue
                header_idx = _find_data_header_row(rows_iter[:10])
                # 保留 header_idx 之前所有行作为 preamble（前 4 行）
                preamble = []
                for r in rows_iter[:header_idx]:
                    preamble.append([_fmt_preamble_cell(c) for c in r])
                hdr_row = rows_iter[header_idx]
                headers = [str(h).strip() if h is not None else f"列{i+1}" for i, h in enumerate(hdr_row)]
                rows = []
                for r in rows_iter[header_idx + 1:]:
                    row = list(r)
                    if any(v not in (None, "") for v in row):
                        rows.append(row)
                sheets_meta.append((sn, headers, rows, preamble))
    except Exception as e:
        raise HTTPException(400, f"解析失败：{e}")

    if not sheets_meta:
        raise HTTPException(400, "Excel 中没有可识别的数据")

    # ===== 全量替换：先删本项目所有现有数据表（级联删除字段、记录、字段权限） =====
    from sqlalchemy import delete as _del
    # 先查出所有相关 field id（要删字段权限）
    fres = await db.execute(
        select(models.Field.id).join(models.Datasheet, models.Field.datasheet_id == models.Datasheet.id)
        .where(models.Datasheet.project_id == pid)
    )
    field_ids = [r[0] for r in fres.all()]
    if field_ids:
        await db.execute(_del(models.FieldPermission).where(models.FieldPermission.field_id.in_(field_ids)))
    # 然后删数据表（外键级联会带走 fields + records）
    await db.execute(_del(models.Datasheet).where(models.Datasheet.project_id == pid))
    await db.flush()
    # =====================================================================

    # 入库：每个 sheet 一个 datasheet
    total_records = 0
    res = await db.execute(
        select(func.max(models.Datasheet.sort_order)).where(models.Datasheet.project_id == pid)
    )
    base_order = (res.scalar() or -1) + 1
    for idx, (sname, headers, rows, preamble) in enumerate(sheets_meta):
        d = models.Datasheet(
            project_id=pid, name=sname, sort_order=base_order + idx,
            header_lines=json.dumps(preamble, ensure_ascii=False) if preamble else None,
        )
        db.add(d)
        await db.flush()  # 拿到 d.id

        # 推断各列类型 + 识别需要跳过的"序号"列（避免和表格 # 列重复）
        col_count = len(headers)
        fields: list[models.Field] = []
        skipped_cols: set[int] = set()
        for ci, hname in enumerate(headers):
            col_samples = [r[ci] if ci < len(r) else None for r in rows[:50]]
            # 字段名明确是"序号"且内容是连续整数 → 跳过
            if is_rownum_field_name(hname) and _is_rownum_column(col_samples):
                skipped_cols.add(ci)
                continue
            ftype = _infer_field_type(col_samples)
            f = models.Field(
                datasheet_id=d.id, name=hname, type=ftype, sort_order=ci,
            )
            db.add(f)
            fields.append(f)
        await db.flush()

        # 行入库（跳过被忽略的列）
        for ri, row in enumerate(rows):
            values: dict[str, Any] = {}
            # 按 field 的源列序号去 row 里取值
            field_idx = 0
            for ci in range(col_count):
                if ci in skipped_cols:
                    continue
                f = fields[field_idx]
                field_idx += 1
                if ci < len(row):
                    nv = _normalize_value(row[ci], f.type)
                    if nv is not None:
                        values[str(f.id)] = nv
            r = models.Record(
                datasheet_id=d.id, sort_order=ri, values=values,
                created_by=current.id, updated_by=current.id,
            )
            db.add(r)
            total_records += 1

    await db.commit()
    return schemas.Msg(message=f"导入完成：{len(sheets_meta)} 个数据表，共 {total_records} 行")


# ============== 导出公共工具：preamble 公式列实时计算 ==============
# 与前端 DatasheetGrid.vue 的 DATE_DERIVED_COLS / DATE_KEYS 保持一致
_PREAMBLE_DERIVED = {"货期", "已过时间", "已经过时间", "倒计时", "剩余天数", "剩余"}
_PREAMBLE_ORDER_KEYS = {"下单日期", "下单时间", "下单"}
_PREAMBLE_DELIVER_KEYS = {"交货日期", "交付日期", "交期", "交货时间"}
_DATE_HEAD_RE = re.compile(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})")


def _parse_loose_date(s: Any) -> Optional[date]:
    """松散解析日期字符串：识别 YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD / YYYY年MM月DD日"""
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    text = str(s).strip()
    m = _DATE_HEAD_RE.match(text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _find_header_idx(header_row: list[Any], candidates: set[str]) -> int:
    for i, h in enumerate(header_row):
        k = str(h or "").strip()
        if k in candidates:
            return i
    return -1


def _compute_preamble_value(header_row: list[Any], value_row: list[Any], col_idx: int) -> Any:
    """对值行的某列：是公式列就实时计算，否则原样返回。

    与 DatasheetGrid.preambleCell 行为对齐：
      货期         = 交货日期 - 下单日期
      已过时间     = TODAY() - 下单日期
      倒计时       = 交货日期 - TODAY()
    """
    raw = value_row[col_idx] if col_idx < len(value_row) else ""
    header = str(header_row[col_idx] if col_idx < len(header_row) else "").strip()
    if header not in _PREAMBLE_DERIVED:
        return raw

    order_idx = _find_header_idx(header_row, _PREAMBLE_ORDER_KEYS)
    deliver_idx = _find_header_idx(header_row, _PREAMBLE_DELIVER_KEYS)
    order_date = _parse_loose_date(value_row[order_idx]) if 0 <= order_idx < len(value_row) else None
    deliver_date = _parse_loose_date(value_row[deliver_idx]) if 0 <= deliver_idx < len(value_row) else None
    today = date.today()

    if header == "货期":
        if order_date and deliver_date:
            return (deliver_date - order_date).days
        return raw
    if header in ("已过时间", "已经过时间"):
        if order_date:
            return (today - order_date).days
        return raw
    if header in ("倒计时", "剩余天数", "剩余"):
        if deliver_date:
            return (deliver_date - today).days
        return raw
    return raw


def _write_preamble_to_sheet(ws, header_lines_json: Optional[str]) -> None:
    """把 datasheet.header_lines（JSON 字符串）写到工作表顶部，
    其中值行（idx=2）的公式列按 TODAY 实时计算。"""
    if not header_lines_json:
        return
    try:
        lines = json.loads(header_lines_json)
    except Exception:
        return
    if not lines:
        return
    header_row = lines[1] if len(lines) > 1 else []
    for idx, line in enumerate(lines):
        if idx == 2 and header_row:
            row = [
                _compute_preamble_value(header_row, line, i)
                for i in range(len(line))
            ]
            ws.append(row)
        else:
            ws.append(list(line))


# ============== 导出 ==============
@router.get("/datasheets/{did}/export")
async def export_datasheet(
    did: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """导出单个数据表为 .xlsx"""
    res = await db.execute(select(models.Datasheet).where(models.Datasheet.id == did))
    d = res.scalar_one_or_none()
    if not d:
        raise HTTPException(404, "数据表不存在")
    pres = await db.execute(select(models.Project).where(models.Project.id == d.project_id))
    p = pres.scalar_one_or_none()
    if not p or not await user_can_view_project(db, current, p):
        raise HTTPException(403, "无权下载")

    fres = await db.execute(
        select(models.Field).where(models.Field.datasheet_id == did)
        .order_by(models.Field.sort_order, models.Field.id)
    )
    fields = fres.scalars().all()
    rres = await db.execute(
        select(models.Record).where(models.Record.datasheet_id == did)
        .order_by(models.Record.sort_order, models.Record.id)
    )
    records = rres.scalars().all()

    wb = Workbook()
    ws = wb.active
    ws.title = d.name[:31] or "Sheet1"
    # 1. 项目信息行（preamble）—— 公式列按 TODAY 实时算，与网页一致
    _write_preamble_to_sheet(ws, d.header_lines)
    # 2. 数据表头
    ws.append([f.name for f in fields])
    # 3. 数据
    for r in records:
        row = []
        for f in fields:
            v = (r.values or {}).get(str(f.id))
            if isinstance(v, list):
                v = "、".join(str(x) for x in v)
            row.append(v)
        ws.append(row)

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    fname = f"{p.code}_{d.name}.xlsx"
    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


@router.get("/projects/{pid}/export")
async def export_project(
    pid: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """导出整个项目所有数据表为一个 .xlsx 多 sheet 文件"""
    res = await db.execute(
        select(models.Project).where(models.Project.id == pid, models.Project.is_deleted == False)
    )
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "项目不存在")
    if not await user_can_view_project(db, current, p):
        raise HTTPException(403, "无权下载")

    dres = await db.execute(
        select(models.Datasheet).where(models.Datasheet.project_id == pid)
        .order_by(models.Datasheet.sort_order, models.Datasheet.id)
    )
    sheets = dres.scalars().all()
    if not sheets:
        raise HTTPException(404, "项目没有数据表")

    wb = Workbook()
    wb.remove(wb.active)
    for d in sheets:
        ws = wb.create_sheet(title=d.name[:31] or "Sheet")
        fres = await db.execute(
            select(models.Field).where(models.Field.datasheet_id == d.id)
            .order_by(models.Field.sort_order, models.Field.id)
        )
        fields = fres.scalars().all()
        rres = await db.execute(
            select(models.Record).where(models.Record.datasheet_id == d.id)
            .order_by(models.Record.sort_order, models.Record.id)
        )
        records = rres.scalars().all()
        # 1. 项目信息行（preamble）—— 公式列按 TODAY 实时算
        _write_preamble_to_sheet(ws, d.header_lines)
        # 2. 数据表头
        ws.append([f.name for f in fields])
        # 3. 数据
        for r in records:
            row = []
            for f in fields:
                v = (r.values or {}).get(str(f.id))
                if isinstance(v, list):
                    v = "、".join(str(x) for x in v)
                row.append(v)
            ws.append(row)

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    fname = f"{p.code}_{p.name}.xlsx"
    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )
