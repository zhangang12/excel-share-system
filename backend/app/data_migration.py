"""存量数据迁移：启动时一次性运行，所有函数必须幂等。

这里只处理"业务规则变更带来的存量数据修正"，
不做 schema 改动（schema 由 Base.metadata.create_all 负责）。
"""
import json
import logging
from sqlalchemy import select, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine

from . import models

log = logging.getLogger("data_migration")


# ---------- 🆕 v3 schema 补列（create_all 只建新表、不给已有表加列） ----------
# 表名 -> [(列名, DDL 类型片段)]；ADD COLUMN 在 SQLite 与 PostgreSQL 均支持，幂等：已存在跳过
_NEW_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "roles": [("can_push", "BOOLEAN DEFAULT FALSE")],
    "users": [("wxid", "VARCHAR(64)")],
}


async def ensure_schema_columns(engine: AsyncEngine) -> int:
    """启动时在 create_all 之后、seed 之前运行：给存量表补新增列。幂等。"""
    added = 0

    def _existing_cols(sync_conn, table: str) -> set[str]:
        insp = inspect(sync_conn)
        if table not in insp.get_table_names():
            return set()
        return {c["name"] for c in insp.get_columns(table)}

    async with engine.begin() as conn:
        for table, cols in _NEW_COLUMNS.items():
            existing = await conn.run_sync(_existing_cols, table)
            if not existing:
                continue  # 表还不存在（全新库），create_all 已含新列
            for col, ddl in cols:
                if col in existing:
                    continue
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                added += 1
                log.info("[ensure_schema_columns] %s.%s 已补列", table, col)
    return added


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


async def backfill_members_all_users_all_projects(db: AsyncSession) -> dict:
    """确保每个 active 非 admin/manager 用户都是每个 active 项目的成员。

    修复「项目先建好、用户后创建」导致的新用户缺存量项目权限问题。
    比 backfill_empty_project_members 更彻底（后者只处理 0 成员的项目，
    无法把后建的用户补进已有成员的项目）。

    - 已存在的 (项目,用户) 成员关系不动（尊重已配置的 view/edit）
    - 只新增缺失的，权限默认 edit
    - 幂等：补齐后再次运行命中不到缺失项
    """
    res = await db.execute(
        select(models.User.id).join(models.Role).where(
            models.User.is_active == True,
            models.Role.code.notin_(("admin", "manager")),
        )
    )
    user_ids = [r[0] for r in res.all()]
    res = await db.execute(
        select(models.Project.id).where(models.Project.is_deleted == False)
    )
    pids = [r[0] for r in res.all()]
    if not user_ids or not pids:
        return {"added": 0}

    res = await db.execute(
        select(models.ProjectMember.project_id, models.ProjectMember.user_id)
    )
    existing = {(p, u) for p, u in res.all()}

    added = 0
    for pid in pids:
        for uid in user_ids:
            if (pid, uid) in existing:
                continue
            db.add(models.ProjectMember(
                project_id=pid, user_id=uid, permission="edit"
            ))
            added += 1

    if added:
        await db.commit()
        log.info(
            "[backfill_members_all_users_all_projects] 补 %d 条成员关系"
            "（%d 用户 × %d 项目 全覆盖）",
            added, len(user_ids), len(pids),
        )
    return {"added": added}


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


async def migrate_sheet_template_v2(db: AsyncSession) -> dict:
    """把存量数据表迁移到「模板 v2」：钣金装配字段改名 + 钣金装配/外协外购补「备注」列。

    必须在 align_known_sheet_fields_to_template / cleanup_misaligned_known_sheets
    之前运行，否则旧字段名与新模板不一致会被 cleanup 当成错位整表清空（丢数据）。

    1. 钣金装配字段改名（按 field.id 改 name，records 按 id 取值，数据不丢）：
       钣金/钳工→工艺1、钣金发出日期→工艺1发出日期、钣金完成日期→工艺1完成日期、
       封板/抛光→工艺2、封板发出日期→工艺2发出日期、封板完成日期→工艺2完成日期
    2. 钣金装配 / 外协外购：若缺「备注」字段则补一个（放末尾）

    幂等：改完名 / 补完列后再次运行命中不到旧名 / 已有备注，空操作。
    """
    RENAME_MAP = {
        '钣金/钳工': '工艺1',
        '钣金发出日期': '工艺1发出日期',
        '钣金完成日期': '工艺1完成日期',
        '封板/抛光': '工艺2',
        '封板发出日期': '工艺2发出日期',
        '封板完成日期': '工艺2完成日期',
    }
    ADD_REMARK_SHEETS = {'钣金装配', '外协外购'}

    res = await db.execute(select(models.Datasheet))
    datasheets = res.scalars().all()
    renamed = 0
    remark_added = 0

    for d in datasheets:
        if d.name not in ('钣金装配', '外协外购'):
            continue
        res = await db.execute(
            select(models.Field).where(models.Field.datasheet_id == d.id)
        )
        fields = list(res.scalars().all())

        # 1) 改名（仅钣金装配会命中 RENAME_MAP）
        for f in fields:
            new_name = RENAME_MAP.get((f.name or '').strip())
            if new_name and f.name != new_name:
                f.name = new_name
                renamed += 1

        # 2) 补「备注」列
        if d.name in ADD_REMARK_SHEETS:
            names = {(f.name or '').strip() for f in fields}
            if '备注' not in names:
                max_order = max((f.sort_order for f in fields), default=-1)
                db.add(models.Field(
                    datasheet_id=d.id, name='备注', type='text',
                    sort_order=max_order + 1,
                ))
                remark_added += 1

    if renamed or remark_added:
        await db.commit()
        log.info(
            "[migrate_sheet_template_v2] 钣金装配改名 %d 个字段；补「备注」列 %d 张表",
            renamed, remark_added,
        )
    return {"renamed": renamed, "remark_added": remark_added}


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
        # 按模板重建空字段（让用户看到固定列名的空表，可重新导入或手填）
        for sort_i, tpl_name in enumerate(template):
            db.add(models.Field(
                datasheet_id=d.id,
                name=tpl_name,
                type='text',
                sort_order=sort_i,
            ))
        log.warning(
            "[cleanup_misaligned_known_sheets] 清空+按模板重建 datasheet#%d (%s)；"
            "%s。请用户重新导入 Excel 填充数据。",
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


async def sync_overview_to_header(db: AsyncSession) -> dict:
    """一次性把存量项目的一览字段（__o__）同步到项目详情头表（__h__）。

    针对 OVERVIEW_HEADER_ALIAS 里的 5 个映射字段：
      签订日期→下单日期 / 电工→电器 / 销售 / 设计师 / 交货日期

    策略：
    - 如果项目的 __o__<key> 有值，__h__<alias> 没值 → 复制
    - 如果项目的 __h__<alias> 已经有值（用户在详情手填过） → **不动**
      保护用户手填的详情数据，避免被一览数据强制覆盖

    幂等：再次启动不会重复同步（因为 __h__ 已经有值会被跳过）。
    用户后续可以手动编辑详情来调整不一致的值。
    """
    from .sheet_templates import OVERVIEW_HEADER_ALIAS
    from .routers.projects_router import OVERVIEW_KEY_PREFIX, HEADER_KEY_PREFIX

    if not OVERVIEW_HEADER_ALIAS:
        return {"synced_projects": 0, "synced_fields": 0}

    res = await db.execute(
        select(models.Project).where(models.Project.is_deleted == False)
    )
    projects = res.scalars().all()

    synced_projects = 0
    synced_fields = 0
    for p in projects:
        extra = dict(p.extra or {})
        before = dict(extra)  # 浅拷贝用于判断是否改动
        for ov_key, h_key in OVERVIEW_HEADER_ALIAS.items():
            o_storage = f"{OVERVIEW_KEY_PREFIX}{ov_key}"
            h_storage = f"{HEADER_KEY_PREFIX}{h_key}"
            o_val = extra.get(o_storage)
            h_val = extra.get(h_storage)
            # __o__ 有值 + __h__ 无值 → 复制
            if o_val not in (None, '') and (h_val in (None, '')):
                extra[h_storage] = o_val
                synced_fields += 1
        if extra != before:
            p.extra = extra
            synced_projects += 1

    if synced_projects:
        await db.commit()
        log.info(
            "[sync_overview_to_header] 同步 %d 个项目的一览数据到项目详情头表（共 %d 个字段）",
            synced_projects, synced_fields,
        )
    return {"synced_projects": synced_projects, "synced_fields": synced_fields}


async def backfill_template_sheets_for_empty_projects(db: AsyncSession) -> dict:
    """对"一张数据表都没有"的活跃项目，预置 4 个固定数据表（空表头）。

    背景：早期"新建项目"按钮只建项目不建数据表，导致进入项目详情看到
    "还没有数据表"。新逻辑下每个项目都应自带 4 张固定表（钣金装配/
    标准件清单/外协外购/原料下料单）。

    策略（保守）：
    - 只处理 datasheet 数量为 0 的活跃项目（已有任意表的项目一律不动，
      避免干扰已导入 Excel 的项目）
    - 幂等：建完就有 4 张表，下次启动不再命中
    """
    from .routers.projects_router import create_default_template_sheets

    res = await db.execute(
        select(models.Project.id).where(models.Project.is_deleted == False)
    )
    active_pids = [r[0] for r in res.all()]
    if not active_pids:
        return {"projects": 0, "sheets": 0}

    # 已有至少一张数据表的项目集合
    res = await db.execute(select(models.Datasheet.project_id).distinct())
    pids_with_sheets = {r[0] for r in res.all()}

    empty_pids = [pid for pid in active_pids if pid not in pids_with_sheets]
    if not empty_pids:
        return {"projects": 0, "sheets": 0}

    total_sheets = 0
    for pid in empty_pids:
        total_sheets += await create_default_template_sheets(db, pid)

    await db.commit()
    log.info(
        "[backfill_template_sheets_for_empty_projects] 为 %d 个空项目预置了 %d 张固定数据表",
        len(empty_pids), total_sheets,
    )
    return {"projects": len(empty_pids), "sheets": total_sheets}


async def sync_header_to_overview(db: AsyncSession) -> dict:
    """反向同步：把存量项目详情头表（__h__）的数据补到一览（__o__）。

    背景：通过「导入 Excel」建的项目，项目头数据写在 __h__；一览读 __o__，
    于是这些项目在一览/新版项目头表里看不到值。这里做一次反向回填，让
    两边同源。

    针对 OVERVIEW_HEADER_ALIAS 的反向映射（header_key → overview_label）：
      下单日期→签订日期 / 电器→电工 / 销售 / 设计师 / 交货日期
    策略：__h__<header_key> 有值且 __o__<overview_label> 没值 → 复制；
    已有 __o__ 值（用户在一览填过）则不动。幂等。
    """
    from .sheet_templates import OVERVIEW_HEADER_ALIAS
    from .routers.projects_router import OVERVIEW_KEY_PREFIX, HEADER_KEY_PREFIX

    if not OVERVIEW_HEADER_ALIAS:
        return {"synced_projects": 0, "synced_fields": 0}

    # 反向映射：header_key → overview_label
    reverse = {h_key: ov_key for ov_key, h_key in OVERVIEW_HEADER_ALIAS.items()}

    res = await db.execute(
        select(models.Project).where(models.Project.is_deleted == False)
    )
    projects = res.scalars().all()

    synced_projects = 0
    synced_fields = 0
    for p in projects:
        extra = dict(p.extra or {})
        before = dict(extra)
        for h_key, ov_key in reverse.items():
            h_storage = f"{HEADER_KEY_PREFIX}{h_key}"
            o_storage = f"{OVERVIEW_KEY_PREFIX}{ov_key}"
            h_val = extra.get(h_storage)
            o_val = extra.get(o_storage)
            if h_val not in (None, '') and (o_val in (None, '')):
                extra[o_storage] = h_val
                synced_fields += 1
        if extra != before:
            p.extra = extra
            synced_projects += 1

    if synced_projects:
        await db.commit()
        log.info(
            "[sync_header_to_overview] 反向同步 %d 个项目的详情头表数据到一览（共 %d 个字段）",
            synced_projects, synced_fields,
        )
    return {"synced_projects": synced_projects, "synced_fields": synced_fields}


async def backfill_completion_date(db: AsyncSession) -> dict:
    """给存量「已完成」项目回填完成日期（__o__完成日期），用于冻结
    已过时间/剩余制作时间（完成后不再实时计算）。

    真实完成时间无从得知，用项目 updated_at 的日期作为最佳近似；
    幂等：已有 __o__完成日期 的项目跳过。状态非「已完成」的不处理。
    """
    from .routers.projects_router import OVERVIEW_KEY_PREFIX

    cd_key = f"{OVERVIEW_KEY_PREFIX}完成日期"
    res = await db.execute(
        select(models.Project).where(
            models.Project.is_deleted == False,
            models.Project.status == "已完成",
        )
    )
    projects = res.scalars().all()
    filled = 0
    for p in projects:
        extra = dict(p.extra or {})
        if extra.get(cd_key):
            continue
        d = p.updated_at
        if d is None:
            continue
        extra[cd_key] = d.strftime("%Y-%m-%d")
        p.extra = extra
        filled += 1

    if filled:
        await db.commit()
        log.info("[backfill_completion_date] 回填 %d 个已完成项目的完成日期（按 updated_at 近似）", filled)
    return {"filled": filled}


async def normalize_overview_date_fields(db: AsyncSession) -> dict:
    """把存量项目 extra 里的日期型字段统一规范化为 YYYY-MM-DD。

    覆盖一览（__o__）与项目详情头表（__h__）两套前缀下、字段名是日期型的 key
    （签订日期/交货日期/制图开始/制图结束/完成日期/下单日期/制表日期/出货日期…）。
    用户历史上填的 2026/5/12、2026.5.12、2026年5月12日、2026-6-4 等都转成 2026-05-12。

    幂等：已是 YYYY-MM-DD 的值规范化后不变；解析不了的（如"待定"）原样保留。
    """
    from .routers.projects_router import OVERVIEW_KEY_PREFIX, HEADER_KEY_PREFIX
    from .sheet_templates import is_date_field, normalize_date_str

    res = await db.execute(
        select(models.Project).where(models.Project.is_deleted == False)
    )
    projects = res.scalars().all()

    changed_projects = 0
    changed_cells = 0
    for p in projects:
        extra = dict(p.extra or {})
        before = dict(extra)
        for k, v in list(extra.items()):
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            if k.startswith(OVERVIEW_KEY_PREFIX):
                label = k[len(OVERVIEW_KEY_PREFIX):]
            elif k.startswith(HEADER_KEY_PREFIX):
                label = k[len(HEADER_KEY_PREFIX):]
            else:
                continue
            if not is_date_field(label):
                continue
            nv = normalize_date_str(v)
            if nv != v:
                extra[k] = nv
                changed_cells += 1
        if extra != before:
            p.extra = extra
            changed_projects += 1

    if changed_projects:
        await db.commit()
        log.info(
            "[normalize_overview_date_fields] 规范化 %d 个项目的 %d 个日期单元格为 YYYY-MM-DD",
            changed_projects, changed_cells,
        )
    return {"projects": changed_projects, "cells": changed_cells}


async def align_overview_fields_to_template(db: AsyncSession) -> dict:
    """把一览字段表（overview_fields）对齐到「项目一览」模板的可配置列。

    背景：overview_fields 是早期导入/手动建的，残留了大量与现模板不符的旧字段
    （制图完成日期/完成时间/生产进度单/实际用时/滞后时间/发货日期/fdsf…），还把
    派生列（货期/已过时间/剩余制作时间）也塞了进来，导致「权限管理 → 项目一览
    字段」的列名和一览页对不上、且配了不生效。

    这里按一览模板的「可填写列」重建权限字段集：
      数量 / 销售 / 签订日期 / 交货日期 / 设计师 / 制图开始 / 制图结束 / 制图用时 / 电工
      - 删除不在期望集的字段（连带其 OverviewFieldPermission）
      - 补齐缺失字段、修正 type 与 sort_order
      - 名字已对的字段保留（其已配权限不丢）
    幂等：再次运行无多余字段可删、期望字段都在，空操作。
    """
    from sqlalchemy import delete as _del

    EXPECTED: list[tuple[str, str]] = [
        ('数量', 'number'), ('销售', 'text'), ('签订日期', 'date'),
        ('交货日期', 'date'), ('设计师', 'text'), ('制图开始', 'date'),
        ('制图结束', 'date'), ('制图用时', 'number'), ('电工', 'text'),
    ]
    expected_names = {n for n, _ in EXPECTED}

    res = await db.execute(select(models.OverviewField))
    existing = {f.name: f for f in res.scalars().all()}

    # 1) 删除多余字段 + 其权限
    stale = [f for name, f in existing.items() if name not in expected_names]
    if stale:
        ids = [f.id for f in stale]
        await db.execute(_del(models.OverviewFieldPermission).where(
            models.OverviewFieldPermission.field_id.in_(ids)))
        await db.execute(_del(models.OverviewField).where(
            models.OverviewField.id.in_(ids)))

    # 2) 补齐缺失 / 修正 type 与顺序
    added = 0
    for i, (name, typ) in enumerate(EXPECTED):
        f = existing.get(name)
        if f is not None:
            if f.sort_order != i:
                f.sort_order = i
            if f.type != typ:
                f.type = typ
        else:
            db.add(models.OverviewField(name=name, type=typ, sort_order=i))
            added += 1

    if stale or added:
        await db.commit()
        log.info(
            "[align_overview_fields_to_template] 一览字段表对齐模板：删除 %d 个旧字段，补齐 %d 个",
            len(stale), added,
        )
    return {"deleted": len(stale), "added": added}


async def merge_buyers_into_purchase(db: AsyncSession) -> dict:
    """🆕 v3 P-22：把 buyer_standard / buyer_outsource 两采购角色合并为单一 buyer（采购部）。

    - 存量用户 role_id 改指向 buyer
    - 两旧角色的 FieldPermission / OverviewFieldPermission 合并到 buyer：
      同 field 已有 buyer 权限则取 OR（can_view/can_edit 任一为真则真），否则复制一条
    - 旧角色记录保留（只增不改；seed 描述已标注"保留兼容"），不再分配新用户
    - 幂等：用户已是 buyer 跳过；权限合并后再跑命中不到差异
    """
    res = await db.execute(select(models.Role).where(models.Role.code == "buyer"))
    buyer = res.scalar_one_or_none()
    if buyer is None:
        return {"users": 0, "perms": 0}  # seed 未跑（理论不会发生）

    res = await db.execute(
        select(models.Role).where(models.Role.code.in_(("buyer_standard", "buyer_outsource")))
    )
    old_roles = res.scalars().all()
    if not old_roles:
        return {"users": 0, "perms": 0}
    old_ids = [r.id for r in old_roles]

    # 1) 用户迁移
    res = await db.execute(select(models.User).where(models.User.role_id.in_(old_ids)))
    users = res.scalars().all()
    for u in users:
        u.role_id = buyer.id

    # 2) 字段权限合并（两套权限表同一套逻辑）
    merged = 0
    for PermModel in (models.FieldPermission, models.OverviewFieldPermission):
        res = await db.execute(select(PermModel).where(PermModel.role_id.in_(old_ids)))
        old_perms = res.scalars().all()
        if not old_perms:
            continue
        res = await db.execute(select(PermModel).where(PermModel.role_id == buyer.id))
        buyer_perms = {p.field_id: p for p in res.scalars().all()}
        for p in old_perms:
            bp = buyer_perms.get(p.field_id)
            if bp is None:
                db.add(PermModel(
                    field_id=p.field_id, role_id=buyer.id,
                    can_view=p.can_view, can_edit=p.can_edit,
                ))
                buyer_perms[p.field_id] = PermModel(
                    field_id=p.field_id, role_id=buyer.id,
                    can_view=p.can_view, can_edit=p.can_edit,
                )
                merged += 1
            else:
                nv, ne = bp.can_view or p.can_view, bp.can_edit or p.can_edit
                if nv != bp.can_view or ne != bp.can_edit:
                    bp.can_view, bp.can_edit = nv, ne
                    merged += 1

    if users or merged:
        await db.commit()
        log.info(
            "[merge_buyers_into_purchase] %d 个采购用户并入采购部；合并/新增权限 %d 条",
            len(users), merged,
        )
    return {"users": len(users), "perms": merged}


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
        await backfill_members_all_users_all_projects(db)
    except Exception as e:
        log.warning("backfill_members_all_users_all_projects failed: %s", e)
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
        await migrate_sheet_template_v2(db)
    except Exception as e:
        log.warning("migrate_sheet_template_v2 failed: %s", e)
    try:
        await align_known_sheet_fields_to_template(db)
    except Exception as e:
        log.warning("align_known_sheet_fields_to_template failed: %s", e)
    try:
        await cleanup_misaligned_known_sheets(db)
    except Exception as e:
        log.warning("cleanup_misaligned_known_sheets failed: %s", e)
    try:
        await sync_overview_to_header(db)
    except Exception as e:
        log.warning("sync_overview_to_header failed: %s", e)
    try:
        await sync_header_to_overview(db)
    except Exception as e:
        log.warning("sync_header_to_overview failed: %s", e)
    try:
        await backfill_template_sheets_for_empty_projects(db)
    except Exception as e:
        log.warning("backfill_template_sheets_for_empty_projects failed: %s", e)
    try:
        await backfill_completion_date(db)
    except Exception as e:
        log.warning("backfill_completion_date failed: %s", e)
    try:
        await normalize_overview_date_fields(db)
    except Exception as e:
        log.warning("normalize_overview_date_fields failed: %s", e)
    try:
        await align_overview_fields_to_template(db)
    except Exception as e:
        log.warning("align_overview_fields_to_template failed: %s", e)
    try:
        await merge_buyers_into_purchase(db)
    except Exception as e:
        log.warning("merge_buyers_into_purchase failed: %s", e)
