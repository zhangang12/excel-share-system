# Agent 工具 vs 可用数据源 · 差距分析

> 生成时间：2026-07-26
> 基准代码：`backend/app/routers/agent_router.py` @ HEAD
> 范围：仅分析**只读查询**（Agent 红线）；所有统计口径以 `tools` 层实际 SELECT 为准

---

## 1. 现有工具（7 个）· 精确字段与数据源

### 1.1 `morning_report` — 晨报聚合
- **数据源**：复用 `tool_po_arrival_overdue` + `tool_overdue_orders` + `tool_balance_due` + `_hr_due_rows`
- **返回**：四段各 Top5 + 总数，today 日期
- **字段**：
  - `po_arrival_overdue` → count + top[item_name, po_no, supplier, project_code, expected_arrival, over_days]
  - `overdue_orders` → count + top[dept, dept_name, project_code, worker, due_date, over_days]
  - `balance_due` → count + top[project_code, customer, balance, balance_date, days, sales]
  - `hr_due` → count + top[kind, name, dept, date, days]

### 1.2 `po_arrival_overdue` — 采购到期未到货
- **表**：`purchase_items + suppliers + users`（JOIN 由 ORM relationship 触发）
- **过滤**：`expected_arrival <= today` AND `arrival_date` IS NULL/空
- **返回**：count + items（最多 20 条）
- **字段**：`item_name, po_no, supplier, project_code, buyer, expected_arrival, over_days`

### 1.3 `po_arriving` — 未来 N 天预计到货
- **表**：同上
- **过滤**：`expected_arrival` BETWEEN `today` AND `today+N` days AND `arrival_date` 为空
- **返回**：count + days + items（最多 20 条）
- **字段**：`item_name, po_no, supplier, project_code, expected_arrival, in_days`

### 1.4 `po_overdue_by_supplier` — 未到货·按供应商汇总
- **数据源**：调用 `tool_po_arrival_overdue` 后客户端聚合（不另查库）
- **返回**：count + item_total + suppliers（最多 20 条）
- **字段**：`supplier, count, max_over_days, projects[]`

### 1.5 `balance_due` — 尾款到期/逾期清单
- **表**：`sales_ledger + projects + users`（通过 ORM relationship）
- **过滤**：`balance > 0` AND `balance_date` 非空且 `<= today+14天`
- **返回**：count + items（最多 20 条）
- **字段**：`project_code, project_name, customer, balance, balance_date, days`(负数=已逾期), `sales`

### 1.6 `overdue_orders` — 部门逾期任务
- **表**：`dept_orders + projects + users`（通过 ORM relationship）
- **过滤**：`status == "in_progress"` AND `due_date < today`，可选 `dept` 限定 design/electric/produce
- **返回**：count + items（最多 20 条）
- **字段**：`dept, dept_name, project_code, worker, due_date, over_days`

### 1.7 `project_status` — 项目进度查询（按编号）
- **表**：`projects + dept_orders + purchase_items + suppliers + sales_ledger`（多次 SELECT）
- **过滤**：按 `project.code` 精确匹配；采购只取未收货行；台账取一行
- **返回**：
  - 项目概要：`found, code, name, status, is_deleted, manager`
  - 部门任务列表：`dept_name, status, worker, start_date, due_date, done_date`
  - 采购待收货：`po_pending_count` + 明细 `item_name, po_no, supplier, expected_arrival, over_days`（最多 10 条）
  - 台账：`customer, amount, prepay, before_ship, balance, balance_date, balance_days, sales`

---

## 2. 数据域清单

### 2.1 销售域 (Sales)

**表**：
| 表名 | 关键字段 | 说明 |
|------|---------|------|
| `sales_ledger` | project_id, customer, cust_type, contract, amount, tax_rate, invoice_state, order_state, void_state, prepay, before_ship, balance, balance_date, ship_date, order_type, sales_uid | 一项目一行的台账（29 列） |
| `sales_leads` | source, customer, contact, phone, requirement, owner_uid, status, follow_log, lost_reason | 销售线索池 |
| `shipments` | project_id, status, receiver_name, receiver_company, receiver_phone, receiver_addr, freight_cost, freight_payer, packlist_status | 发货单 |

**已有 GET 端点**（12 GET）：`/ledger`, `/customers`, `/equipment-names`, `/next-code`, `/salespeople`, `/receiver-by-code`, `/receiver-by-customer`, `/order-approvals`, `/invoice-approvals`, `/void-approvals`, `/ledger/{lid}/receiver`, `/ledger/export`

**用户会问的自然语言问题**：
- "哪些项目还没签合同？"
- "这个月签了多少合同金额？"
- "还有哪些应收款没收回来（按客户/按销售员）？"
- "开票到了哪一步？有多少等待开票的项目？"
- "销售线索池有多少报价中的线索？成交率是多少？"
- "哪些项目货款已结清（尾款已到账）？"
- "显示 XX 客户的所有历史订单"

**Agent 工具现状**：`tool_balance_due` 覆盖尾款逾期；`tool_project_status` 覆盖单项目台账。**缺失**：线索池统计、合同统计、开票状态、应收款全貌、按客户/销售员聚合。

---

### 2.2 设计/电工/生产域 (Design / Electric / Produce)

**表**：
| 表名 | 关键字段 | 说明 |
|------|---------|------|
| `dept_orders` | project_id, dept, status, worker_id, due_date, done_date, design_done_flag, electric_done_flag, ship_prep_done | 各执行部门任务单 |
| `produce_group_tasks` | order_id, project_id, group(sheetmetal/assembly), status, worker_id, due_date | 生产分组派发（钣金/装配） |
| `feedback` | project_id, content, status, created_by, designer_uid | 生产问题反馈流 |
| `revision_requests` | project_id, order_id, dept, reason, status | 技术资料修订意见 |

**已有 GET 端点**：
- `orders_router`: `/options`, `/push-state`（2 GET）
- `produce_router`: `/sheetmetal-projects`, `/assembly-projects`, `/sealing-projects`, `/dispatch-options`（4 GET）
- `collab_router`: `/projects/{pid}/workflow`, `/assembly/sheet-status`（2 GET）
- `feedback_router`: `/projects`（1 GET）

**用户会问的自然语言问题**：
- "设计部当前有多少进行中的任务？哪些逾期了？" → 已覆盖 `overdue_orders`
- "电工组还有多少未完成的任务？谁最忙？"
- "钣金组这周完成了多少？装配组在做什么？"
- "哪些项目的生产任务还没分派？"
- "生产反馈里还有多少未处理的？"
- "最近有哪些技术资料修订意见没处理？"
- "列出所有超期 3 天以上的生产任务"

**Agent 工具现状**：`tool_overdue_orders` 覆盖逾期（三种部门），**缺少**：按部门/组统计进行中任务数、按工人负载统计、生产反馈/修订意见未处理计数。

---

### 2.3 采购域 (Procurement)

**表**：
| 表名 | 关键字段 | 说明 |
|------|---------|------|
| `suppliers` | name, code, category, contact, phone, settlement_type, credit_days, created_by | 供应商档案 |
| `purchase_items` | po_no, supplier_id, project_code, item_name, qty, unit_price, delivery_date, contract_no, expected_arrival, arrival_date, invoice_no, invoice_amount, paid_amount, received_amount, buyer_id | 采购明细（37 列） |
| `purchase_requests` | requester_id, buyer_id, status | 仓库采购申请 |
| `purchase_request_lines` | request_id, item_name, spec, qty, project_code | 采购申请明细 |
| `payment_requests` | supplier_id, requested_amount, status, requester_id, paid_amount, paid_date, payment_method, reject_reason | 请款单 |
| `payment_request_items` | request_id, item_id, allocated_amount | 请款↔采购明细关联 |

**已有 GET 端点**（25 GET）：`/items`, `/items/summary`, `/items/{iid}/receipts`, `/orders/{po_no}`, `/orders/{po_no}/pdf`, `/buyers`, `/custom-fields`, `/purchasable/{project_id}`, `/purchase-requests`, `/purchase-requests/{prid}/pdf`, `/payment-requests`, `/receiving`, `/reports/by-buyer`, `/reports/by-project` 等

**用户会问的自然语言问题**：
- "哪些物料到了但还没开发票？"
- "哪些物料已开发票但还没付款？"
- "XX 项目一共采购了多少钱？还没到货的还有多少钱？"
- "XX 供应商欠了多少发票？"
- "这个月采购了多少物料？按供应商汇总？"
- "请款审批里还有多少待处理的？"
- "库存备货申请有多少在等待采购？"
- "预计这周到货的总额是多少？"

**Agent 工具现状**：`tool_po_arrival_overdue` + `tool_po_arriving` + `tool_po_overdue_by_supplier` 覆盖到货时间线；`tool_project_status` 覆盖单项目采购待收货。**丰富但不完整**：**缺** 付款进度、发票欠票、采购申请待处理、请款审批待办。

---

### 2.4 仓库域 (Warehouse)

**表**：
| 表名 | 关键字段 | 说明 |
|------|---------|------|
| `wh_materials` | code, name, spec, category, material_grade, unit, unit_price, location, safety_stock, init_stock, status | 物料主数据 |
| `wh_txns` | material_id, biz_date, direction, qty, unit_price, amount, source, party, project_id, ref_no, is_reversal | 出入库流水 |
| `material_categories` | parent_id, level, seg_code, name | 物料分类树 |
| `wh_locations` | name, note | 库位 |
| `material_dict` | dtype, value | 物料字典（材质/类别下拉） |
| `supplier_opening_balances` | supplier_id, balance_date, outstanding_amount | 供应商期初余额 |

**已有 GET 端点**（14 GET）：`/materials`, `/materials/suggest`, `/material-categories`, `/material-custom-fields`, `/material-dict`, `/locations`, `/txns`, `/summary`, `/inventory-value`, `/demand-overview`, `/demand/{project_id}`, `/project-cost`, `/ship-list/pending`, `/ship-list/{project_id}`

**用户会问的自然语言问题**：
- "XX 物料库存还有多少？仓库里最贵的物料是什么？"
- "哪些物料库存低于安全库存（要补货了）？"
- "这个月入库了多少物料？出库了多少？"
- "XX 项目的领料成本是多少？"
- "最近一周有哪些收货记录？"
- "仓库物料总价值是多少？按大类分别多少？"

**Agent 工具现状**：**完全缺失**。仓库是系统中最独立且数据丰富的领域，目前没有任何 Agent 工具覆盖。

---

### 2.5 物流域 (Logistics)

**表**：
| 表名 | 关键字段 | 说明 |
|------|---------|------|
| `shipments` | project_id, status, receiver_name, receiver_company, receiver_phone, receiver_addr, freight_cost, freight_payer, shipped_at, packlist_status | 发货单 |
| `attachments` | biz_type, biz_id, kind, project_id, name, pushed | 发货文件（ship_doc 等） |

**已有 GET 端点**（3 GET）：`/board`, `/pending-count`, `/receiver-by-code`

**用户会问的自然语言问题**：
- "现在有多少待发货的项目？"
- "XX 项目的物流状态是什么？发货了没？收货人是谁？"
- "这个月发了多少货？运费总计多少？"
- "货物到哪了（物流跟踪）？" — 系统暂不跟踪在途，只能查已发/未发

**Agent 工具现状**：**完全缺失**。发货看板的核心数据（待发货数、运费、收货人）虽然有 GET 端点但没有 Agent 工具。

---

### 2.6 财务域 (Finance)

**表**：
| 表名 | 关键字段 | 说明 |
|------|---------|------|
| `sales_ledger` | invoice_state, amount, prepay, before_ship, balance, balance_date | 应收款/开票状态 |
| `payment_requests` | supplier_id, requested_amount, status, paid_amount, paid_date | 请款审批 |
| `purchase_items` | invoice_no, invoice_amount, paid_amount, unit_price | 发票/付款 |
| `after_sales` | project_id, kind, cost, status | 售后费用 |
| `payroll_monthly` | month, department_id, total_amount | 部门月度工资（人工分摊） |
| `wh_txns` | amount, source, direction | 物料成本（采购入库金额、领料金额） |

**已有 GET 端点**（5 GET）：`/pending-invoices`, `/invoiced`, `/payment-requests`, `/aftersales`, `/expense-overview`

**用户会问的自然语言问题**：
- "公司这个月支出了多少？在哪里支出的？"
- "还有多少开票申请等待财务处理？"
- "XX 供应商一共付了多少钱？今年付了多少？"
- "费用总览：按类别人工/采购/售后/运费分别列"
- "项目毛利率 = 合同金额 − 各项成本 = 多少？"
- "这个月的现金流量（收款 vs 付款）多少？"

**Agent 工具现状**：`tool_balance_due` 覆盖尾款逾期；`tool_project_status` 有单项目台账的金额。**严重缺失**：费用支出总览、毛利率、现金流、请款/开票待办都没有。

---

### 2.7 人事域 (HR)

**表**：
| 表名 | 关键字段 | 说明 |
|------|---------|------|
| `employees` | emp_no, name, department_id, position, hire_date, regular_date, contract_end, status, leave_date, user_id | 花名册 |
| `departments` | name, lead_role | 组织架构 |
| `payroll_monthly` | month, department_id, total_amount | 部门月度工资总额 |
| `attendance_monthly` | employee_id, period, should_days, actual_days, leave_days, overtime_hours, late/early_leave/missing_card count | 考勤 |
| `employee_salary_monthly` | employee_id, period, base, merit, overtime_pay, allowance, social_deduct, personal_tax, other_deduct | 个人工资 |

**已有 GET 端点**（9 GET）：`/employees`, `/attendance`, `/payroll`, `/payroll-summary`, `/salary`, `/bindable-users` + import templates

**用户会问的自然语言问题**：
- "公司在职多少人？各部门多少人？"
- "这个月工资总额多少？哪个部门最高？"
- "最近有哪些员工合同要到期了？试用期要转正的？"
- "这个月考勤最好/最差的是谁？"
- "XX 部门今年累计工资总额多少？"

**Agent 工具现状**：晨报含 `hr_due`（合同到期 / 试用期转正的提醒），但不作为独立工具暴露。**缺失**：在职人数统计、工资总额、考勤汇总、按部门聚合。

---

### 2.8 OA 审批域 (Office Automation)

**表**：
| 表名 | 关键字段 | 说明 |
|------|---------|------|
| `oa_requests` | request_no, category, doc_type, department_id, requester_id, title, amount, status, current_step_order, settle_amount | 申请单 |
| `oa_request_steps` | request_id, step_order, approver_role, step_label, status, acted_by | 审批步骤快照 |
| `oa_request_cc` | request_id, user_id | 申请抄送 |
| `oa_doc_types` | key, category, label | 单据类型字典 |
| `oa_approval_steps` | department_id, doc_type, step_order, approver_role | 审批链配置 |
| `oa_flow_cc` | department_id, doc_type, role_code | 固定抄送规则 |

**已有 GET 端点**（10 GET）：`/requests`, `/requests/{rid}`, `/chains`, `/chains/overview`, `/departments`, `/doc-types`, `/flow-cc`, `/reports/summary`, `/reports/summary/detail`, `/cc-candidates`

**用户会问的自然语言问题**：
- "我有多少待审批的申请？它们卡在哪一步了？"
- "上个月有多少报销申请？总金额多少？"
- "XX 申请书现在审批到谁了？"
- "哪些申请已经超过 3 天没处理了？"

**Agent 工具现状**：**完全缺失**。OA 数据很丰富且有统计报告端点，但没有任何 Agent 工具暴露。

---

### 2.9 售后域 (After-Sales)

**表**：
| 表名 | 关键字段 | 说明 |
|------|---------|------|
| `after_sales` | project_id, project_name, kind, problem, cost, status | 售后/安装登记 |

**已有 GET 端点**：`aftersales_router: /projects`（1 GET）；`finance_router: /aftersales`（财务视角）

**用户会问的自然语言问题**：
- "最近 3 个月有多少售后单？分安装和售后各多少？"
- "售后总花费多少？哪个项目售后最多？"
- "还有多少售后单等待审批？"

**Agent 工具现状**：**完全缺失**。

---

### 2.10 管理层待办域 (Management Todo)

**表**：
| 表名 | 关键字段 | 说明 |
|------|---------|------|
| `management_todos` | title, content, priority, due_date, created_by | 待办事项 |
| `management_todo_targets` | todo_id, user_id, status, committed_at, progress, done_at, extend_status, extend_to | 收件人各自的处理态 |

**已有 GET 端点**（3 GET）：`/mine`, `/mine/count`, `/sent`

**用户会问的自然语言问题**：
- "XX 下属还有多少待办没完成？谁逾期了？"
- "我这周需要完成什么待办？"
- "还有多少没回复承诺时间的？"

**Agent 工具现状**：**完全缺失**（晨报也不含管理层待办逾期）。

---

### 2.11 消息/通知域 (Messages)

**表**：
| 表名 | 关键字段 | 说明 |
|------|---------|------|
| `messages` | to_user_id, kind, text, read, biz_type, biz_id, created_at | 站内消息 |

**已有 GET 端点**：`/unread-count`（1 GET via messages_router）

**用户会问的自然语言问题**：
- "我有多少未读消息？主要是关于哪个项目的？"
- "最近一周收到了哪些预警消息？"

**Agent 工具现状**：**完全缺失**。

---

### 2.12 其他辅助域

| 域 | 表 | 说明 |
|----|----|------|
| 用户/角色 | `users`, `roles`, `user_roles` | 人员/权限：谁是谁，谁有什么角色，在线状态 |
| 项目 | `projects`, `project_members` | 项目主数据与权限 |
| 数据表 | `datasheets`, `fields`, `records` | 自定义数据表（Excel 导入的各种清单） |
| 附件 | `attachments` | 全系统统一文件索引 |
| 审计 | `audit_logs` | 操作审计日志 |
| 导出 | `export_requests` | 导出审批 |
| 用户反馈 | `user_feedback` | 反馈系统（Agent 方向不同，暂不纳入） |
| 桌面客户端 | `desktop_clients` | 客户端在线统计 |

这些辅助域目前**均无 Agent 工具覆盖**，但有些查询价值较低（如审计日志）或属于管理工具（如导出审批）。

---

## 3. 差距汇总表

| 数据域 | 可用数据表 | 最有价值的查询场景 | 已有工具? | 实现难度 |
|--------|-----------|-------------------|----------|---------|
| **销售** | sales_ledger, sales_leads, shipments | 应收款全貌 / 合同统计 / 线索成交率 / 按客户聚合 | ⚠️ 部分(尾款) | 低 |
| **采购** | purchase_items, suppliers, purchase_requests, payment_requests | 付款进度 / 发票欠票 / 请款待办 / 按供应商付款汇总 | ⚠️ 部分(到货) | 低 |
| **仓库** | wh_materials, wh_txns, material_categories | 低库存预警 / 库存总览 / 按物料查项目消耗 / 月度出入统计 | ❌ 无 | 低 |
| **财务** | sales_ledger, payment_requests, after_sales, payroll_monthly | 费用支出总览 / 项目毛利率 / 现金流 / 开票待办统计 | ⚠️ 部分(尾款) | 中 |
| **设计/电工/生产** | dept_orders, produce_group_tasks, feedback, revision_requests | 按工人负载统计 / 各组任务计数 / 未处理反馈数 | ⚠️ 部分(逾期) | 低 |
| **物流** | shipments, attachments | 待发货数 / 运费总览 / 发货记录 | ❌ 无 | 低 |
| **人事** | employees, departments, payroll_monthly, attendance_monthly, employee_salary_monthly | 在职人数统计 / 工资总额 / 考勤汇总 / 到期提醒(合同/试用) | ⚠️ 部分(到期提醒隐式) | 低 |
| **OA** | oa_requests, oa_request_steps, oa_doc_types | 待审批/我的申请 / 卡审批追踪 / 月度审批汇总统计 | ❌ 无 | 低 |
| **售后** | after_sales | 售后/安装计数与花费 / 待审批售后 | ❌ 无 | 低 |
| **管理层待办** | management_todos, management_todo_targets | 我的待办/我下发的 / 谁逾期了 / 待办统计 | ❌ 无 | 低 |
| **消息** | messages | 未读统计 / 预警消息摘要 | ❌ 无 | 低 |

**已有覆盖评价**：
- ⚠️ 部分：已有工具覆盖了该域的某几个维度，但缺更多关键维度
- ❌ 无：该域完全没有 Agent 工具

---

## 4. Quick Wins：最容易实现、价值最高的 6 个工具

以下工具按**实现难度 × 业务价值**排序，都可以**复用现有 GET 端点的查询逻辑**或写一个简单 SELECT。h

### #1 `tool_warehouse_low_stock` — 低库存预警（仓库域）
- **查询**：`WhMaterial` where `current_stock <= safety_stock`（current_stock 需基于 init_stock + Σ(in) − Σ(out) 计算，但仓库已有 `_stock_map` 辅助函数）
- **价值**：仓库/管理层高频问题"我们该补货了"，目前只能手动查
- **难度**：低（复用 `warehouse_router._stock_map`）
- **返回**：`material_name, spec, current_stock, safety_stock, deficit, unit`

### #2 `tool_finance_pending` — 财务待办统计（财务域）
- **查询**：COUNT `sales_ledger` where `invoice_state == "pending_invoice"`；COUNT `payment_requests` where `status == "pending"`
- **价值**：财务每天问"要开票的/要付款的有多少"
- **难度**：低（两次简单 COUNT）
- **返回**：`pending_invoice_count, pending_payment_count, total_payment_amount`

### #3 `tool_logistics_pending` — 待发货清单（物流域）
- **查询**：`Shipment` where `status == "pending"` → 列出项目、收货人、等待天数
- **价值**：物流每日最核心问题
- **难度**：低（单表查询，已有 GET `/board` 可参考）
- **返回**：`project_code, receiver, overdue_days(since ship_date expected), packlist_status`

### #4 `tool_purchase_payment_gap` — 采购付款缺口（采购域）
- **查询**：`PurchaseItem` where `invoice_amount > paid_amount`（已开票未付款）
- **价值**：采购员/财务问"还欠供应商多少钱"
- **难度**：低（单表过滤）
- **返回**：按供应商聚合 `supplier, total_invoiced, total_paid, gap, item_count`

### #5 `tool_hr_headcount` — 人力概览（人事域）
- **查询**：`Employee` group by department / status
- **价值**：管理层每天晨会问"各部门多少人"
- **难度**：低（一次 GROUP BY）
- **返回**：按部门 `dept_name, total, 试用, 在职, 离职`，以及即将到期提醒

### #6 `tool_oa_my_pending` — 待审批申请（OA 域）
- **查询**：`OaRequest` + `OaRequestStep` where 当前步骤的 approver_role 匹配当前用户角色的在职申请
- **价值**：每人每天第一件事
- **难度**：中（涉及多表 join + 角色匹配；但 OA_router 已有类似查询逻辑可复用）
- **返回**：`request_no, title, doc_type, current_step, requester, created_at, waiting_days`

---

## 5. 中期建议（3-5 个工具一批）

| 工具 | 域 | 查询内容 | 价值 |
|------|----|---------|------|
| `tool_sales_receivable` | 销售 | 应收款全貌：prepay+before_ship+balance 各多少、哪些已逾期 | 销售主管每日必看 |
| `tool_sales_leads_summary` | 销售 | 线索按状态/来源/销售员统计，成交率 | 销售周会核心数据 |
| `tool_production_load` | 生产 | 按工人/组统计当前进行中任务数 | 分派时看谁有空 |
| `tool_finance_pnl` | 财务 | 项目毛利 = amount − Σ(采购成本+工资+运费+售后) | 管理层高频问题 |
| `tool_oa_overdue_approvals` | OA | 卡在某一步超过 3 天的申请 | 找出流程瓶颈 |
| `tool_warehouse_txn_summary` | 仓库 | 本月入库/出库金额、按来源/去向分类 | 仓库月报数据 |
| `tool_mgmt_todo_overdue` | 管理层 | 我下发的待办中谁逾期了、谁还没回承诺 | 管理追踪 |

---

## 6. 不推荐的查询方向

| 场景 | 原因 |
|------|------|
| 审计日志查询 | 数据量大、查询慢、自然语言难以精确表达 |
| 数据表内容查询（datasheets） | schema 自由度高、字段动态、Agent 难以理解 |
| 附件内容查询 | 只存索引，不含文件内容 |
| 导出审批 | 低频管理操作，不值得 Agent 化 |
| UserFeedback（用户反馈） | 属于开发管理工具，不属于业务查询 |

---

## 7. 实现路线图建议

```
第一期（Quick Wins，1-2天）
  ├── tool_warehouse_low_stock       仓库低库存预警
  ├── tool_finance_pending          财务待办统计
  ├── tool_logistics_pending        待发货
  ├── tool_purchase_payment_gap     采购付款缺口
  ├── tool_hr_headcount             人力概览
  └── tool_oa_my_pending            待审批

第二期（补齐核心域，2-3天）
  ├── tool_sales_receivable         应收款全貌
  ├── tool_production_load          生产负载
  ├── tool_finance_pnl              项目毛利
  ├── tool_warehouse_txn_summary    仓库月度统计
  └── tool_mgmt_todo_overdue        待办逾期

第三期（报表级）
  ├── tool_sales_leads_summary      线索统计
  ├── tool_oa_overdue_approvals     卡审批跟踪
  └── tool_purchase_request_pending 采购申请待处理
```

每期新增的工具在 `TOOL_SCHEMAS` 和 `_run_tool` 中各注册一行、在 `_suggestions_for` 中补充追问建议即可。

---

## 附录 A：现有工具覆盖矩阵

| 工具 | 采购 | 销售 | 设计 | 电工 | 生产 | 仓库 | 物流 | 财务 | 人事 | OA | 售后 | 待办 | 消息 |
|------|------|------|------|------|------|------|------|------|------|----|------|------|------|
| morning_report | ✓ | ✓ | ✓ | ✓ | ✓ | — | — | ✓ | ✓ | — | — | — | — |
| po_arrival_overdue | ✓ | — | — | — | — | — | — | — | — | — | — | — | — |
| po_arriving | ✓ | — | — | — | — | — | — | — | — | — | — | — | — |
| po_overdue_by_supplier | ✓ | — | — | — | — | — | — | — | — | — | — | — | — |
| balance_due | — | ✓ | — | — | — | — | — | ✓ | — | — | — | — | — |
| overdue_orders | — | — | ✓ | ✓ | ✓ | — | — | — | — | — | — | — | — |
| project_status | ✓ | ✓ | ✓ | ✓ | ✓ | — | — | ✓ | — | — | — | — | — |

> 注：`project_status` 覆盖的域（采购/销售/设计/电工/生产/财务）仅限**单项目按编号查询**，不覆盖聚合统计。

## 附录 B：数据库表完整清单（53 张表）

| # | 表名 | 域 | Agent 查询? |
|---|------|----|-----------|
| 1 | roles | 用户/角色 | — |
| 2 | users | 用户 | 间接(格式化用) |
| 3 | user_roles | 用户/角色 | — |
| 4 | projects | 项目 | ✓ project_status |
| 5 | project_members | 项目 | — |
| 6 | datasheets | 数据表 | — |
| 7 | fields | 数据表字段 | — |
| 8 | records | 数据表行 | — |
| 9 | overview_fields | 一览表字段 | — |
| 10 | field_permissions | 权限 | — |
| 11 | overview_field_permissions | 权限 | — |
| 12 | dept_orders | 设计/电工/生产 | ✓ overdue_orders, project_status |
| 13 | produce_group_tasks | 生产 | — |
| 14 | sales_ledger | 销售 | ✓ balance_due, project_status |
| 15 | shipments | 物流 | — |
| 16 | attachments | 通用附件 | — |
| 17 | wh_materials | 仓库 | — |
| 18 | wh_txns | 仓库 | — |
| 19 | after_sales | 售后 | — |
| 20 | feedbacks | 生产反馈 | — |
| 21 | messages | 消息 | — |
| 22 | export_requests | 导出 | — |
| 23 | audit_logs | 审计 | — |
| 24 | user_feedback | 用户反馈 | — |
| 25 | sales_leads | 销售线索 | — |
| 26 | revision_requests | 技术修订 | — |
| 27 | suppliers | 采购 | ✓ po_* tools |
| 28 | purchase_items | 采购 | ✓ po_* tools, project_status |
| 29 | purchase_custom_fields | 采购配置 | — |
| 30 | wh_material_custom_fields | 仓库配置 | — |
| 31 | material_categories | 仓库 | — |
| 32 | wh_locations | 仓库 | — |
| 33 | material_dict | 仓库字典 | — |
| 34 | supplier_opening_balances | 财务 | — |
| 35 | payment_requests | 财务/采购 | — |
| 36 | payment_request_items | 财务/采购 | — |
| 37 | purchase_requests | 采购 | — |
| 38 | purchase_request_lines | 采购 | — |
| 39 | employees | 人事 | ✓ morning_report(隐式) |
| 40 | payroll_monthly | 人事/财务 | — |
| 41 | attendance_monthly | 人事 | — |
| 42 | employee_salary_monthly | 人事 | — |
| 43 | departments | OA/人事 | — |
| 44 | oa_doc_types | OA | — |
| 45 | oa_approval_steps | OA | — |
| 46 | oa_flow_cc | OA | — |
| 47 | oa_requests | OA | — |
| 48 | oa_request_steps | OA | — |
| 49 | oa_request_cc | OA | — |
| 50 | management_todos | 管理层 | — |
| 51 | management_todo_targets | 管理层 | — |
| 52 | app_settings | 系统 | — |
| 53 | desktop_clients | 系统 | — |

> 统计：53 张表中，只有 5 张被 Agent 工具直接查询（dept_orders, sales_ledger, projects, purchase_items, suppliers），另有 2 张通过 ORM relationship 间接访问（users, employees）。其余 46 张表完全没有 Agent 工具覆盖。

---

## 附录 C：各 Router GET 端点数量（快速参考）

| Router 文件 | GET 数 | 主要暴露数据 |
|-------------|--------|-------------|
| purchase_mgmt_router | 25 | 采购明细/汇总/请款/收货/报表 |
| warehouse_router | 14 | 物料/库存/出入库/发货清单 |
| sales_router | 12 | 台账/客户/销售员/开票/收货人 |
| oa_router | 10 | 申请单/审批链/部门/报表 |
| hr_router | 9 | 员工/考勤/工资/薪资汇总 |
| field_perm_router | 8 | 字段权限矩阵 |
| reports_router | 6 | 成本审计/资金面板/项目损益/月度 |
| admin_router | 5 | 用户/角色/审计/菜单 |
| finance_router | 5 | 开票/请款/售后/费用总览 |
| produce_router | 4 | 钣金/装配/封板项目 |
| projects_router | 3 | 项目详情/成员 |
| logistics_router | 3 | 发货看板/待发数 |
| datasheets_router | 3 | 数据表/字段/记录 |
| downstream_router | 3 | 采购 inbox/下游项目 |
| management_todo_router | 3 | 我的待办/已下发 |
| overview_router | 2 | 一览表字段 |
| sales_leads_router | 1 | 线索报表 |
| messages_router | 1 | 未读消息 |
| agent_router | 2 | 配置/模型列表 |

