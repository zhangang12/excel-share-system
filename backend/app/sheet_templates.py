"""Excel 模板的固定字段定义 —— 项目模板和一览模板。

# 数据表 sheet 类型
SHEET_TEMPLATES 里的字段顺序就是导入后的字段顺序。Excel 文件里"序号"列
（自动行号）会被跳过，剩下的列按位置对应模板字段。

某些 Excel 列名重复（如"钣金/钳工"下面有"发出日期/完成日期"，"封板/抛光"
下面也有），这里把它们改成有上下文的名字（"钣金发出日期"、"封板发出日期"），
避免数据库里出现同名字段引起混淆。

# 项目一览
OVERVIEW_FIELDS 是一览页固定显示的列。
OVERVIEW_HEADER_ALIAS 把一览的列名（"签订日期/电工"）映射到项目头表的物理
key（"下单日期/电器"），让两套 UI 共享同一份数据。
"""
import re

SHEET_TEMPLATES: dict[str, list[str]] = {
    # 🆕 2026-06-24 模板更新（仅对新建项目生效）：钣金装配瘦身为纯工艺跟踪——
    # 去掉 采购负责人/供应商/发出日期/到料日期（采购信息拆到「激光件清单」），
    # 编号 提到图纸名称之后，仓库负责人签字→负责人签字。
    # 存量项目保持旧 16 列形态，见 LEGACY_SHEET_TEMPLATES（启动校验放行，不清空）。
    '钣金装配': [
        '名称',
        '图纸名称',
        '编号',
        '工艺1',
        '工艺1发出日期',
        '工艺1完成日期',
        '工艺2',
        '工艺2发出日期',
        '工艺2完成日期',
        '进度',
        '负责人签字',
        '备注',
        '库位',   # 🆕 #195 存放库位(末尾追加——导入按位置映射,不能插中间)
    ],
    '标准件清单': [
        '项目',
        '规格型号',
        '数量',
        '材质',
        '品牌',
        '采购负责人',
        '订购日期',
        '到货日期',
        '进度',
        '仓库签字',
        '备注',
        '库位',   # 🆕 #195
        '预计到货',   # 🆕 采购下单填预计到货日期(末尾追加——导入按位置映射,不能插中间)
    ],
    # 🆕 2026-06-18 按客户提供的 287 提升式压料机模板（sheet「外协加工」）收窄为 9 列，
    # 去掉原料编号/工艺1·2 各段日期，到料日期→到货日期、仓库负责人签字→仓库。
    # 🆕 2026-06-19 表名改为「外协加工」（原「外协外购」），存量由 rename_known_sheets_v3 迁移改名。
    '外协加工': [
        '名称',
        '图纸名称',
        '采购负责人',
        '供应商',
        '发出日期',
        '到货日期',
        '进度',
        '仓库',
        '备注',
        '库位',   # 🆕 #195
        '预计到货',   # 🆕 采购下单填预计到货日期
    ],
    # 🆕 2026-06-19 表名改为「不锈钢原料下料单」（原「原料下料单」），列不变。
    '不锈钢原料下料单': [
        '材料类别',
        '图纸名称',
        '规格型号',
        '材质',
        '数量',
        '供应商',
        '采购负责人',
        '下单日期',
        '到料日期',
        '进度',
        '仓库',
        '备注',
        '库位',   # 🆕 #195
        '预计到货',   # 🆕 采购下单填预计到货日期
    ],
    # 🆕 2026-06-24 新增「激光件清单」（仅对新建项目生效）：激光切割件的采购/到料跟踪
    # （即原钣金装配里拆出来的采购信息）。
    '激光件清单': [
        '名称',
        '图纸名称',
        '编号',
        '采购负责人',
        '供应商',
        '发出日期',
        '到料日期',
        '进度',
        '仓库',
        '备注',
        '库位',   # 🆕 #195
        '预计到货',   # 🆕 采购下单填预计到货日期
    ],
}


# 🆕 2026-06-24 历史模板（模板更新前的形态）。
# 口径：模板更新只对「新建项目」生效；存量项目对应表保持原形态——
# align_known_sheet_fields_to_template / cleanup_misaligned_known_sheets 同时认可
# 「当前模板」与这里的「历史模板」，避免启动校验把老项目清空或重排。
LEGACY_SHEET_TEMPLATES: dict[str, list[list[str]]] = {
    '钣金装配': [
        # 2026-06 模板更新前的 16 列版本
        ['名称', '图纸名称', '采购负责人', '供应商', '编号', '发出日期', '到料日期',
         '工艺1', '工艺1发出日期', '工艺1完成日期', '工艺2', '工艺2发出日期', '工艺2完成日期',
         '进度', '仓库负责人签字', '备注'],
        # 🆕 #195 16 列版本 + 库位（add_location_column_to_known_sheets 会给老表补列）
        ['名称', '图纸名称', '采购负责人', '供应商', '编号', '发出日期', '到料日期',
         '工艺1', '工艺1发出日期', '工艺1完成日期', '工艺2', '工艺2发出日期', '工艺2完成日期',
         '进度', '仓库负责人签字', '备注', '库位'],
        # 🆕 #195 12 列版本（加库位前的"当前模板"，补列迁移跑完即变为当前模板形态）
        ['名称', '图纸名称', '编号', '工艺1', '工艺1发出日期', '工艺1完成日期',
         '工艺2', '工艺2发出日期', '工艺2完成日期', '进度', '负责人签字', '备注'],
    ],
    # 🆕 #195 各表"加库位前"的形态：迁移补列期间/失败时不被 cleanup 误清
    '标准件清单': [
        ['项目', '规格型号', '数量', '材质', '品牌', '采购负责人', '订购日期',
         '到货日期', '进度', '仓库签字', '备注'],
        # 🆕 加「预计到货」前的形态（含库位）：存量补列迁移跑完即变为当前模板形态
        ['项目', '规格型号', '数量', '材质', '品牌', '采购负责人', '订购日期',
         '到货日期', '进度', '仓库签字', '备注', '库位'],
    ],
    '外协加工': [
        ['名称', '图纸名称', '采购负责人', '供应商', '发出日期', '到货日期', '进度', '仓库', '备注'],
        # 🆕 加「预计到货」前的形态（含库位）
        ['名称', '图纸名称', '采购负责人', '供应商', '发出日期', '到货日期', '进度', '仓库', '备注', '库位'],
    ],
    '不锈钢原料下料单': [
        ['材料类别', '图纸名称', '规格型号', '材质', '数量', '供应商', '采购负责人',
         '下单日期', '到料日期', '进度', '仓库', '备注'],
        # 🆕 加「预计到货」前的形态（含库位）
        ['材料类别', '图纸名称', '规格型号', '材质', '数量', '供应商', '采购负责人',
         '下单日期', '到料日期', '进度', '仓库', '备注', '库位'],
    ],
    '激光件清单': [
        ['名称', '图纸名称', '编号', '采购负责人', '供应商', '发出日期', '到料日期',
         '进度', '仓库', '备注'],
        # 🆕 加「预计到货」前的形态（含库位）
        ['名称', '图纸名称', '编号', '采购负责人', '供应商', '发出日期', '到料日期',
         '进度', '仓库', '备注', '库位'],
    ],
}


def resolve_sheet_template(sheet_name: str, field_names: set[str]) -> list[str] | None:
    """给定一张已知 sheet 及其当前字段名集合，返回它应对齐到的模板字段列表：
    - 字段集合与当前模板一致 → 当前模板
    - 否则与某历史模板一致 → 该历史模板（存量项目保形，不强转新版）
    - 否则 → 当前模板（兜底；由 cleanup 决定是否按当前模板清空重建）
    非已知 sheet 返回 None。"""
    cur = SHEET_TEMPLATES.get(sheet_name)
    if cur is None:
        return None
    if field_names == set(cur):
        return cur
    for variant in LEGACY_SHEET_TEMPLATES.get(sheet_name, []):
        if field_names == set(variant):
            return variant
    return cur


# 🆕 v3 M12：电工采购单 = 项目详单第 5 张表（§十六）。
# 刻意**不**放进 SHEET_TEMPLATES：
#   - 避免污染设计完成的"四表校验"（D1 只校验上面 4 张）
#   - 避免 cleanup_misaligned_known_sheets / align 误清这张含动态数据的表
#   - 避免被用户「导入 Excel」全量替换误删（excel_router 白名单保护）
# 来源：电工部接单后上传采购清单自动生成；采购负责人/订购日期/到货日期/仓库签字
# 由采购、仓库后续在详单内补充（FieldPermission 控列级编辑）。
ELEC_PO_SHEET_NAME = "电工采购单"
ELEC_PO_COLUMNS: list[str] = [
    '项目', '规格型号', '数量', '品牌', '采购负责人',
    '订购日期', '到货日期', '进度', '仓库签字', '备注', '库位',   # 🆕 #195
    '预计到货',   # 🆕 采购下单填预计到货日期
]

# 🆕 v3 §十七 装配前置三表：完成情况汇总到装配组工作台（done_flag 标记）
ASSEMBLY_PRECHECK_SHEETS: list[str] = ['钣金装配', '标准件清单', '外协加工']


# 项目一览的固定列（"# 序号"由表格自动生成，不在此列表）
# 一览与项目详情数据"完全独立"——一览的 meta 列存 __o__<label>，
# 项目详情存 __h__<label>，互不影响。
#
# 🆕 v3 hidden=True：逻辑删除列（§十二.19/P-19 口径：UI 不展示、无开关、业务无感知）。
# 数据链路完整保留——__o__制图* key 照常存取、设计任务接单/完成仍回写；
# 业务反悔把 hidden 改回 False 即恢复展示（这是保留定义而非删除条目的价值）。
OVERVIEW_FIELDS: list[dict] = [
    {'label': '项目编号',     'source': 'code',    'editable': False},
    {'label': '项目名称',     'source': 'name',    'editable': True},
    {'label': '数量',         'source': 'meta',    'editable': True},
    {'label': '状态',         'source': 'status',  'editable': True},
    {'label': '销售',         'source': 'meta',    'editable': True},
    {'label': '签订日期',     'source': 'meta',    'editable': True},
    {'label': '交货日期',     'source': 'meta',    'editable': True},
    {'label': '设计师',       'source': 'meta',    'editable': True},
    {'label': '制图开始',     'source': 'meta',    'editable': True,  'hidden': True},
    {'label': '制图结束',     'source': 'meta',    'editable': True,  'hidden': True},
    {'label': '制图用时',     'source': 'derived', 'editable': False, 'derived': 'design_days', 'hidden': True},
    {'label': '电工',         'source': 'meta',    'editable': True},
    {'label': '货期',         'source': 'derived', 'editable': False, 'derived': 'duration'},
    {'label': '已过时间',     'source': 'derived', 'editable': False, 'derived': 'elapsed'},
    {'label': '剩余制作时间', 'source': 'derived', 'editable': False, 'derived': 'remaining'},
]

# 🆕 逻辑删除列名集合（导出/展示层统一引用，避免散落硬编码）
HIDDEN_OVERVIEW_LABELS: set[str] = {
    f['label'] for f in OVERVIEW_FIELDS if f.get('hidden')
}


# 一览字段 → 项目详情头表 key 的映射。
# 一览写入时除了写 __o__<label>，会**同时**写一份到 __h__<alias>，
# 这样项目详情头表的"销售/设计师/电器/下单日期/交货日期"能自动同步。
# 反向（项目详情改字段）不会回写一览，避免互相覆盖。
#
# 只对"项目详情头表存在的 7 个 meta 字段"做映射：
# 数量 / 制表日期 / 销售 / 设计师 / 电器 / 下单日期 / 交货日期
OVERVIEW_HEADER_ALIAS: dict[str, str] = {
    '签订日期': '下单日期',
    '电工':    '电器',
    '销售':    '销售',
    '设计师':  '设计师',
    '交货日期': '交货日期',
    '数量':    '数量',
}


# ===== 日期字段统一规范化（YYYY-MM-DD） =====
# 一览/项目头里这些字段名视为"日期型"，录入和存量都规范化为 YYYY-MM-DD。
# 不以"日期"结尾但本质是日期的列（制图开始/制图结束），在 _DATE_FIELD_EXTRA 显式列出。
_DATE_FIELD_EXTRA = {'制图开始', '制图结束'}
_DATE_RE = re.compile(r'^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})')
_DATE_RE_COMPACT = re.compile(r'^(\d{4})(\d{2})(\d{2})$')  # 20260408 这种无分隔符


def is_date_field(label: str) -> bool:
    """字段名是否为日期型（需要规范化为 YYYY-MM-DD）。"""
    s = (label or '').strip()
    return s.endswith('日期') or s in _DATE_FIELD_EXTRA


def normalize_date_str(s) -> str:
    """把松散日期规范化为 YYYY-MM-DD；解析不了就原样返回（保留"待定"等文本）。

    支持：2026-6-4 / 2026/5/12 / 2026.5.12 / 2026年5月12日 / 20260408（8 位无分隔符）
    / Excel 日期序列号（5 位，如 45390）。
    幂等：已是 YYYY-MM-DD 的值规范化后不变。前端 normalizeDate 用同一套规则。
    """
    if s is None:
        return ''
    raw = str(s).strip()
    if not raw:
        return ''
    m = _DATE_RE.match(raw) or _DATE_RE_COMPACT.match(raw)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    elif raw.isdigit() and len(raw) == 5 and 30000 <= int(raw) <= 60000:
        # Excel 日期序列号（基准 1899-12-30，已含 1900 闰年补偿）
        from datetime import date as _date, timedelta as _td
        dt = _date(1899, 12, 30) + _td(days=int(raw))
        y, mo, d = dt.year, dt.month, dt.day
    else:
        return raw
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return raw
    return f"{y:04d}-{mo:02d}-{d:02d}"


def is_known_sheet(sheet_name: str) -> bool:
    """判断 sheet 名是否在模板里（已知类型）"""
    return sheet_name in SHEET_TEMPLATES


def map_excel_to_template(
    sheet_name: str,
    excel_headers: list[str],
    excel_rows: list[list],
    header_merge_skip_cols: set[int] | None = None,
) -> tuple[list[str], list[list]] | None:
    """对已知 sheet 类型，把 Excel 数据按模板字段名 + 位置映射重建。

    策略：
    - 跳过 Excel 的"序号"列（按名字识别）
    - 跳过空标头列
    - 跳过"表头合并的延续列"（即表头行合并范围内非左上角的列）
      —— 用户场景：如 F、G 列合并显示"品牌"，G 列虽然空但也属于
      品牌列；如果旧 bug 给 G 列填了脏数据，跳过它能避免位置错位
    - 剩下的列按位置对应模板字段
    - 如果 Excel 有效列数比模板少，模板尾部字段在数据里设 None
    - 如果 Excel 有效列数比模板多，多出来的尾列丢弃

    参数:
        header_merge_skip_cols: 表头行合并的"延续列"索引集合（0-indexed），
            这些列在 Excel 中视觉上属于左侧的合并大列，本身不算单独的字段。
            传 None 等价于空集（向后兼容）。

    返回: (模板字段名列表, 转换后的数据行) 或 None（如果不是已知 sheet）
    """
    if sheet_name not in SHEET_TEMPLATES:
        return None

    template = SHEET_TEMPLATES[sheet_name]
    rownum_names = {'序号', '#', '序', '行号', 'No', 'No.'}
    skip = set(header_merge_skip_cols or set())

    # 找出 Excel 的"有效列"索引：去掉序号 + 空标头 + 合并延续列
    valid_idx: list[int] = []
    for i, h in enumerate(excel_headers):
        if i in skip:
            continue
        name = str(h or '').strip()
        if not name:
            continue
        if name in rownum_names:
            continue
        valid_idx.append(i)

    # 按位置映射模板字段到 Excel 列
    new_rows: list[list] = []
    for r in excel_rows:
        row = []
        for j in range(len(template)):
            if j < len(valid_idx):
                ex_idx = valid_idx[j]
                row.append(r[ex_idx] if ex_idx < len(r) else None)
            else:
                row.append(None)
        new_rows.append(row)

    return list(template), new_rows
