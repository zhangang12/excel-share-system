"""一次性回填：把 datasheets.header_lines 中的 Excel 日期序列号 / 整数浮点 修正。

跑法：
  docker exec pms2_backend python /app/backfill_header_lines.py
"""
import asyncio
import json
import re
import sys
import os
sys.path.insert(0, '/app')

from sqlalchemy import select
from app.database import SessionLocal
from app import models


SERIAL_NUM_RE = re.compile(r'^-?\d+(\.\d+)?$')


def serial_to_date(serial: float) -> str | None:
    """Excel 1900 模式日期序列号 → 'YYYY-MM-DD'。"""
    try:
        from xlrd.xldate import xldate_as_datetime
        dt = xldate_as_datetime(serial, 0)
        # 合理日期范围：1990-2100
        if dt.year < 1990 or dt.year > 2100:
            return None
        return dt.strftime('%Y-%m-%d')
    except Exception:
        return None


def looks_like_date_col_name(name: str) -> bool:
    """列名是否暗示日期类型？"""
    if not name:
        return False
    keywords = ('日期', '时间', '完成', '签订', '交货', '下单', '发货', '到料', '发出', '制表')
    return any(k in name for k in keywords)


def fix_cell(cell: str, col_name: str | None) -> str:
    """格式化单个单元格字符串。"""
    if not cell or cell == '':
        return cell
    s = str(cell)
    if not SERIAL_NUM_RE.match(s):
        return s
    # 是纯数字字符串
    try:
        v = float(s)
    except ValueError:
        return s
    # 看列名提示
    if col_name and looks_like_date_col_name(col_name):
        if 20000 < v < 70000:  # 大致 1954-2091 年范围内的 Excel 序列号
            d = serial_to_date(v)
            if d:
                return d
    # 整数浮点 → 去 .0
    if v.is_integer():
        return str(int(v))
    return s


async def main():
    fixed = 0
    async with SessionLocal() as db:
        res = await db.execute(select(models.Datasheet).where(models.Datasheet.header_lines.is_not(None)))
        for d in res.scalars().all():
            try:
                lines = json.loads(d.header_lines)
            except Exception:
                continue
            if not isinstance(lines, list):
                continue
            # 通用：preamble 第二行是列名（如果存在），第三行是值
            #   line 0 = company title (single merged cell)
            #   line 1 = 项目信息列名 (列名行)
            #   line 2 = 项目信息值 (值行)
            new_lines = [list(ln) for ln in lines]
            changed = False
            for ri, line in enumerate(new_lines):
                # 前一行作为列名提示
                col_names_row = new_lines[ri - 1] if ri > 0 else []
                for ci, cell in enumerate(line):
                    col_name = col_names_row[ci] if ci < len(col_names_row) else None
                    new_v = fix_cell(cell, col_name)
                    if new_v != cell:
                        line[ci] = new_v
                        changed = True
            if changed:
                d.header_lines = json.dumps(new_lines, ensure_ascii=False)
                fixed += 1
                print(f'fixed datasheet {d.id} ({d.name})')
        await db.commit()
    print(f'\ntotal datasheets fixed: {fixed}')


if __name__ == '__main__':
    asyncio.run(main())
