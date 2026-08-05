#!/usr/bin/env python3
"""只读诊断：哪些进度表会被 cleanup_misaligned_known_sheets 判成「错位」而清空。

背景
----
`data_migration.cleanup_misaligned_known_sheets` **每次后端启动都跑**：只要某张
已知类型表的字段名集合跟「当前模板」和「历史模板」都不严格相等，就把
fields + records 全删掉再按模板重建空表。它的前置假设写在注释里——
「用户的真实数据保存在 Excel 文件里，重新导入即可恢复」。

这个假设**在采购/仓库这几张表上不成立**：到料日期、进度、仓库签字是仓库
每天直接在系统里改的，Excel 里根本没有。一清就是真丢。

这个脚本只 SELECT，不改任何数据。跑它可以在**发版前**知道这次会不会清掉谁。

用法（服务器上）：
    docker cp ops/diagnose-sheet-wipe.py pms2_backend:/tmp/
    docker exec -e PYTHONPATH=/app pms2_backend python /tmp/diagnose-sheet-wipe.py
"""
import asyncio
import sys

from sqlalchemy import select, func

from app.database import SessionLocal
from app import models
from app.sheet_templates import SHEET_TEMPLATES, LEGACY_SHEET_TEMPLATES


async def main() -> int:
    async with SessionLocal() as db:
        sheets = list((await db.execute(select(models.Datasheet))).scalars().all())
        targets = [d for d in sheets if d.name in SHEET_TEMPLATES]

        proj = {p.id: p.code for p in
                (await db.execute(select(models.Project))).scalars().all()}

        doomed, ok = [], 0
        for d in targets:
            fields = list((await db.execute(
                select(models.Field).where(models.Field.datasheet_id == d.id)
            )).scalars().all())
            names = [(f.name or "").strip() for f in fields]
            nameset = set(names)

            def _matches(tpl):
                return (nameset == set(tpl) and len(fields) == len(tpl)
                        and len(names) == len(nameset))

            tpl = SHEET_TEMPLATES[d.name]
            if any(_matches(v) for v in [tpl, *LEGACY_SHEET_TEMPLATES.get(d.name, [])]):
                ok += 1
                continue

            n_rec = (await db.execute(
                select(func.count(models.Record.id))
                .where(models.Record.datasheet_id == d.id))).scalar() or 0
            doomed.append({
                "ds_id": d.id, "sheet": d.name,
                "project": proj.get(d.project_id, f"project#{d.project_id}"),
                "records": n_rec,
                "missing": sorted(set(tpl) - nameset),
                "extra": sorted(nameset - set(tpl)),
                "dup": sorted({n for n in names if names.count(n) > 1}),
            })

        print(f"\n检查了 {len(targets)} 张已知类型表：对齐 {ok}，"
              f"**会被清空 {len(doomed)}**\n")
        if not doomed:
            print("  ✅ 这次启动不会清掉任何表。")
            return 0

        at_risk = sorted(doomed, key=lambda x: -x["records"])
        total_rows = sum(x["records"] for x in at_risk)
        print(f"  ⚠️ 这些表一旦重启就会被删光，合计 {total_rows} 行真实数据：\n")
        for x in at_risk:
            flag = "🔴" if x["records"] else "  "
            print(f"  {flag} {x['project']} / {x['sheet']} (ds#{x['ds_id']})"
                  f"  {x['records']} 行")
            if x["missing"]:
                print(f"       缺列: {x['missing']}")
            if x["extra"]:
                print(f"       多列: {x['extra']}")
            if x["dup"]:
                print(f"       重复: {x['dup']}")
        print()
        return 1 if total_rows else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
