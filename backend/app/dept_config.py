"""🆕 v3 部门工作台配置 —— 与原型 DEPTS 一致（增量UI设计图.html :192-201）。

三个执行部门（设计/电工/生产）的派单/接单/完成流配置：
- worker_role / lead_role：工人与负责人角色 code（对应 seed.ROLES）
- sheet_check：完成前置校验"四表已导入"（仅设计，D1 口径=有 Excel 导入记录 P-16）
- start_outputs：接单后上传并推送下游（设计→图纸包→钣金；电工→采购清单→采购）
- outputs：完成时上传产物并推送下游（required=必传，如电工电路图）
- notify_pool：完成弹窗"通知人"候选角色（必选其一，企微/站内通知）
- 标签：start_label/end_label/done_label 供前端展示
"""

DEPTS: dict[str, dict] = {
    "design": {
        "name": "设计部",
        "worker_role": "designer",
        "lead_role": "design_lead",
        "sheet_check": True,
        "start_outputs": [
            {"k": "sheetpkg", "label": "PDF 图纸包", "to_role": "sheetmetal"},
        ],
        "outputs": [
            {"k": "manual",    "label": "说明书 (Word)", "to_role": "logistics", "required": False},
            {"k": "nameplate", "label": "铭牌 (CAD)",    "to_role": "logistics", "required": False},
        ],
        "notify_pool": "logistics",
        "notify_label": "完成后通知物流部人员",
        "start_label": "制图开始", "end_label": "预计完成", "done_label": "制图完成",
        # 接单/完成回写一览列（OVERVIEW_HEADER_ALIAS 自动双写 __h__）
        "writeback_worker": "设计师", "writeback_start": "制图开始", "writeback_done": "制图结束",
    },
    "electric": {
        "name": "电工部",
        "worker_role": "electrician",
        "lead_role": "electric_lead",
        "sheet_check": False,
        "start_outputs": [
            {"k": "plist", "label": "采购清单 (Excel)", "to_role": "buyer"},
        ],
        "outputs": [
            {"k": "circuit", "label": "电路图 (PDF)", "to_role": "logistics", "required": True},
        ],
        "notify_pool": "buyer",
        "notify_label": "完成后通知采购部人员",
        "start_label": "接线开始", "end_label": "预计完成", "done_label": "接线完成",
        "writeback_worker": "电工", "writeback_start": None, "writeback_done": None,
    },
    "produce": {
        "name": "生产部",
        "worker_role": "assembler",
        "lead_role": "pm_lead",
        "sheet_check": False,
        "start_outputs": [],
        "outputs": [],  # E3：生产无产物，完成只是状态信号
        "notify_pool": "logistics",
        "notify_label": "完成后通知物流部人员",
        "start_label": "生产开始", "end_label": "预计完成", "done_label": "生产完成",
        "writeback_worker": None, "writeback_start": None, "writeback_done": None,
    },
}

# 任务状态枚举（入库存英文；中文映射由前端做）
ORDER_STATUS = ("pending_assign", "assigned", "in_progress", "done", "voided")


def compute_efficiency(start: str | None, due: str | None, done: str | None):
    """效率/按时口径（C1-C3，供完成流/逾期推送/报表共用单一实现）：
    - 自然日计（C1）
    - done == due 算按时（C2）
    - 预计 0 天按 1 天算（C3）
    - 效率% = round(实际天数 / max(预计天数,1) * 100)，≤100 为按时高效
    返回 (eff_pct | None, on_time | None, overdue_days)
    """
    from datetime import date

    def _p(s):
        try:
            y, m, d = str(s).split("-")
            return date(int(y), int(m), int(d))
        except Exception:
            return None

    ds, dd, dn = _p(start), _p(due), _p(done)
    if not ds or not dd or not dn:
        return None, None, 0
    planned = max((dd - ds).days, 1)   # C3
    actual = max((dn - ds).days, 0)
    eff = round(actual / planned * 100)
    on_time = dn <= dd                 # C2
    overdue_days = max((dn - dd).days, 0)
    return eff, on_time, overdue_days
