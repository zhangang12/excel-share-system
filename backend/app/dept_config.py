"""🆕 v3 部门工作台配置 —— 与原型 DEPTS 一致（增量UI设计图.html :192-201）。

三个执行部门（设计/电工/生产）的派单/接单/完成流配置：
- worker_role / lead_role：工人与负责人角色 code（对应 seed.ROLES）
- sheet_check：完成前置校验"四表已导入"（仅设计，D1 口径=有 Excel 导入记录 P-16）
- start_outputs：接单后上传并推送下游（设计→图纸包→采购+钣金；电工→采购清单→采购）
  🆕 #303：上传与推送分离——上传后附件 pushed=0(待推送)、下游不可见，点「推送」(start-push)才下发；
  例外：电工采购清单(plist)维持上传即推送
- outputs：完成时上传产物并推送下游（required=必传，如电工电路图）
  🆕 #294：电工电路图(circuit)前置——进行中即可上传，同 #303 口径待推送、点推送才下发物流(logistics)
- notify_pool：完成弹窗"通知人"候选角色（必选其一，企微/站内通知）
- 标签：start_label/end_label/done_label 供前端展示
"""

DEPTS: dict[str, dict] = {
    "design": {
        "name": "设计部",
        "worker_role": "designer",
        "lead_role": "design_lead",
        "sheet_check": True,
        # 🆕 2026-06-19：图纸包改为「CAD激光图纸」并推送采购部；新增「外购附图」也推采购部
        # 🆕 2026-07-22：CAD激光图纸(sheetpkg)推送时除采购外同步推钣金组(start_push 特判)，钣金组工作台图纸列同源可见
        # 🆕 #303：上传≠推送——上传后待推送(pushed=0)，点「推送」才下发对应 to_role 并推消息
        # 🆕 #324：to_domain=按采购员分工域(BUYER_SHEET_MAP)路由推送，只推该域采购员；
        #   域内无匹配活跃用户时回退原 to_role 池（防没人收到）。sheetpkg 同步推钣金组不变。
        "start_outputs": [
            {"k": "sheetpkg", "label": "CAD激光图纸", "to_role": "buyer", "to_domain": "laser"},
            {"k": "outsource_img", "label": "外购附图", "to_role": "buyer", "to_domain": "standard"},
            # 🆕 封板文件(机架图/横梁图)→推送封板组(sealing);封板组 tab 里可下载
            {"k": "sealing_pkg", "label": "封板文件(机架图/横梁图)", "to_role": "sealing"},
            # 🆕 #269 冷作图纸→推送钣金组(sheetmetal);钣金组 tab 里可下载
            {"k": "coldwork_pkg", "label": "冷作图纸", "to_role": "sheetmetal"},
            # 🆕 #304 钳工图纸→推送装配组(assembler)：钳工不搞独立组，就是装配组的人，图纸链路并入装配组
            {"k": "fitter_pkg", "label": "钳工图纸", "to_role": "assembler"},
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
            {"k": "plist", "label": "电器清单 (Excel)", "to_role": "buyer"},
        ],
        "outputs": [
            # 🆕 #294 电路图前置：进行中卡片即可上传（上传后待推送，点推送下发 logistics）；
            #   已完成 tab 发货准备保留补传/更换（同待推送口径）
            {"k": "circuit", "label": "电路图 (PDF)", "to_role": "logistics", "required": True},
        ],
        "notify_pool": "logistics",
        "notify_label": "完成后通知物流部人员",
        # 🆕 电工三步流（2026-08-12）：主板完成(结考核) → 电路完成(status=done,发货放行) → 上传电路图
        #   end_label「预计完成」对着的是**主板完成**——考核和预计日期一起走第一步。
        "start_label": "接线开始", "end_label": "预计完成（主板）", "done_label": "安装调试完成",
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

# 🆕 外协人员（按登录账号 username 配置，dept -> [username]）：
#   这些人的任务单在部门工作台单列一个「外协订单」tab，供负责人/管理层集中监控其订单状态；
#   **本人看不到该 tab**（他只看自己的「我的订单」三个 tab，与普通工人无差别）。
#   —— 改这里即可增减外协人员，不要把账号名写进路由/前端。
OUTSOURCE_WORKERS: dict[str, list[str]] = {
    "electric": ["zhourui"],
}

# 🆕 R4/A6：采购员按清单分工（username -> 负责的清单域集合）。
# 用途一（purchase_mgmt）：采购下单「按人分表」的可见性——仅限这三名采购员各管自己的清单，
#   其他采购员 + 采购主管 + admin/manager 不受限（看全部）。
# 用途二（🆕 #324，orders_router.start_push）：设计部图纸推送按 start_outputs[].to_domain
#   路由到负责该域的采购员（sheetpkg→laser=王芹域；outsource_img→standard=李新新域）。
BUYER_SHEET_MAP: dict[str, set[str]] = {
    "lixinxin": {"standard", "elec_po"},   # 李新新：标准件清单 + 电工采购单
    "wangqin": {"material", "laser"},       # 王芹：不锈钢原料下料单 + 激光件清单
    "fangbusen": {"outsource"},             # 方步森：外协加工
}


def compute_efficiency(start: str | None, due: str | None, done: str | None):
    """效率/按时口径（C1-C3，供完成流/逾期推送/报表共用单一实现）：
    - 自然日计（C1）
    - done == due 算按时（C2）
    - 预计/实际用时不足 1 天均按 1 天算（C3：避免除零，且当天完成不致 0%）
    - 🆕 完成效率% = round(预计天数 / 实际天数 * 100)，越高越好：
      100=按时，>100=提前完成（越快越高），<100=超期（越慢越低）
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
    planned = max((dd - ds).days, 1)   # C3：预计不足 1 天按 1 天
    actual = max((dn - ds).days, 1)    # 🆕 C3：实际不足 1 天按 1 天（当天完成=至少 100%，且避免除零）
    eff = round(planned / actual * 100)  # 🆕 越高越好：预计 ÷ 实际
    on_time = dn <= dd                 # C2
    overdue_days = max((dn - dd).days, 0)
    return eff, on_time, overdue_days
