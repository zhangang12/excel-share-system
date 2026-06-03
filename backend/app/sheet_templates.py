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

SHEET_TEMPLATES: dict[str, list[str]] = {
    '钣金装配': [
        '名称',
        '图纸名称',
        '采购负责人',
        '供应商',
        '编号',
        '发出日期',
        '到料日期',
        '钣金/钳工',
        '钣金发出日期',
        '钣金完成日期',
        '封板/抛光',
        '封板发出日期',
        '封板完成日期',
        '进度',
        '仓库负责人签字',
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
    ],
    '外协外购': [
        '名称',
        '图纸名称',
        '采购负责人',
        '供应商',
        '原料编号',
        '发出日期',
        '到料日期',
        '工艺1',
        '工艺1发出日期',
        '工艺1完成日期',
        '工艺2',
        '工艺2发出日期',
        '工艺2完成日期',
        '进度',
        '仓库负责人签字',
    ],
    '原料下料单': [
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
    ],
}


# 项目一览的固定列（"# 序号"由表格自动生成，不在此列表）
# 注意：一览与项目详情的数据"完全独立"——一览的"签订日期"存
# __h__签订日期，项目详情的"下单日期"存 __h__下单日期，互不影响。
# 这样改一处不会影响另一处。
OVERVIEW_FIELDS: list[dict] = [
    {'label': '项目编号',     'source': 'code',    'editable': False},
    {'label': '项目名称',     'source': 'name',    'editable': True},
    {'label': '签订日期',     'source': 'meta',    'editable': True},
    {'label': '交货日期',     'source': 'meta',    'editable': True},
    {'label': '销售',         'source': 'meta',    'editable': True},
    {'label': '设计师',       'source': 'meta',    'editable': True},
    {'label': '设图开始',     'source': 'meta',    'editable': True},
    {'label': '设图结束',     'source': 'meta',    'editable': True},
    {'label': '设图费时',     'source': 'derived', 'editable': False, 'derived': 'design_days'},
    {'label': '电工',         'source': 'meta',    'editable': True},
    {'label': '货期',         'source': 'derived', 'editable': False, 'derived': 'duration'},
    {'label': '已过时间',     'source': 'derived', 'editable': False, 'derived': 'elapsed'},
    {'label': '剩余货期时间', 'source': 'derived', 'editable': False, 'derived': 'remaining'},
    # 已完成项目专属（进行中为空）
    {'label': '完成日期',     'source': 'meta',    'editable': True},
    {'label': '实际用时',     'source': 'derived', 'editable': False, 'derived': 'actual_days'},
    {'label': '拖后时间',     'source': 'derived', 'editable': False, 'derived': 'delay_days'},
    {'label': '出货日期',     'source': 'meta',    'editable': True},
]


# 别名映射已下线：一览与项目详情数据各存各的，互不影响
OVERVIEW_HEADER_ALIAS: dict[str, str] = {}


def is_known_sheet(sheet_name: str) -> bool:
    """判断 sheet 名是否在模板里（已知类型）"""
    return sheet_name in SHEET_TEMPLATES


def map_excel_to_template(
    sheet_name: str,
    excel_headers: list[str],
    excel_rows: list[list],
) -> tuple[list[str], list[list]] | None:
    """对已知 sheet 类型，把 Excel 数据按模板字段名 + 位置映射重建。

    策略：
    - 跳过 Excel 的"序号"列（按名字识别）和完全空的列
    - 剩下的列按位置对应模板字段
    - 如果 Excel 有效列数比模板少，模板尾部字段在数据里设 None
    - 如果 Excel 有效列数比模板多，多出来的尾列丢弃（模板已经能装下所有业务字段）

    返回: (模板字段名列表, 转换后的数据行) 或 None（如果不是已知 sheet）
    """
    if sheet_name not in SHEET_TEMPLATES:
        return None

    template = SHEET_TEMPLATES[sheet_name]
    rownum_names = {'序号', '#', '序', '行号', 'No', 'No.'}

    # 找出 Excel 的"有效列"索引：去掉序号 + 空标头
    valid_idx: list[int] = []
    for i, h in enumerate(excel_headers):
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
