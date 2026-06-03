"""存量数据迁移：启动时一次性运行，所有函数必须幂等。

这里只处理"业务规则变更带来的存量数据修正"，
不做 schema 改动（schema 由 Base.metadata.create_all 负责）。
"""
import json
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import models

log = logging.getLogger("data_migration")


async def backfill_empty_project_members(db: AsyncSession) -> int:
    """对"还没添加任何成员的活跃项目"，回填全员 edit 权限。

    - 候选用户：is_active=true 且 role 不是 admin/manager
      （admin/manager 在权限层自动拥有所有项目，不需要塞 member 列表）
    - 已有成员的项目保持不动（尊重已做的精细化配置）
    - 已软删的项目跳过（这些会被另一个 migration 清掉）
    - 幂等：再次运行不会重复添加
    """
    res = await db.execute(
        select(models.User).join(models.Role).where(
            models.User.is_active == True,
            models.Role.code.notin_(("admin", "manager")),
        )
    )
    candidates = res.scalars().all()
    if not candidates:
        return 0

    res = await db.execute(
        select(models.Project).where(models.Project.is_deleted == False)
    )
    projects = res.scalars().all()
    if not projects:
        return 0

    # 已经有任何成员的项目集合
    res = await db.execute(select(models.ProjectMember.project_id).distinct())
    projects_with_members = {r[0] for r in res.all()}

    empty_projects = [p for p in projects if p.id not in projects_with_members]
    if not empty_projects:
        return 0

    added = 0
    for p in empty_projects:
        for u in candidates:
            db.add(models.ProjectMember(
                project_id=p.id, user_id=u.id, permission="edit"
            ))
            added += 1

    await db.commit()
    log.info(
        "[backfill_empty_project_members] 回填 %d 条 (无成员项目 %d 个 × 候选用户 %d 个)",
        added, len(empty_projects), len(candidates),
    )
    return added


async def cleanup_deleted_project_data(db: AsyncSession) -> dict:
    """清理软删项目（is_deleted=true）挂着的孤儿数据。

    - 项目本身保留（is_deleted=true 是审计追溯需要）
    - 显式删整条链：datasheets / fields / records / field_permissions / project_members
    - 幂等：没有孤儿数据时是空操作
    """
    # 避免循环 import：函数内 import
    from .routers.projects_router import _purge_project_derived_data

    res = await db.execute(
        select(models.Project.id).where(models.Project.is_deleted == True)
    )
    deleted_pids = [r[0] for r in res.all()]
    if not deleted_pids:
        return {"projects": 0}

    counts = await _purge_project_derived_data(db, deleted_pids)
    await db.commit()

    if any(counts.values()):
        log.info(
            "[cleanup_deleted_project_data] 清理 %d 个软删项目的孤儿数据：%s",
            len(deleted_pids), counts,
        )
    return {"projects": len(deleted_pids), **counts}


async def cleanup_rownum_fields(db: AsyncSession) -> dict:
    """清理存量数据中"序号"字段（与表格自带 # 列重复）。

    严格条件 —— 同时满足才删，避免误伤业务数据：
    1. 字段名 ∈ {"序号", "#", "No", "No.", "序", "行号", "Index"}（大小写不敏感）
    2. 该字段在所有 records 里的值是 1, 2, 3, ..., n 连续整数（允许空）

    删除字段时连带 field_permissions 一起清掉；records.values 里的对应 key
    保留（前端按 field.id 取值，字段已不存在就不显示，无害）。
    """
    from .routers.excel_router import ROWNUM_FIELD_NAMES, _is_rownum_column

    res = await db.execute(select(models.Field))
    all_fields = res.scalars().all()
    candidates = [
        f for f in all_fields
        if (f.name or "").strip().lower() in ROWNUM_FIELD_NAMES
    ]
    if not candidates:
        return {"deleted": 0, "kept": 0}

    # 拉所有相关 record 的 values，判断各 candidate 的列值
    from sqlalchemy import delete as sql_delete
    ds_ids = list({f.datasheet_id for f in candidates})
    res = await db.execute(
        select(models.Record).where(models.Record.datasheet_id.in_(ds_ids))
    )
    records_by_ds: dict[int, list[models.Record]] = {}
    for r in res.scalars().all():
        records_by_ds.setdefault(r.datasheet_id, []).append(r)
    for rs in records_by_ds.values():
        rs.sort(key=lambda r: (r.sort_order, r.id))

    to_delete_field_ids: list[int] = []
    kept = 0
    for f in candidates:
        rs = records_by_ds.get(f.datasheet_id, [])
        col_values = [(r.values or {}).get(str(f.id)) for r in rs]
        if _is_rownum_column(col_values):
            to_delete_field_ids.append(f.id)
        else:
            kept += 1

    if to_delete_field_ids:
        await db.execute(sql_delete(models.FieldPermission).where(
            models.FieldPermission.field_id.in_(to_delete_field_ids)
        ))
        await db.execute(sql_delete(models.Field).where(
            models.Field.id.in_(to_delete_field_ids)
        ))
        await db.commit()
        log.info(
            "[cleanup_rownum_fields] 删除 %d 个冗余的'序号'字段（%d 个保留为业务数据）",
            len(to_delete_field_ids), kept,
        )

    return {"deleted": len(to_delete_field_ids), "kept": kept}


async def backfill_project_header_from_datasheets(db: AsyncSession) -> int:
    """从老项目的 datasheet.header_lines 解析"项目头"字段，回填到 Project.extra。

    项目头列：数量 / 制表日期 / 销售 / 设计师 / 电器 / 下单日期 / 交货日期
    （项目编号、设备名称已经在 Project.code/name；货期/已过/倒计时是派生不存）

    规则：
    - 只处理还没有任何 __h__* key 的项目（防止覆盖手填值）
    - 用项目的第一个 datasheet 的 header_lines（项目内各 sheet 头一致）
    - header_lines 是 JSON 字符串 list[list[str]]：[公司标题, 表头行, 值行, ...]
    - 按列名精确匹配（trim）；找不到的列留空
    """
    from .routers.projects_router import HEADER_KEY_PREFIX

    HEADER_FIELDS = ['数量', '制表日期', '销售', '设计师', '电器', '下单日期', '交货日期']

    res = await db.execute(
        select(models.Project).where(models.Project.is_deleted == False)
    )
    projects = res.scalars().all()
    if not projects:
        return 0

    updated_count = 0
    cells_added = 0

    for p in projects:
        extra = dict(p.extra or {})
        # 已有任何 __h__* key 的项目，认为已迁移过 / 用户已手填 → 跳过
        if any(isinstance(k, str) and k.startswith(HEADER_KEY_PREFIX) for k in extra.keys()):
            continue

        # 找该项目的第一个 datasheet（按 sort_order）
        res = await db.execute(
            select(models.Datasheet).where(models.Datasheet.project_id == p.id)
            .order_by(models.Datasheet.sort_order, models.Datasheet.id).limit(1)
        )
        d = res.scalar_one_or_none()
        if not d or not d.header_lines:
            continue

        try:
            lines = json.loads(d.header_lines)
        except Exception:
            continue
        if not isinstance(lines, list) or len(lines) < 3:
            continue
        header_row = lines[1] if isinstance(lines[1], list) else []
        value_row = lines[2] if isinstance(lines[2], list) else []
        if not header_row or not value_row:
            continue

        # 表头列名 → 列索引
        col_idx_by_name: dict[str, int] = {}
        for i, h in enumerate(header_row):
            name = str(h or '').strip()
            if name and name not in col_idx_by_name:
                col_idx_by_name[name] = i

        added_for_this_project = 0
        for fname in HEADER_FIELDS:
            i = col_idx_by_name.get(fname)
            if i is None or i >= len(value_row):
                continue
            v = value_row[i]
            v_str = str(v).strip() if v is not None else ''
            if v_str and v_str not in ('-', '/'):  # 空值或占位符跳过
                extra[f'{HEADER_KEY_PREFIX}{fname}'] = v_str
                added_for_this_project += 1

        if added_for_this_project > 0:
            p.extra = extra
            updated_count += 1
            cells_added += added_for_this_project

    if updated_count:
        await db.commit()
        log.info(
            "[backfill_project_header_from_datasheets] 从 datasheet.header_lines 回填了 "
            "%d 个项目 / 共 %d 个字段",
            updated_count, cells_added,
        )
    return updated_count


async def cleanup_filler_columns(db: AsyncSession) -> dict:
    """清理存量数据中"列N"格式 + 整列空 的空白填充列。

    背景：早期导入时 Excel 文件本身宽度大（如 35 列）但用户只填了前
    N 列，后面会被 fallback 成"列N"自动命名，且数据全空。这种字段
    撑得表格特别宽，14 寸笔记本看不下，窗口缩放也卡。

    严格条件 —— 同时满足才删：
    1. 字段名匹配 ^列\\d+$（系统自动 fallback 命名）
    2. 该字段在所有 records 的 values 中均无值
    """
    import re as _re
    from sqlalchemy import delete as sql_delete
    FALLBACK_RE = _re.compile(r'^列\d+$')

    res = await db.execute(select(models.Field))
    all_fields = res.scalars().all()
    candidates = [f for f in all_fields if FALLBACK_RE.match((f.name or '').strip())]
    if not candidates:
        return {"deleted": 0, "kept": 0}

    # 拉所有相关 records 的 values，查每个 candidate 是否真的全空
    ds_ids = list({f.datasheet_id for f in candidates})
    res = await db.execute(
        select(models.Record).where(models.Record.datasheet_id.in_(ds_ids))
    )
    records_by_ds: dict[int, list] = {}
    for r in res.scalars().all():
        records_by_ds.setdefault(r.datasheet_id, []).append(r)

    to_delete: list[int] = []
    kept = 0
    for f in candidates:
        rs = records_by_ds.get(f.datasheet_id, [])
        has_value = False
        fid_str = str(f.id)
        for r in rs:
            v = (r.values or {}).get(fid_str)
            if v not in (None, '', '-'):
                has_value = True
                break
        if has_value:
            kept += 1
        else:
            to_delete.append(f.id)

    if to_delete:
        await db.execute(sql_delete(models.FieldPermission).where(
            models.FieldPermission.field_id.in_(to_delete)
        ))
        await db.execute(sql_delete(models.Field).where(
            models.Field.id.in_(to_delete)
        ))
        await db.commit()
        log.info(
            '[cleanup_filler_columns] 删除 %d 个"列N"空白填充列（保留 %d 个有数据的）',
            len(to_delete), kept,
        )
    return {"deleted": len(to_delete), "kept": kept}


async def align_known_sheet_fields_to_template(db: AsyncSession) -> dict:
    """对已知 sheet 类型的所有 datasheet，把字段按位置重命名为模板字段名。

    策略：
    - 只处理 datasheet.name 在 SHEET_TEMPLATES 里的（钣金装配等）
    - 按 sort_order 升序取字段
    - 如果字段数 <= 模板字段数，按位置重命名为模板字段名
    - 多余字段（位置超出模板长度）保留原状（避免误删用户数据）
    - records.values 的 key 是 field.id，重命名不影响数据
    - 幂等：如果已经叫模板名了，跳过
    """
    from sqlalchemy import delete as sql_delete
    from .sheet_templates import SHEET_TEMPLATES

    res = await db.execute(select(models.Datasheet))
    datasheets = res.scalars().all()
    targets = [d for d in datasheets if d.name in SHEET_TEMPLATES]
    if not targets:
        return {"datasheets": 0, "renamed_fields": 0}

    renamed = 0
    for d in targets:
        template = SHEET_TEMPLATES[d.name]
        res = await db.execute(
            select(models.Field).where(models.Field.datasheet_id == d.id)
            .order_by(models.Field.sort_order, models.Field.id)
        )
        fields = list(res.scalars().all())
        # 按位置重命名（位置 i 的字段 → template[i]）
        for i, f in enumerate(fields):
            if i >= len(template):
                break
            new_name = template[i]
            if f.name != new_name:
                f.name = new_name
                renamed += 1

    if renamed:
        await db.commit()
        log.info(
            "[align_known_sheet_fields_to_template] 重命名了 %d 个字段以对齐 %d 个已知 sheet 的模板",
            renamed, len(targets),
        )
    return {"datasheets": len(targets), "renamed_fields": renamed}


async def run_all(db: AsyncSession) -> None:
    """启动时调用：依次跑所有迁移；任一失败只 warn 不阻塞启动。"""
    try:
        await cleanup_deleted_project_data(db)
    except Exception as e:
        log.warning("cleanup_deleted_project_data failed: %s", e)
    try:
        await backfill_empty_project_members(db)
    except Exception as e:
        log.warning("backfill_empty_project_members failed: %s", e)
    try:
        await cleanup_rownum_fields(db)
    except Exception as e:
        log.warning("cleanup_rownum_fields failed: %s", e)
    try:
        await backfill_project_header_from_datasheets(db)
    except Exception as e:
        log.warning("backfill_project_header_from_datasheets failed: %s", e)
    try:
        await cleanup_filler_columns(db)
    except Exception as e:
        log.warning("cleanup_filler_columns failed: %s", e)
    try:
        await align_known_sheet_fields_to_template(db)
    except Exception as e:
        log.warning("align_known_sheet_fields_to_template failed: %s", e)
