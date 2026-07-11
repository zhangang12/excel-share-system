"""存量数据迁移：启动时一次性运行，所有函数必须幂等。

这里只处理"业务规则变更带来的存量数据修正"，
不做 schema 改动（schema 由 Base.metadata.create_all 负责）。
"""
import json
import logging
import re
from sqlalchemy import select, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine

from . import models

log = logging.getLogger("data_migration")


# ---------- 🆕 v3 schema 补列（create_all 只建新表、不给已有表加列） ----------
# 表名 -> [(列名, DDL 类型片段)]；ADD COLUMN 在 SQLite 与 PostgreSQL 均支持，幂等：已存在跳过
_NEW_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "roles": [("can_push", "BOOLEAN DEFAULT FALSE")],
    "users": [("wxid", "VARCHAR(64)"), ("can_export", "BOOLEAN DEFAULT FALSE"),
              ("hidden_tabs", "JSON")],   # 🆕 #7 按账号隐藏的二级菜单tab
    "datasheets": [
        ("imported_at", "TIMESTAMP"),       # P-16 四表导入标记
        ("done_flag", "BOOLEAN DEFAULT FALSE"),  # §十七 装配前置完成标记
        ("done_at", "TIMESTAMP"),
    ],
    "attachments": [("kind", "VARCHAR(32)")],      # 附件业务内细分
    "wh_materials": [("material_grade", "VARCHAR(32)"), ("custom_values", "JSON"),
                     ("unit_price", "FLOAT")],   # 🆕 材质（字典管理）+ 自定义字段值 + 需求三 参考单价
    "produce_group_tasks": [("worker_id", "INTEGER"), ("due_date", "VARCHAR(10)")],  # 🆕 派给具体人 + 本组预计完成
    "dept_orders": [
        ("design_done_flag",   "BOOLEAN DEFAULT FALSE"),  # 🆕 设计完成第一步标记
        ("electric_done_flag", "BOOLEAN DEFAULT FALSE"),  # 🆕 接线完成第一步标记
        ("ship_prep_done",     "BOOLEAN DEFAULT FALSE"),  # 🆕 #5 设计部发货准备完成标记
    ],
    "aftersales": [("reject_reason", "TEXT"),
                   ("kind", "VARCHAR(16) DEFAULT 'aftersales'"),
                   ("project_name", "VARCHAR(128)")],  # 🆕 #98 驳回原因 + 需求一 登记类型 + #158 历史项目名
    "suppliers": [("created_by", "INTEGER")],      # 🆕 需求五 建档采购员（谁建谁看）
    "user_feedback": [                             # 🆕 系统回信（处理意见回复）
        ("reply", "TEXT"),
        ("replied_at", "TIMESTAMP"),
        ("replied_by", "INTEGER"),
        ("reply_read", "BOOLEAN DEFAULT FALSE"),
    ],
    "sales_ledger": [                              # 🆕 预付/发货前付收款批注(支持插入时间戳)
        ("prepay_note", "TEXT"),
        ("before_ship_note", "TEXT"),
        ("invoice_batch_id", "INTEGER"),           # 🆕 合并开票批次号(同客户多项目合并)
        ("void_state", "VARCHAR(20)"),             # 🆕 订单作废流: applying/voided
        ("void_reason", "TEXT"),                   # 🆕 作废原因
        ("order_state", "VARCHAR(20)"),            # 🆕 下单审批流: pending/draft
        ("order_type", "VARCHAR(16)"),             # 🆕 调货订单 / 工厂制作订单
    ],
    "shipments": [                                 # 🆕 发货清单：设计推送仓库→仓库备货完成→物流可见
        ("packlist_status", "VARCHAR(16) DEFAULT 'none'"),
        ("packlist_requested_at", "TIMESTAMP"),
        ("packlist_requested_by", "INTEGER"),
        ("packlist_ready_at", "TIMESTAMP"),
        ("packlist_ready_by", "INTEGER"),
        ("receiver_company", "VARCHAR(128)"),   # 🆕 收货单位
    ],
    "purchase_items": [                            # 🆕 采购单号（同一供应商多零件行共享一个采购单）
        ("po_no", "VARCHAR(32)"),
        ("arrival_date", "VARCHAR(10)"),           # 🆕 到货日期（仓库收货填）
        ("payment_method", "VARCHAR(16)"),         # 🆕 付款方式
        ("prepay_ratio", "FLOAT"),                 # 🆕 预付比例(%)，仅现金预付/对公预付时有意义
        ("source_sheet_id", "INTEGER"),            # 🆕 来源数据表（清单→采购单）
        ("source_record_id", "INTEGER"),           # 🆕 来源行
        ("brand", "VARCHAR(64)"),                  # 🆕 品牌（下单时逐行选/填）
        ("custom_values", "JSON"),                 # 🆕 R6 自定义字段值（存量行为空）
        ("invoice_no", "VARCHAR(64)"),             # 🆕 需求十三 开票号
        ("invoice_date", "VARCHAR(10)"),           # 🆕 开票日期（模型原始列，补登记防存量库缺列）
        ("is_kit", "BOOLEAN DEFAULT FALSE"),       # 🆕 成套采购：是否成套明细
        ("kit_parts", "JSON"),                     # 🆕 成套采购：套内零件清单(BOM,描述性)
    ],
    "payment_requests": [
        ("pay_voucher_file_id", "INTEGER"),        # 🆕 付款凭证附件
    ],
    "purchase_requests": [
        ("buyer_id", "INTEGER"),                   # 🆕 #2 采购申请指定采购员（存量表补列）
    ],
    "purchase_items": [
        ("is_stock", "BOOLEAN DEFAULT TRUE"),      # 🆕 备货标记(存量默认只入库,保持旧行为)
        ("stock_location", "VARCHAR(64)"),         # 🆕 库位(采购下单填,收货按此入库)
    ],
    "wh_materials": [
        ("category_id", "INTEGER"),                # 🆕 物料编码分类(细分类叶子)
    ],
    "wh_txns": [                                   # 🆕 库存金额/成本 + 采购收货自动入库来源
        ("unit_price", "FLOAT"),
        ("amount", "FLOAT"),
        ("purchase_item_id", "INTEGER"),
        ("location", "VARCHAR(64)"),        # 🆕 库位(入=放到哪/出=从哪领)
    ],
}


# 表名 -> [(列名, 新类型)]：存量列类型不够长，需要真的 ALTER TYPE（跟"补新列"不是一回事）。
# 只有 Postgres 需要跑——SQLite 本来就不检查 VARCHAR 长度，字符串多长都能塞进去，
# 这也是 supplier_category(17字符) 超过 VARCHAR(16) 这个 bug 在沙箱(SQLite)里一直没暴露的原因。
_WIDEN_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "material_dict": [("dtype", "VARCHAR(32)")],
}

# #158：存量 PG 库把这些列的 NOT NULL 去掉（SQLite 无需，create_all 已按模型建为可空）。幂等。
_DROP_NOTNULL: dict[str, list[str]] = {
    "aftersales": ["project_id"],   # 以往项目允许只填名称，project_id 置空
}


async def ensure_schema_columns(engine: AsyncEngine) -> int:
    """启动时在 create_all 之后、seed 之前运行：给存量表补新增列/放宽列类型。幂等。"""
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
        if engine.dialect.name == "postgresql":
            for table, cols in _WIDEN_COLUMNS.items():
                existing = await conn.run_sync(_existing_cols, table)
                if not existing:
                    continue
                for col, new_type in cols:
                    if col not in existing:
                        continue
                    await conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE {new_type}"))
                    log.info("[ensure_schema_columns] %s.%s 类型已放宽为 %s", table, col, new_type)
            for table, cols in _DROP_NOTNULL.items():
                existing = await conn.run_sync(_existing_cols, table)
                if not existing:
                    continue
                for col in cols:
                    if col not in existing:
                        continue
                    await conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {col} DROP NOT NULL"))
                    log.info("[ensure_schema_columns] %s.%s 已去除 NOT NULL", table, col)
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


async def rename_known_sheets_v3(db: AsyncSession) -> dict:
    """🆕 2026-06-19 存量数据表改名：外协外购→外协加工、原料下料单→不锈钢原料下料单
    （仅改 datasheet.name，字段/记录不动）。必须在 migrate_sheet_template_v2/align/cleanup 之前跑，
    使后续按新模板名(SHEET_TEMPLATES)对齐时能命中。幂等：旧名不存在则空操作。"""
    from sqlalchemy import update as _upd
    RENAME = {'外协外购': '外协加工', '原料下料单': '不锈钢原料下料单'}
    renamed = 0
    for old, new in RENAME.items():
        res = await db.execute(
            _upd(models.Datasheet).where(models.Datasheet.name == old).values(name=new))
        renamed += res.rowcount or 0
    if renamed:
        await db.commit()
        log.info("[rename_known_sheets_v3] 存量数据表改名 %d 张", renamed)
    return {"renamed": renamed}


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
    ADD_REMARK_SHEETS = {'钣金装配', '外协加工'}  # 2026-06-19 外协外购→外协加工(rename 在前)

    res = await db.execute(select(models.Datasheet))
    datasheets = res.scalars().all()
    renamed = 0
    remark_added = 0

    for d in datasheets:
        if d.name not in ('钣金装配', '外协加工'):
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
    from .sheet_templates import SHEET_TEMPLATES, resolve_sheet_template

    res = await db.execute(select(models.Datasheet))
    datasheets = res.scalars().all()
    targets = [d for d in datasheets if d.name in SHEET_TEMPLATES]
    if not targets:
        return {"datasheets": 0, "reordered_fields": 0}

    reordered = 0
    for d in targets:
        res = await db.execute(
            select(models.Field).where(models.Field.datasheet_id == d.id)
        )
        fields = list(res.scalars().all())
        # 🆕 按字段集合选模板：存量(历史形态)按历史模板排、新建按当前模板排，避免把老表打乱
        field_set = {(f.name or '').strip() for f in fields}
        template = resolve_sheet_template(d.name, field_set) or SHEET_TEMPLATES[d.name]
        tpl_idx = {name: i for i, name in enumerate(template)}
        # 按模板顺序重设 sort_order：字段名在模板里的，按模板下标排序；
        # 不在模板里的，放到最后（保留但不参与模板）
        for f in fields:
            target_order = tpl_idx.get((f.name or '').strip())
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


def _tpl_field_type(name: str) -> str:
    """与 projects_router._template_field_type 同源：按字段名推断模板字段类型，
    使重建的已知 sheet 字段（进度=select/日期=date/数量=number）与新建项目一致。"""
    n = (name or '').strip()
    if n == '进度':
        return 'select'
    if n.endswith('日期'):
        return 'date'
    if n == '数量':
        return 'number'
    return 'text'


def _tpl_field_config(name: str):
    """进度列预置「完成/进行中」选项（与 _create_sheet_with_fields 一致）。"""
    if (name or '').strip() == '进度':
        return json.dumps({'options': ['完成', '进行中']}, ensure_ascii=False)
    return None


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
    from .sheet_templates import SHEET_TEMPLATES, LEGACY_SHEET_TEMPLATES

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

        # 严格条件：集合等于 + 字段数等于 + 无重复；当前模板或任一历史模板命中即视为对齐。
        # 🆕 2026-06-24：模板更新只对新建项目生效，存量旧形态按历史模板放行（不清空）。
        def _matches(tpl: list[str]) -> bool:
            return (field_names_set == set(tpl)
                    and len(fields) == len(tpl)
                    and len(field_names_list) == len(field_names_set))
        if any(_matches(v) for v in [template, *LEGACY_SHEET_TEMPLATES.get(d.name, [])]):
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
        # 字段类型与新建项目一致：进度=select、*日期=date、数量=number、其余=text
        for sort_i, tpl_name in enumerate(template):
            db.add(models.Field(
                datasheet_id=d.id,
                name=tpl_name,
                type=_tpl_field_type(tpl_name),
                sort_order=sort_i,
                config=_tpl_field_config(tpl_name),
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
    """对"一张数据表都没有"的活跃项目，预置全套固定数据表 + 电工采购单（空表头）。

    背景：早期"新建项目"按钮只建项目不建数据表，导致进入项目详情看到
    "还没有数据表"。新逻辑下每个项目都应自带固定模板表（钣金装配/标准件清单/
    外协加工/不锈钢原料下料单/激光件清单）+ 电工采购单，由
    create_default_template_sheets 统一建立。

    策略（保守）：
    - 只处理 datasheet 数量为 0 的活跃项目（已有任意表的项目一律不动，
      避免干扰已导入 Excel 的项目）
    - 幂等：建完就有全套表，下次启动不再命中
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

    # 1) 用户迁移：锚点 role_id + user_roles 关联一起改指向 buyer（否则 role_codes 会残留旧角色）
    res = await db.execute(select(models.User).where(models.User.role_id.in_(old_ids)))
    users = res.scalars().all()
    for u in users:
        u.role_id = buyer.id
    # user_roles：旧采购角色关联重指向 buyer（去重后删旧），幂等
    ur_res = await db.execute(select(models.UserRole).where(models.UserRole.role_id.in_(old_ids)))
    old_urs = ur_res.scalars().all()
    if old_urs:
        bres = await db.execute(select(models.UserRole.user_id).where(models.UserRole.role_id == buyer.id))
        have_buyer = {r[0] for r in bres.all()}
        for ur in old_urs:
            if ur.user_id not in have_buyer:
                db.add(models.UserRole(user_id=ur.user_id, role_id=buyer.id))
                have_buyer.add(ur.user_id)
        from sqlalchemy import delete as _del_ur
        await db.execute(_del_ur(models.UserRole).where(models.UserRole.role_id.in_(old_ids)))

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


async def backfill_datasheet_imported_at(db: AsyncSession) -> dict:
    """🆕 v3 P-16 存量回填：四表中已有数据行的 datasheet 视为"已导入"，
    置 imported_at = updated_at（最佳近似）。

    不回填的话，存量项目的设计任务完成会被 D1 校验（四表未导入）卡死。
    幂等：已有 imported_at 的跳过；空表（无 records）不回填——保持"未导入"语义。
    """
    from .sheet_templates import SHEET_TEMPLATES
    from sqlalchemy import func as _f

    res = await db.execute(select(models.Datasheet))
    sheets = [d for d in res.scalars().all()
              if d.name in SHEET_TEMPLATES and d.imported_at is None]
    if not sheets:
        return {"filled": 0}

    ds_ids = [d.id for d in sheets]
    res = await db.execute(
        select(models.Record.datasheet_id, _f.count(models.Record.id))
        .where(models.Record.datasheet_id.in_(ds_ids))
        .group_by(models.Record.datasheet_id)
    )
    counts = dict(res.all())

    filled = 0
    for d in sheets:
        if counts.get(d.id, 0) > 0:
            d.imported_at = d.updated_at or d.created_at
            filled += 1
    if filled:
        await db.commit()
        log.info("[backfill_datasheet_imported_at] 回填 %d 张有数据的模板表为'已导入'", filled)
    return {"filled": filled}


async def backfill_sales_ledger(db: AsyncSession) -> dict:
    """🆕 v3 M02 存量回填：给没有台账行的未删项目补 sales_ledger。

    - sales_uid：按项目一览 __o__销售 的姓名与 users.full_name 唯一匹配，
      匹配不到留空（业务后续在台账补录）
    - 金额/客户等业务字段留空待补录；contract 按是否有签订日期粗推为 有/无
    - 幂等：project_id 已有 ledger 跳过
    """
    res = await db.execute(
        select(models.Project).where(models.Project.is_deleted == False)  # noqa: E712
    )
    projects = res.scalars().all()
    if not projects:
        return {"created": 0}

    res = await db.execute(select(models.SalesLedger.project_id))
    have = {r[0] for r in res.all()}

    res = await db.execute(select(models.User).where(models.User.is_active == True))  # noqa: E712
    by_name: dict[str, list[int]] = {}
    for u in res.scalars().all():
        if u.full_name:
            by_name.setdefault(u.full_name.strip(), []).append(u.id)

    created = 0
    for p in projects:
        if p.id in have:
            continue
        extra = p.extra or {}
        sales_name = str(extra.get("__o__销售") or "").strip()
        if sales_name.startswith("备机"):   # 🆕 备机下单项目不进销售台账
            continue
        uid = None
        if sales_name and len(by_name.get(sales_name, [])) == 1:
            uid = by_name[sales_name][0]
        sign = extra.get("__o__签订日期")
        db.add(models.SalesLedger(
            project_id=p.id, sales_uid=uid,
            contract="有" if sign else "无",
        ))
        created += 1

    if created:
        await db.commit()
        log.info("[backfill_sales_ledger] 为 %d 个存量项目补台账行", created)
    return {"created": created}


async def backfill_shipments(db: AsyncSession) -> dict:
    """🆕 v3 M08 存量回填：给「进行中」未删项目补发货待办行（已完成/已归档视为历史已交付不补）。
    幂等：project_id 已有 shipment 跳过。收货信息留空待补。"""
    res = await db.execute(
        select(models.Project).where(
            models.Project.is_deleted == False,  # noqa: E712
            models.Project.status == "进行中",
        )
    )
    projects = res.scalars().all()
    if not projects:
        return {"created": 0}
    res = await db.execute(select(models.Shipment.project_id))
    have = {r[0] for r in res.all()}
    created = 0
    for p in projects:
        if p.id in have:
            continue
        if str((p.extra or {}).get("__o__销售") or "").startswith("备机"):
            continue  # 备机下单不进物流发货看板
        db.add(models.Shipment(project_id=p.id))
        created += 1
    if created:
        await db.commit()
        log.info("[backfill_shipments] 为 %d 个进行中存量项目补发货待办", created)
    return {"created": created}


async def dedupe_elec_po_sheets(db: AsyncSession) -> dict:
    """清理「电工采购单」重复表：一个项目出现 ≥2 张同名表时，保留有数据(记录最多)的一张，
    其余连同字段/记录/字段权限一并删除。幂等：无重复时空操作。

    必须在 backfill_elec_po_sheet 之前跑——先去重，backfill 的「已有则跳过」才不会误判。
    """
    from .sheet_templates import ELEC_PO_SHEET_NAME
    from sqlalchemy import delete as _del, func as _f

    res = await db.execute(
        select(models.Datasheet).where(models.Datasheet.name == ELEC_PO_SHEET_NAME))
    by_proj: dict[int, list] = {}
    for d in res.scalars().all():
        by_proj.setdefault(d.project_id, []).append(d)

    removed = 0
    for pid, ds in by_proj.items():
        if len(ds) < 2:
            continue
        counts: dict[int, int] = {}
        for d in ds:
            counts[d.id] = (await db.execute(
                select(_f.count(models.Record.id)).where(models.Record.datasheet_id == d.id)
            )).scalar() or 0
        keep = max(ds, key=lambda d: (counts[d.id], -d.id))  # 记录多优先，其次保留 id 最小(最早)
        drop_ids = [d.id for d in ds if d.id != keep.id]
        fres = await db.execute(select(models.Field.id).where(models.Field.datasheet_id.in_(drop_ids)))
        fids = [r[0] for r in fres.all()]
        if fids:
            await db.execute(_del(models.FieldPermission).where(models.FieldPermission.field_id.in_(fids)))
            await db.execute(_del(models.Field).where(models.Field.id.in_(fids)))
        await db.execute(_del(models.Record).where(models.Record.datasheet_id.in_(drop_ids)))
        await db.execute(_del(models.Datasheet).where(models.Datasheet.id.in_(drop_ids)))
        removed += len(drop_ids)

    if removed:
        await db.commit()
        log.info("[dedupe_elec_po_sheets] 清理重复「电工采购单」%d 张", removed)
    return {"removed": removed}


async def backfill_elec_po_sheet(db: AsyncSession) -> dict:
    """🆕 v3 M12：给已有数据表的存量活跃项目补建第 5 张「电工采购单」空表（§十六）。

    backfill_template_sheets_for_empty_projects 只处理 0 表项目，不会给已有 4 表的
    项目补第 5 张——故单独迁移。幂等：项目已有同名表跳过。
    """
    from .sheet_templates import ELEC_PO_SHEET_NAME, ELEC_PO_COLUMNS
    from .routers.projects_router import _create_sheet_with_fields

    res = await db.execute(
        select(models.Project.id).where(models.Project.is_deleted == False)  # noqa: E712
    )
    active_pids = [r[0] for r in res.all()]
    if not active_pids:
        return {"created": 0}

    # 已有电工采购单的项目集合
    res = await db.execute(
        select(models.Datasheet.project_id).where(
            models.Datasheet.name == ELEC_PO_SHEET_NAME)
    )
    have = {r[0] for r in res.all()}
    # 至少有 1 张表的项目才补（纯空项目交给 backfill_template_sheets_for_empty_projects 建全套）
    res = await db.execute(select(models.Datasheet.project_id).distinct())
    has_any = {r[0] for r in res.all()}

    created = 0
    for pid in active_pids:
        if pid in have or pid not in has_any:
            continue
        await _create_sheet_with_fields(db, pid, ELEC_PO_SHEET_NAME, ELEC_PO_COLUMNS, 100)
        created += 1
    if created:
        await db.commit()
        log.info("[backfill_elec_po_sheet] 为 %d 个存量项目补建电工采购单第5表", created)
    return {"created": created}


async def backfill_elec_po_from_uploaded(db: AsyncSession) -> dict:
    """🆕 对"已上传电工采购清单但第5表仍空"的项目，启动时用已存的 Excel 自动解析回填。

    背景：早期 _populate_elec_po_from_excel 只支持 .xlsx，电工上传 .xls 时解析静默失败 →
    第5表空。现解析已兼容 .xls/.xlsx，本迁移把那批已上传文件补解析入表，免去人工重传。
    幂等：_populate_elec_po_from_excel 自带"第5表非空则跳过"保护；仅对仍为空的表生效。
    """
    from .routers.orders_router import _populate_elec_po_from_excel
    from .sheet_templates import ELEC_PO_SHEET_NAME

    res = await db.execute(select(models.Datasheet).where(
        models.Datasheet.name == ELEC_PO_SHEET_NAME))
    sheets = list(res.scalars().all())
    filled = 0
    for ds in sheets:
        # 该项目最近一次电工采购清单附件（电工接单上传：biz_type=order_start_output, kind=plist）
        att = (await db.execute(
            select(models.Attachment).where(
                models.Attachment.project_id == ds.project_id,
                models.Attachment.biz_type == "order_start_output",
                models.Attachment.kind == "plist",
            ).order_by(models.Attachment.id.desc()).limit(1)
        )).scalar_one_or_none()
        if not att:
            continue
        proj = (await db.execute(
            select(models.Project).where(models.Project.id == ds.project_id))).scalar_one_or_none()
        if not proj:
            continue
        try:
            n = await _populate_elec_po_from_excel(db, ds.project_id, proj.code, att)
            if n > 0:
                filled += 1
        except Exception as e:  # noqa: BLE001  单个项目失败不影响其它
            log.warning("[backfill_elec_po_from_uploaded] project=%s file=%s err=%s",
                        proj.code, att.name, e)
    if filled:
        await db.commit()
        log.info("[backfill_elec_po_from_uploaded] 用已上传Excel回填 %d 个项目电工采购单", filled)
    return {"filled": filled}


async def close_legacy_electric_done_orders(db: AsyncSession) -> dict:
    """🆕 #197 止损迁移：「接线完成即完成」上线时，把此前已点过接线完成、还卡在
    进行中等发货准备的存量电工单直接置为完成（完成日期=本次部署日）——
    避免这批单按旧口径继续累计逾期天数（系统没记录当初接线完成是哪天，
    done_date 只能取部署日，比继续流血准）。电路图/发货准备去「已完成」tab 里补。

    幂等：新口径下点接线完成即置 done；reopen 现在会清 electric_done_flag——
    此后 in_progress + electric_done_flag 的组合不会再出现，本迁移只命中存量。"""
    from datetime import date as _date

    r = await db.execute(select(models.DeptOrder).where(
        models.DeptOrder.dept == "electric",
        models.DeptOrder.status == "in_progress",
        models.DeptOrder.electric_done_flag == True))  # noqa: E712
    orders = list(r.scalars().all())
    if not orders:
        return {"closed": 0}
    today_s = _date.today().isoformat()
    for o in orders:
        o.status = "done"
        if not o.done_date:
            o.done_date = today_s
    await db.commit()
    log.info("[close_legacy_electric_done_orders] 止损置完成 %d 张电工存量单(done_date=%s)",
             len(orders), today_s)
    return {"closed": len(orders)}


async def add_location_column_to_known_sheets(db: AsyncSession) -> dict:
    """🆕 #195：给存量项目的进度表（钣金装配/标准件清单/外协加工/不锈钢原料下料单/
    激光件清单/电工采购单）末尾补「库位」列。新建项目由模板自带。
    幂等：已有同名字段的表跳过。追加在末尾，不影响 Excel 按位置导入的映射。"""
    from .sheet_templates import SHEET_TEMPLATES, ELEC_PO_SHEET_NAME

    names = list(SHEET_TEMPLATES.keys()) + [ELEC_PO_SHEET_NAME]
    sheets = (await db.execute(select(models.Datasheet).where(
        models.Datasheet.name.in_(names)))).scalars().all()
    added = 0
    for ds in sheets:
        flds = (await db.execute(select(models.Field).where(
            models.Field.datasheet_id == ds.id))).scalars().all()
        if not flds:
            continue   # 空表(未导入)不补,建列由导入/模板流程负责
        if any((f.name or "").strip() == "库位" for f in flds):
            continue
        mx = max((f.sort_order or 0) for f in flds)
        db.add(models.Field(datasheet_id=ds.id, name="库位", type="text", sort_order=mx + 1))
        added += 1
    if added:
        await db.commit()
        log.info("[add_location_column_to_known_sheets] 为 %d 张进度表补「库位」列", added)
    return {"added": added}


async def fix_elec_po_item_names(db: AsyncSession) -> dict:
    """🆕 #196：修复「电工采购单」'项目'列被写成项目编号的存量行。

    旧 _populate_elec_po_from_excel 把'项目'列硬编码为项目编号，把电工 Excel 里的物料名称
    整列丢掉（该列口径与标准件清单一致=物料名称）。本迁移只处理"全部行'项目'==项目编号"
    的表（bug 特征指纹），重新解析原上传 Excel 的 项目/名称/品名/物料名称 列按行序回填；
    解析行数与表内行数对不上（被手工增删过）或 Excel 无名称列则跳过。
    幂等：修复后'项目'≠项目编号，下次启动不再命中。
    """
    from pathlib import Path
    from .routers.orders_router import _read_excel_rows, _cell_to_str
    from .sheet_templates import ELEC_PO_SHEET_NAME
    from .config import settings

    sheets = (await db.execute(select(models.Datasheet).where(
        models.Datasheet.name == ELEC_PO_SHEET_NAME))).scalars().all()
    fixed = 0
    for ds in sheets:
        proj = (await db.execute(select(models.Project).where(
            models.Project.id == ds.project_id))).scalar_one_or_none()
        if not proj:
            continue
        flds = (await db.execute(select(models.Field).where(
            models.Field.datasheet_id == ds.id))).scalars().all()
        fld = next((f for f in flds if f.name == "项目"), None)
        if not fld:
            continue
        recs = (await db.execute(select(models.Record).where(
            models.Record.datasheet_id == ds.id)
            .order_by(models.Record.sort_order, models.Record.id))).scalars().all()
        if not recs:
            continue
        fid = str(fld.id)
        if not all(str((r.values or {}).get(fid) or "").strip() == proj.code for r in recs):
            continue   # 有行不是项目编号 → 不是本 bug 的表（或已手工修过），不动
        att = (await db.execute(select(models.Attachment).where(
            models.Attachment.project_id == ds.project_id,
            models.Attachment.biz_type == "order_start_output",
            models.Attachment.kind == "plist")
            .order_by(models.Attachment.id.desc()).limit(1))).scalar_one_or_none()
        if not att:
            continue
        try:
            rows = _read_excel_rows(Path(settings.files_dir) / att.path)
        except Exception as e:  # noqa: BLE001
            log.warning("[fix_elec_po_item_names] 解析失败 %s: %s", proj.code, e)
            continue
        if not rows:
            continue
        head_idx, best = 0, -1
        for i, r in enumerate(rows[:5]):
            n = sum(1 for c in r if c not in (None, ""))
            if n > best:
                best, head_idx = n, i
        headers = [str(c).strip() if c is not None else "" for c in rows[head_idx]]
        name_col = next((i for i, h in enumerate(headers)
                         if h in ("项目", "名称", "品名", "物料名称")), None)
        if name_col is None:
            continue
        # 复刻入表时的行筛选（跳过全空行/无命中列的行），保证与 sort_order 同序
        fnames = {f.name for f in flds}
        col_map = {h: i for i, h in enumerate(headers) if h in fnames and h != "项目"}
        names = []
        for r in rows[head_idx + 1:]:
            if not any(c not in (None, "") for c in r):
                continue
            if not any(ci < len(r) and r[ci] not in (None, "") for ci in col_map.values()):
                continue
            v = r[name_col] if name_col < len(r) else None
            names.append(_cell_to_str(v) if v not in (None, "") else "")
        if len(names) != len(recs):
            log.warning("[fix_elec_po_item_names] %s 行数不匹配(表%d/Excel%d)，跳过",
                        proj.code, len(recs), len(names))
            continue
        changed = False
        for rec, nm in zip(recs, names):
            if nm and nm != proj.code:
                vals = dict(rec.values or {})
                vals[fid] = nm
                rec.values = vals
                changed = True
        if changed:
            fixed += 1
    if fixed:
        await db.commit()
        log.info("[fix_elec_po_item_names] 修复 %d 张电工采购单的'项目'列(物料名称)", fixed)
    return {"fixed": fixed}


async def backfill_project_visibility_from_overview_names(db: AsyncSession) -> dict:
    """🆕 存量项目可见性补全（幂等）：解析项目目录 销售/电工/设计师 列(project.extra __o__)，
    把列中人名按系统用户 full_name 精确匹配，匹配到的 user_id 并入项目「可见名单」
    project.extra['__viz_uids__']，使这些人在项目目录/详单能看到自己经手的存量项目。

    口径（用户 2026-06-17 确认）：
    - 一格多名(顿号/逗号/分号/空格/斜杠分隔)→都授予
    - 匹配不到系统用户→跳过并记日志；同名多用户→跳过(歧义不乱授)并记日志
    - 销售列匹配到、且台账 sales_uid 为空时，顺带回填 sales_uid
    幂等：可见名单做并集(只增不减)，再次运行不重复授权。
    """
    import re as _re
    from .routers.projects_router import OVERVIEW_KEY_PREFIX

    ures = await db.execute(select(models.User))
    name_map: dict[str, list[int]] = {}
    for u in ures.scalars().all():
        nm = (u.full_name or "").strip()
        if nm:
            name_map.setdefault(nm, []).append(u.id)

    COLS = ["销售", "设计师", "电工"]
    SEP = _re.compile(r"[、，,;；/\s]+")
    res = await db.execute(select(models.Project).where(models.Project.is_deleted == False))  # noqa: E712
    projects = list(res.scalars().all())

    granted = 0
    proj_changed = 0
    sales_filled = 0
    unmatched: set[str] = set()
    ambiguous: set[str] = set()

    for p in projects:
        extra = dict(p.extra or {})
        cur_viz = set(extra.get("__viz_uids__") or [])
        new_viz = set(cur_viz)
        sales_uid_match = None
        for col in COLS:
            raw = extra.get(f"{OVERVIEW_KEY_PREFIX}{col}")
            if not raw or not isinstance(raw, str):
                continue
            for nm in SEP.split(raw.strip()):
                nm = nm.strip()
                if not nm or nm in ("—", "-", "/", "无"):
                    continue
                uids = name_map.get(nm)
                if not uids:
                    unmatched.add(nm); continue
                if len(uids) > 1:
                    ambiguous.add(nm); continue
                new_viz.add(uids[0])
                if col == "销售":
                    sales_uid_match = uids[0]
        if new_viz != cur_viz:
            extra["__viz_uids__"] = sorted(new_viz)
            p.extra = extra
            granted += len(new_viz - cur_viz)
            proj_changed += 1
        if sales_uid_match is not None:
            lres = await db.execute(select(models.SalesLedger).where(models.SalesLedger.project_id == p.id))
            led = lres.scalar_one_or_none()
            if led and not led.sales_uid:
                led.sales_uid = sales_uid_match
                sales_filled += 1

    if proj_changed or sales_filled:
        await db.commit()
    if unmatched:
        log.info("[viz_backfill] 未匹配系统用户的人名(跳过): %s", "、".join(sorted(unmatched))[:300])
    if ambiguous:
        log.info("[viz_backfill] 同名多用户(跳过歧义): %s", "、".join(sorted(ambiguous))[:200])
    log.info("[backfill_project_visibility_from_overview_names] 授权 %d 条 / 改 %d 项目 / 回填 sales_uid %d",
             granted, proj_changed, sales_filled)
    return {"granted": granted, "projects": proj_changed, "sales_filled": sales_filled}


async def backfill_user_roles(db: AsyncSession) -> dict:
    """🆕 多角色：给存量用户补 user_roles 关联（镜像其锚点 role_id）。

    新表 user_roles 由 create_all 建好；存量用户的角色此前只在 users.role_id。
    这里确保每个用户在 user_roles 至少有锚点角色一行，使 role_codes/role_ids
    与现状一致。幂等：已有 (user_id, role_id) 跳过。
    """
    res = await db.execute(select(models.User.id, models.User.role_id))
    user_role = [(uid, rid) for uid, rid in res.all() if rid is not None]
    if not user_role:
        return {"added": 0}
    res = await db.execute(select(models.UserRole.user_id, models.UserRole.role_id))
    existing = {(u, r) for u, r in res.all()}
    added = 0
    for uid, rid in user_role:
        if (uid, rid) not in existing:
            db.add(models.UserRole(user_id=uid, role_id=rid))
            added += 1
    if added:
        await db.commit()
        log.info("[backfill_user_roles] 补 %d 条用户角色关联（镜像锚点角色）", added)
    return {"added": added}


async def normalize_tax_rate_no_invoice(db: AsyncSession) -> dict:
    """🆕 税票口径统一(2026-06-18)：存量「不开票」由 "/" 改为 "0"。幂等。"""
    from sqlalchemy import update as _upd
    res = await db.execute(
        _upd(models.SalesLedger).where(models.SalesLedger.tax_rate == "/").values(tax_rate="0")
    )
    n = res.rowcount or 0
    if n:
        await db.commit()
        log.info("[normalize_tax_rate_no_invoice] 不开票税票 '/' → '0' 共 %d 行", n)
    return {"updated": n}


async def backfill_material_dict(db: AsyncSession) -> dict:
    """🆕 物料字典（幂等）：把「预置 ∪ 存量已用过的」类别/单位/材质并入 material_dict。
    存量物料用过的自定义值不丢；字典里已存在的值跳过。"""
    DEFAULT_CATS = ["标准件", "不锈钢", "激光", "外协", "电气", "耗材"]
    DEFAULT_UNITS = ["个", "件", "套", "米", "公斤", "张", "卷", "桶"]
    DEFAULT_GRADES = ["304不锈钢", "201不锈钢", "碳钢", "Q235", "铝合金", "45#钢"]   # 🆕 材质常见起始值，可在字典设置里增删
    er = await db.execute(select(models.MaterialDict.dtype, models.MaterialDict.value))
    existing = {(d, v) for d, v in er.all()}
    mr = await db.execute(select(models.WhMaterial.category, models.WhMaterial.unit, models.WhMaterial.material_grade))
    used_cats: set[str] = set()
    used_units: set[str] = set()
    used_grades: set[str] = set()
    for cat, unit, grade in mr.all():
        if cat and cat.strip():
            used_cats.add(cat.strip())
        if unit and unit.strip():
            used_units.add(unit.strip())
        if grade and grade.strip():
            used_grades.add(grade.strip())
    added = 0

    def _merge(dtype: str, defaults: list[str], used: set[str]) -> None:
        nonlocal added
        seq: list[str] = []
        for v in defaults:                       # 预置在前，保序
            if v not in seq:
                seq.append(v)
        for v in sorted(used):                   # 历史自定义值追加在后
            if v not in seq:
                seq.append(v)
        for i, v in enumerate(seq):
            if (dtype, v) not in existing:
                db.add(models.MaterialDict(dtype=dtype, value=v, sort_order=i, enabled=True))
                added += 1

    _merge("category", DEFAULT_CATS, used_cats)
    _merge("unit", DEFAULT_UNITS, used_units)
    _merge("material_grade", DEFAULT_GRADES, used_grades)
    if added:
        await db.commit()
        log.info("[backfill_material_dict] 并入物料字典 %d 条", added)
    return {"added": added}


async def backfill_supplier_category_dict(db: AsyncSession) -> dict:
    """🆕 供应商分类字典（幂等）：独立于物料类别字典（dtype='supplier_category'），
    两者取值语义不同（供应商分类描述"这家供应商是做什么的"，物料类别描述"这个物料是什么类型"，
    生产实际使用中已经分道扬镳——不能直接合并成一份下拉，否则互相污染）。
    把「预置 ∪ 存量已用过的」供应商分类并入这个独立字典，历史自定义值不丢。"""
    DEFAULT_CATS = ["外协", "标准件", "不锈钢", "激光", "电气", "运输"]
    er = await db.execute(select(models.MaterialDict.value).where(
        models.MaterialDict.dtype == "supplier_category"))
    existing = {v for (v,) in er.all()}
    sr = await db.execute(select(models.Supplier.category))
    used: set[str] = set()
    for (cat,) in sr.all():
        if cat and cat.strip():
            used.add(cat.strip())
    seq: list[str] = []
    for v in DEFAULT_CATS:
        if v not in seq:
            seq.append(v)
    for v in sorted(used):
        if v not in seq:
            seq.append(v)
    added = 0
    for i, v in enumerate(seq):
        if v not in existing:
            db.add(models.MaterialDict(dtype="supplier_category", value=v, sort_order=i, enabled=True))
            added += 1
    if added:
        await db.commit()
        log.info("[backfill_supplier_category_dict] 并入供应商分类字典 %d 条", added)
    return {"added": added}


async def backfill_payment_method_v2(db: AsyncSession) -> dict:
    """🆕 付款方式改为5个精确值（现金全款/对公全款/账期/现金预付/对公预付），替换旧的
    现金/对公转账/月结/承兑/预付。幂等：只对仍是旧值的行做一次性语义改写。
    「承兑」「预付」跟新5值没有干净的一一对应关系（承兑是单独的票据工具；预付不知道是现金还是对公），
    不强行瞎猜着改，保留原值——历史记录不会丢，只是下次编辑时需要人工挑一个新值。"""
    from sqlalchemy import update as _upd, func as _f
    CLEAN_MAP = {"现金": "现金全款", "对公转账": "对公全款", "月结": "账期"}
    updated = 0
    for old_val, new_val in CLEAN_MAP.items():
        r = await db.execute(_upd(models.PurchaseItem)
                              .where(models.PurchaseItem.payment_method == old_val)
                              .values(payment_method=new_val))
        updated += r.rowcount or 0
    left = await db.execute(select(_f.count(models.PurchaseItem.id)).where(
        models.PurchaseItem.payment_method.in_(["承兑", "预付"])))
    left_count = left.scalar() or 0
    if updated:
        await db.commit()
        log.info("[backfill_payment_method_v2] 改写 %d 条为新付款方式值", updated)
    if left_count:
        log.warning("[backfill_payment_method_v2] %d 条仍是「承兑/预付」旧值，无法自动对应新5值，保留原值待人工编辑", left_count)
    return {"updated": updated, "left_ambiguous": left_count}


async def backfill_auto_reconcile_invoice_status(db: AsyncSession) -> dict:
    """🆕 对账状态自动化规则补跑一次：现金全款/对公全款 + 已收货 + 已付清 的存量「待对账」明细，
    直接判定为「已对账」，跟新记录此后的自动规则保持一致。必须排在 backfill_payment_method_v2
    之后跑（付款方式先改写成新5值，这里才能按新值匹配到）。"""
    from sqlalchemy import update as _upd
    r = await db.execute(
        _upd(models.PurchaseItem)
        .where(
            models.PurchaseItem.invoice_status == "待对账",
            models.PurchaseItem.payment_method.in_(["现金全款", "对公全款"]),
            models.PurchaseItem.arrival_date.isnot(None),
            models.PurchaseItem.received_amount > 0,
            models.PurchaseItem.paid_amount >= models.PurchaseItem.received_amount - 0.005,
        )
        .values(invoice_status="已对账")
    )
    updated = r.rowcount or 0
    if updated:
        await db.commit()
        log.info("[backfill_auto_reconcile_invoice_status] 自动置已对账 %d 条", updated)
    return {"updated": updated}


async def backfill_oa_departments(db: AsyncSession) -> dict:
    """🆕 OA 部门字典默认值（幂等）：首次启动灌入常见部门；已存在则跳过，不覆盖管理层后续的改名/增删。
    lead_role 只给"有分组的部门"（普通员工/主管两个角色）设主管角色，让主管能看到本部门全部申请；
    人事/财务/物流这类单一角色部门直接把该角色设为 lead_role（该角色本身即视同"部门负责人"）。"""
    DEFAULTS = [
        ("销售部", "sales_lead"), ("采购部", "buyer_lead"), ("售后部", "as_lead"),
        ("设计部", "design_lead"), ("生产部", "pm_lead"), ("仓库", "warehouse_lead"),
        ("电工部", "electric_lead"), ("物流发货部", None), ("财务部", "finance"), ("人事部", "hr"),
    ]
    r = await db.execute(select(models.Department.name))
    existing = {n for (n,) in r.all()}
    added = 0
    for i, (name, lead_role) in enumerate(DEFAULTS):
        if name in existing:
            continue
        db.add(models.Department(name=name, lead_role=lead_role, sort_order=i, enabled=True))
        added += 1
    if added:
        await db.commit()
        log.info("[backfill_oa_departments] 新增默认部门 %d 个", added)
    return {"added": added}


async def backfill_oa_doc_types(db: AsyncSession) -> dict:
    """🆕 OA 单据类型字典默认值（幂等）：首次启动灌入业务方明确列出的8种单据类型。
    key 是历史申请/审批链配置引用的稳定标识，不因后续改名/重新排序而变。"""
    DEFAULTS = [
        ("trip", "business", "出差申请"), ("hospitality", "business", "招待申请"),
        ("company_car", "business", "公车申请"), ("private_car", "business", "私车公用申请"),
        ("other_biz", "business", "其他申请"),
        ("travel_expense", "reimbursement", "差旅费用报销"), ("expense", "reimbursement", "费用报销"),
        ("purchase", "purchase", "采购申请"),
    ]
    r = await db.execute(select(models.OaDocTypeDict.key))
    existing = {k for (k,) in r.all()}
    added = 0
    for i, (key, category, label) in enumerate(DEFAULTS):
        if key in existing:
            continue
        db.add(models.OaDocTypeDict(key=key, category=category, label=label, sort_order=i, enabled=True))
        added += 1
    if added:
        await db.commit()
        log.info("[backfill_oa_doc_types] 新增默认单据类型 %d 个", added)
    return {"added": added}


async def backfill_order_type_and_dept_orders(db: AsyncSession) -> dict:
    """🆕 2026-06-20（幂等）：给存量 SalesLedger 补 order_type（默认工厂制作订单，2026-008 为调货订单）。
    注：原"为进行中项目补建 design/electric/produce 任务单"已于 2026-06-23 停用——它每次启动都跑、
    会让管理层删除/作废的任务单复活成"待分派"，与删除语义冲突；现网项目均已派单，使命已完成。
    """
    from sqlalchemy import update as _upd

    # —— 1. order_type ——
    res = await db.execute(
        _upd(models.SalesLedger)
        .where(models.SalesLedger.order_type == None)   # noqa: E711
        .values(order_type="工厂制作订单")
    )
    type_set = res.rowcount or 0

    # 把 2026-008 单独改成调货订单
    res008 = await db.execute(
        select(models.Project).where(models.Project.code == "2026-008")
    )
    p008 = res008.scalar_one_or_none()
    if p008:
        await db.execute(
            _upd(models.SalesLedger)
            .where(models.SalesLedger.project_id == p008.id)
            .values(order_type="调货订单")
        )

    # —— 2.（已停用）补建部门任务单 ——
    # 🆕 2026-06-23 停用：此一次性回填原为"上线前存量老项目"补建 design/electric/produce 任务单，
    # 现网项目均已派单，使命已完成。且它每次启动都跑、按 (项目,部门) 缺失即补，会让管理层
    # 「删除/作废」的任务单在重启后复活成"待分派"，与删除/作废语义冲突，故停用。
    # 如个别老项目确需补单，请由销售/管理层手工下单。
    orders_created = 0

    if type_set:
        await db.commit()
    log.info(
        "[backfill_order_type_and_dept_orders] order_type 补 %d 行，dept_orders 补 %d 条",
        type_set, orders_created,
    )
    return {"order_type_set": type_set, "orders_created": orders_created}


async def backfill_v2_template_for_058_plus(db: AsyncSession) -> dict:
    """🆕 2026-07-01：对编号 >= 2026-058 的项目强制对齐到"最新6表模板"。

    背景：2026-06-24 模板更新（钣金装配 16列→12列，新增激光件清单）仅对新建项目生效；
    2026-058 及之后的项目为新版业务口径，均应采用 6 表模板（客户确认）。

    操作（幂等）：
    1. 缺少「激光件清单」→ 补建空表（按当前模板字段）
    2. 「钣金装配」仍为旧 16 列格式（LEGACY_SHEET_TEMPLATES）且无任何记录 → 清空字段后
       按当前 12 列模板重建；若已有数据则跳过（不丢数据）

    不处理：
    - 编号 < 2026-058 的项目（保留旧模板，客户旧项目不动）
    - 已删除项目
    - 标准件清单/外协加工/不锈钢原料下料单（与旧模板字段相同，无需变更）
    - 电工采购单（已由 backfill_elec_po_sheet 统一处理，本函数不重复）
    """
    from sqlalchemy import delete as sql_delete
    from .sheet_templates import SHEET_TEMPLATES, LEGACY_SHEET_TEMPLATES
    from .routers.projects_router import _create_sheet_with_fields

    # 找出 code >= "2026-058" 的活跃项目
    res = await db.execute(
        select(models.Project).where(
            models.Project.is_deleted == False,   # noqa: E712
            models.Project.code >= "2026-058",
        )
    )
    target_projects = list(res.scalars().all())
    if not target_projects:
        return {"laser_added": 0, "bangin_rebuilt": 0, "skipped_data": 0}

    target_pids = [p.id for p in target_projects]
    pid_to_code = {p.id: p.code for p in target_projects}

    # 取所有相关 datasheet
    res = await db.execute(
        select(models.Datasheet).where(models.Datasheet.project_id.in_(target_pids))
    )
    all_ds = list(res.scalars().all())
    ds_by_pid: dict[int, dict[str, models.Datasheet]] = {}
    for d in all_ds:
        ds_by_pid.setdefault(d.project_id, {})[d.name] = d

    laser_tpl = SHEET_TEMPLATES["激光件清单"]
    bangin_new_tpl = SHEET_TEMPLATES["钣金装配"]
    bangin_legacy_sets = [set(v) for v in LEGACY_SHEET_TEMPLATES.get("钣金装配", [])]

    laser_added = 0
    bangin_rebuilt = 0
    skipped_data = 0

    for pid in target_pids:
        sheets = ds_by_pid.get(pid, {})

        # —— 1. 补建激光件清单 ——
        if "激光件清单" not in sheets:
            # sort_order: 放在现有最后一张之后（或默认 4）
            max_order = max((d.sort_order for d in sheets.values()), default=3)
            await _create_sheet_with_fields(
                db, pid, "激光件清单", list(laser_tpl), max_order + 1
            )
            laser_added += 1
            log.info("[backfill_v2_template_058+] %s 补建激光件清单", pid_to_code[pid])

        # —— 2. 钣金装配旧格式（16列）→ 升级到新 12 列（无数据时） ——
        bangin = sheets.get("钣金装配")
        if bangin is None:
            continue
        # 取字段
        res = await db.execute(
            select(models.Field).where(models.Field.datasheet_id == bangin.id)
        )
        fields = list(res.scalars().all())
        field_set = {(f.name or "").strip() for f in fields}

        # 已经是新模板 → 跳过
        if field_set == set(bangin_new_tpl):
            continue
        # 不是历史格式 → 跳过（可能已是非标结构，不动）
        if not any(field_set == legacy_set for legacy_set in bangin_legacy_sets):
            continue

        # 检查是否有数据记录
        res = await db.execute(
            select(models.Record.id).where(
                models.Record.datasheet_id == bangin.id
            ).limit(1)
        )
        has_records = res.scalar_one_or_none() is not None
        if has_records:
            skipped_data += 1
            log.info(
                "[backfill_v2_template_058+] %s 钣金装配有存量数据，跳过格式升级",
                pid_to_code[pid],
            )
            continue

        # 无数据 → 清空字段 + 按新模板重建
        field_ids = [f.id for f in fields]
        if field_ids:
            await db.execute(sql_delete(models.FieldPermission).where(
                models.FieldPermission.field_id.in_(field_ids)
            ))
            await db.execute(sql_delete(models.Field).where(
                models.Field.id.in_(field_ids)
            ))
        for sort_i, tpl_name in enumerate(bangin_new_tpl):
            db.add(models.Field(
                datasheet_id=bangin.id,
                name=tpl_name,
                type=_tpl_field_type(tpl_name),
                sort_order=sort_i,
                config=_tpl_field_config(tpl_name),
            ))
        bangin_rebuilt += 1
        log.info(
            "[backfill_v2_template_058+] %s 钣金装配 16列旧格式→12列新格式",
            pid_to_code[pid],
        )

    if laser_added or bangin_rebuilt:
        await db.commit()
    log.info(
        "[backfill_v2_template_058+] 激光件清单补建 %d 个，钣金装配格式升级 %d 个，"
        "有数据跳过 %d 个",
        laser_added, bangin_rebuilt, skipped_data,
    )
    return {"laser_added": laser_added, "bangin_rebuilt": bangin_rebuilt,
            "skipped_data": skipped_data}


async def split_mixed_supplier_po(db: AsyncSession) -> int:
    """存量修复(#147/#148)：把「一个采购单号混了多个 (供应商/采购员/下单日期)」的自动采购单拆开。

    历史 `_next_po_no` 用 COUNT(DISTINCT) 算序号,删单留下空档后会算出一个已存在的号,
    新生成的单就并进老单里,导致同一 po_no 串了多个供应商(账目/PDF 抬头全乱)。
    - 只处理自动生成的 `TH{yyyymmdd}-NNN` 号;历史导入的自定义单号不动。
    - 按 (供应商, 采购员, 下单日期) 分组,一次正常生成=一个组;>1 组即为被误并,拆开。
    - 第一组(最早)保留原号,其余组各分一个「当日最大后缀+1」的新号,永不与现有号冲突。
    - 幂等:拆完每个 po_no 只剩一个组,再次运行不动任何行。
    """
    from collections import defaultdict
    r = await db.execute(
        select(models.PurchaseItem)
        .where(models.PurchaseItem.po_no.isnot(None))
        .order_by(models.PurchaseItem.id))
    items = list(r.scalars().all())
    by_po: dict[str, list] = defaultdict(list)
    for it in items:
        by_po[it.po_no].append(it)
    po_re = re.compile(r"^(TH\d{8}-)(\d+)$")
    max_seq: dict[str, int] = defaultdict(int)   # 每个日期前缀现有最大后缀
    for po in by_po:
        m = po_re.match(po or "")
        if m:
            max_seq[m.group(1)] = max(max_seq[m.group(1)], int(m.group(2)))
    fixed_groups = 0
    for po, its in list(by_po.items()):
        m = po_re.match(po or "")
        if not m:
            continue   # 非自动号(历史导入)不动
        prefix = m.group(1)
        groups: dict[tuple, list] = defaultdict(list)
        for it in its:
            groups[(it.supplier_id, it.buyer_id, it.delivery_date)].append(it)
        if len(groups) <= 1:
            continue
        for idx, (_key, gitems) in enumerate(groups.items()):
            if idx == 0:
                continue   # 第一组(最早)保留原号
            max_seq[prefix] += 1
            new_po = f"{prefix}{max_seq[prefix]:03d}"
            for it in gitems:
                it.po_no = new_po
            fixed_groups += 1
    if fixed_groups:
        await db.commit()
        log.info("[split_mixed_supplier_po] 拆分 %d 个被误并的采购单组", fixed_groups)
    return fixed_groups


async def run_all(db: AsyncSession) -> None:
    """启动时调用：依次跑所有迁移；任一失败只 warn 不阻塞启动。"""
    try:
        await backfill_user_roles(db)
    except Exception as e:
        log.warning("backfill_user_roles failed: %s", e)
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
        await rename_known_sheets_v3(db)   # 改名须在模板对齐/清理之前
    except Exception as e:
        log.warning("rename_known_sheets_v3 failed: %s", e)
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
    try:
        await backfill_datasheet_imported_at(db)
    except Exception as e:
        log.warning("backfill_datasheet_imported_at failed: %s", e)
    try:
        await backfill_sales_ledger(db)
    except Exception as e:
        log.warning("backfill_sales_ledger failed: %s", e)
    try:
        await backfill_shipments(db)
    except Exception as e:
        log.warning("backfill_shipments failed: %s", e)
    try:
        await dedupe_elec_po_sheets(db)   # 先去重,再补建
    except Exception as e:
        log.warning("dedupe_elec_po_sheets failed: %s", e)
    try:
        await backfill_elec_po_sheet(db)
    except Exception as e:
        log.warning("backfill_elec_po_sheet failed: %s", e)
    try:
        await backfill_elec_po_from_uploaded(db)   # 必须在补建第5表之后
    except Exception as e:
        log.warning("backfill_elec_po_from_uploaded failed: %s", e)
    try:
        await fix_elec_po_item_names(db)   # 🆕 #196 修'项目'列=项目编号的存量表
    except Exception as e:
        log.warning("fix_elec_po_item_names failed: %s", e)
    try:
        await add_location_column_to_known_sheets(db)   # 🆕 #195 存量进度表补「库位」列
    except Exception as e:
        log.warning("add_location_column_to_known_sheets failed: %s", e)
    try:
        await close_legacy_electric_done_orders(db)   # 🆕 #197 止损:卡在旧二步流的电工单置完成
    except Exception as e:
        log.warning("close_legacy_electric_done_orders failed: %s", e)
    try:
        await backfill_project_visibility_from_overview_names(db)
    except Exception as e:
        log.warning("backfill_project_visibility_from_overview_names failed: %s", e)
    try:
        await normalize_tax_rate_no_invoice(db)
    except Exception as e:
        log.warning("normalize_tax_rate_no_invoice failed: %s", e)
    try:
        await backfill_order_type_and_dept_orders(db)
    except Exception as e:
        log.warning("backfill_order_type_and_dept_orders failed: %s", e)
    try:
        await backfill_v2_template_for_058_plus(db)
    except Exception as e:
        log.warning("backfill_v2_template_for_058_plus failed: %s", e)
    try:
        await backfill_material_dict(db)
    except Exception as e:
        log.warning("backfill_material_dict failed: %s", e)
    try:
        await backfill_supplier_category_dict(db)
    except Exception as e:
        log.warning("backfill_supplier_category_dict failed: %s", e)
    try:
        await backfill_payment_method_v2(db)
    except Exception as e:
        log.warning("backfill_payment_method_v2 failed: %s", e)
    try:
        await backfill_auto_reconcile_invoice_status(db)
    except Exception as e:
        log.warning("backfill_auto_reconcile_invoice_status failed: %s", e)
    try:
        await backfill_oa_departments(db)
    except Exception as e:
        log.warning("backfill_oa_departments failed: %s", e)
    try:
        await backfill_oa_doc_types(db)
    except Exception as e:
        log.warning("backfill_oa_doc_types failed: %s", e)
    try:
        await split_mixed_supplier_po(db)
    except Exception as e:
        log.warning("split_mixed_supplier_po failed: %s", e)
