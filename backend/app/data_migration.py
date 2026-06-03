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
    """清理存量数据中"列N"格式的空白填充列。

    背景：早期导入时 Excel 文件本身宽度大（如 35 列）但用户只填了前
    N 列；或合并单元格被错误填充塞进了表头位置 → 后面会被 fallback
    成"列N"自动命名。这些字段不仅撑宽表格，还会让 align_known_sheet_*
    数据迁移按位置重命名时整体错位。

    判定条件 —— 满足任一即删（更激进，因为这些字段本就来自空标头位置）：
    1. 字段名匹配 ^列\\d+$（系统自动 fallback 命名）
    2. 即使该字段有数据（可能是合并单元格脏数据），也删除：
       原因是 fallback 名说明 Excel 那位置原本没有列名，
       字段意义不明确，数据可能是错位/合并填充导致的副产物
    """
    import re as _re
    from sqlalchemy import delete as sql_delete
    FALLBACK_RE = _re.compile(r'^列\d+$')

    res = await db.execute(select(models.Field))
    all_fields = res.scalars().all()
    candidates = [f for f in all_fields if FALLBACK_RE.match((f.name or '').strip())]
    if not candidates:
        return {"deleted": 0, "with_data": 0}

    # 统计有数据的"列N"字段数（仅供日志，全部都删）
    ds_ids = list({f.datasheet_id for f in candidates})
    res = await db.execute(
        select(models.Record).where(models.Record.datasheet_id.in_(ds_ids))
    )
    records_by_ds: dict[int, list] = {}
    for r in res.scalars().all():
        records_by_ds.setdefault(r.datasheet_id, []).append(r)

    with_data = 0
    to_delete: list[int] = [f.id for f in candidates]
    for f in candidates:
        rs = records_by_ds.get(f.datasheet_id, [])
        for r in rs:
            v = (r.values or {}).get(str(f.id))
            if v not in (None, '', '-'):
                with_data += 1
                break

    await db.execute(sql_delete(models.FieldPermission).where(
        models.FieldPermission.field_id.in_(to_delete)
    ))
    await db.execute(sql_delete(models.Field).where(
        models.Field.id.in_(to_delete)
    ))
    await db.commit()
    log.info(
        '[cleanup_filler_columns] 删除 %d 个"列N"空白填充列（其中 %d 个含数据但来自空标头位置，删除以避免后续 align 错位）',
        len(to_delete), with_data,
    )
    return {"deleted": len(to_delete), "with_data": with_data}


async def align_known_sheet_fields_to_template(db: AsyncSession) -> dict:
    """对已知 sheet 类型的所有 datasheet，安全地确认字段名与模板一致。

    安全策略（重要 —— 避免历史 bug 扩散）：
    - 只对"字段名已经在模板里"的字段，按其在模板里的位置移动 sort_order
    - 不强行按位置重命名 —— 因为如果之前有"列N"等错位字段，按位置
      重命名会把"采购负责人"改成"订购日期"等，错位扩散
    - 字段名不在模板里的（如"列N"、旧数据"完成日期"），保留原状，
      留给 cleanup_misaligned_known_sheets 处理
    """
    from .sheet_templates import SHEET_TEMPLATES

    res = await db.execute(select(models.Datasheet))
    datasheets = res.scalars().all()
    targets = [d for d in datasheets if d.name in SHEET_TEMPLATES]
    if not targets:
        return {"datasheets": 0, "reordered_fields": 0}

    reordered = 0
    for d in targets:
        template = SHEET_TEMPLATES[d.name]
        tpl_idx = {name: i for i, name in enumerate(template)}
        res = await db.execute(
            select(models.Field).where(models.Field.datasheet_id == d.id)
        )
        fields = list(res.scalars().all())
        # 按模板顺序重设 sort_order：字段名在模板里的，按模板下标排序；
        # 不在模板里的，放到最后（保留但不参与模板）
        for f in fields:
            target_order = tpl_idx.get(f.name)
            if target_order is not None and f.sort_order != target_order:
                f.sort_order = target_order
                reordered += 1

    if reordered:
        await db.commit()
        log.info(
            "[align_known_sheet_fields_to_template] 重排了 %d 个字段的顺序（仅对模板内的字段名）",
            reordered,
        )
    return {"datasheets": len(targets), "reordered_fields": reordered}


async def cleanup_misaligned_known_sheets(db: AsyncSession) -> dict:
    """检测已知 sheet 类型的字段是否与模板严格一致；不一致就清空 datasheet。

    判定"对齐"的标准（严格）：
    - 字段名集合（去重后）必须严格等于模板字段集合
    - 字段总数必须等于模板字段数（防止重复字段）

    任一不满足 → 字段错位或残留，清空 datasheet 的 fields + records
    + field_permissions，让用户重新导入 Excel 走干净的映射。

    这是修复历史 bug 的"硬重置"。前置假设：用户的真实数据保存在
    Excel 文件里，重新导入即可恢复。
    """
    from sqlalchemy import delete as sql_delete
    from .sheet_templates import SHEET_TEMPLATES

    res = await db.execute(select(models.Datasheet))
    datasheets = res.scalars().all()
    targets = [d for d in datasheets if d.name in SHEET_TEMPLATES]
    if not targets:
        return {"cleared": 0, "checked": 0}

    cleared = 0
    for d in targets:
        template = SHEET_TEMPLATES[d.name]
        tpl_set = set(template)
        res = await db.execute(
            select(models.Field).where(models.Field.datasheet_id == d.id)
        )
        fields = list(res.scalars().all())
        field_names_list = [(f.name or '').strip() for f in fields]
        field_names_set = set(field_names_list)

        # 严格条件：集合等于 + 字段数等于 + 无重复
        is_aligned = (
            field_names_set == tpl_set
            and len(fields) == len(template)
            and len(field_names_list) == len(field_names_set)  # 无重复
        )
        if is_aligned:
            continue

        # 收集差异信息用于日志
        missing = sorted(tpl_set - field_names_set)
        extra = sorted(field_names_set - tpl_set)
        duplicated = sorted({n for n in field_names_list if field_names_list.count(n) > 1})
        reasons = []
        if missing:
            reasons.append(f"缺失={missing}")
        if extra:
            reasons.append(f"多余={extra}")
        if duplicated:
            reasons.append(f"重复={duplicated}")

        field_ids = [f.id for f in fields]
        if field_ids:
            await db.execute(sql_delete(models.FieldPermission).where(
                models.FieldPermission.field_id.in_(field_ids)
            ))
            await db.execute(sql_delete(models.Field).where(
                models.Field.id.in_(field_ids)
            ))
        await db.execute(sql_delete(models.Record).where(
            models.Record.datasheet_id == d.id
        ))
        log.warning(
            "[cleanup_misaligned_known_sheets] 清空错位数据表 datasheet#%d (%s)；%s。"
            "请用户重新导入 Excel。",
            d.id, d.name, ", ".join(reasons) or "字段结构与模板不一致",
        )
        cleared += 1

    if cleared:
        await db.commit()
        log.info(
            "[cleanup_misaligned_known_sheets] 共清空 %d 个错位的已知 sheet 类型数据表（共检查 %d 个）",
            cleared, len(targets),
        )
    return {"cleared": cleared, "checked": len(targets)}


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
    try:
        await cleanup_misaligned_known_sheets(db)
    except Exception as e:
        log.warning("cleanup_misaligned_known_sheets failed: %s", e)
