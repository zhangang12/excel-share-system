# Agent 助手（只读问数 POC）架构分析

> 分析日期：2026-07-26  
> 分析范围：`backend/app/routers/agent_router.py`（794 行）、`frontend/src/views/AgentView.vue`（367 行）、`frontend/src/api/agent.ts`（49 行）、`backend/app/models.py` (AppSetting 表)、`backend/app/config.py` (agent_llm_* settings)

---

## 一、概述

Agent 助手是一个 **只读数据问答 POC**，嵌入在 PMS 系统中，通过 OpenAI 兼容的 function calling 接口让用户用自然语言查询项目数据。它有两个核心特点：

1. **LLM 主路径**：调用配置的大模型（默认 DeepSeek），通过 function calling 选择并执行数据工具，然后由 LLM 总结回复。
2. **规则降级路径**：当 LLM 不可用（未配 API Key 或调用异常）时，自动切换到关键词意图匹配 + Markdown 模板格式化，永远可用。

**权限**：仅 `admin` 和 `manager` 角色可用。配置页（LLM 参数）**仅 admin**，manager 只能使用聊天 + 选择模型。

---

## 二、文件清单

| 文件 | 路径 | 说明 |
|------|------|------|
| Agent 路由（后端） | `backend/app/routers/agent_router.py` | 794 行，所有端点、7 个数据工具、LLM 调度、降级逻辑 |
| 配置入口（.env） | `backend/app/config.py`，行 60-64 | `agent_llm_base_url` / `api_key` / `model` / `models` 四个配置项 |
| AppSetting 模型 | `backend/app/models.py`，行 1055-1064 | `app_settings` 表，key-value 结构，存 LLM 页面覆盖配置 |
| 路由注册 | `backend/app/main.py`，行 175 | `app.include_router(agent_router.router)` |
| 菜单定义 | `backend/app/menus.py`，行 49 | `{"key": "agent", "label": "Agent 助手"}` → 管理组菜单 |
| 权限依赖 | `backend/app/deps.py`，行 37-41 | `require_admin_or_manager` → admin/manager |
| 测试 | `backend/tests/test_agent_chat.py` | 回归测试：权限 403、降级路径、工具口径 |
| 前端 API 模块 | `frontend/src/api/agent.ts` | 49 行，4 个 API 封装 |
| 前端聊天页面 | `frontend/src/views/AgentView.vue` | 367 行，聊天 UI + 模型选择 + admin 配置弹窗 |
| 前端路由 | `frontend/src/router/index.ts`，行 163-166 | `/agent` 路由，lazy import |
| 前端菜单图标 | `frontend/src/layouts/MainLayout.vue`，行 31,34 | MagicStick 图标，归入 ADMIN_EXTRA 管理组 |

---

## 三、后端端点一览

所有端点前缀均为 `/api/agent`，在 `agent_router.py` 中定义。

### 3.1 `GET /api/agent/models`（行 670-681）
- **权限**：`require_admin_or_manager`（admin 或 manager）
- **功能**：返回可选模型白名单 + 默认模型 + 是否已配置 LLM Key
- **返回**：
  ```json
  { "models": ["deepseek-chat","deepseek-reasoner"], "default": "deepseek-chat", "llm_enabled": true }
  ```
- **说明**：走生效配置（DB > .env），`llm_enabled` 用于前端判断是否显示"规则模式"提示

### 3.2 `GET /api/agent/config`（行 684-690）
- **权限**：`_require_admin_only`（仅 admin，**不含 manager**）
- **功能**：返回当前生效的 LLM 全量配置，`api_key` 只回打码值（`****xxxx` 格式）
- **返回**：
  ```json
  { "base_url": "...", "model": "...", "models": "...", "api_key_masked": "****abcd", "has_key": true }
  ```

### 3.3 `PUT /api/agent/config`（行 700-751）
- **权限**：`_require_admin_only`（仅 admin）
- **功能**：保存 LLM 配置，写 `app_settings` 表。字段均可选：
  - 空字符串 → 保持不变（防止页面回显打码值被误存）
  - `"-"` → 清除库中覆盖值，回退 `.env` 默认
  - 其他值 → 覆盖并立即全局生效
- **校验**：Base URL 必须以 `http(s)` 开头；模型列表非空；默认模型不在白名单时自动并入

### 3.4 `POST /api/agent/chat`（行 765-794）
- **权限**：`require_admin_or_manager`（admin 或 manager）
- **请求体**（`ChatIn`，行 759-762）：
  ```json
  { "message": "...", "history": [{"role":"user/assistant","content":"..."}], "model": "deepseek-chat" }
  ```
  - `history`：最多最近 10 轮（20 条），后端自动截断
  - `model`：可选指定 LLM 模型（须在白名单内），规则降级时忽略
- **返回**（`ChatMsg`，行 754-756）：
  ```json
  { "reply": "...", "fallback": false, "sources": ["采购到期未到货"], "suggestions": ["按供应商汇总未到货","未来7天到货"] }
  ```
- **聊天流程**（见第六节详细说明）

---

## 四、7 个数据工具（全部只读 SELECT）

所有工具定义在 `agent_router.py` 行 57-280。

### 4.1 `tool_po_arrival_overdue`（行 59-86）
- **功能**：采购到期未到货明细
- **口径**：`expected_arrival <= 今天` 且 `arrival_date` 为空（仍未收货）
- **参数**：`min_overdue_days`（默认 0，含当天到期）
- **返回**：`{ count, items[] }`，每项含 `item_name/po_no/supplier/project_code/buyer/expected_arrival/over_days`
- **限制**：最多返回 20 条，按超期天数降序

### 4.2 `tool_po_arriving`（行 89-111）
- **功能**：未来 N 天预计到货、仍未收货的采购明细
- **口径**：`expected_arrival` 在 [今天, 今天+N天] 区间且 `arrival_date` 为空
- **参数**：`days`（默认 3）
- **返回**：`{ count, days, items[] }`，每项含 `in_days`（几天后到货）

### 4.3 `tool_po_overdue_by_supplier`（行 114-128）
- **功能**：到期未到货按供应商聚合
- **实现**：复用 `tool_po_arrival_overdue`，在 Python 层按供应商聚合条数、最大超期天数、涉及项目集合
- **返回**：`{ count, item_total, suppliers[] }`

### 4.4 `tool_balance_due`（行 131-158）
- **功能**：尾款到期/逾期清单（14 天窗口）
- **口径**：`SalesLedger.balance > 0` 且 `balance_date <= 今天+14天`
- **返回**：`{ count, items[] }`，含 `project_code/customer/balance/balance_date/days/sales`

### 4.5 `tool_overdue_orders`（行 161-185）
- **功能**：部门逾期未完成任务
- **口径**：`DeptOrder.status == "in_progress"` 且 `due_date < 今天`
- **参数**：`dept`（可选，限 `design`/`electric`/`produce`）
- **返回**：`{ count, items[] }`，含 `dept/dept_name/project_code/worker/due_date/over_days`

### 4.6 `_hr_due_rows`（行 188-205）
- **功能**：人事到期（合同到期 30 天窗口 + 试用期转正 7 天窗口）
- **口径**：`Employee.status != '离职'`，遍历每个员工的合同到期和转正日期
- **返回**：`[{ kind, name, dept, date, days }]`

### 4.7 `tool_morning_report`（行 208-222）
- **功能**：晨报聚合，一键调用 `arrival_overdue` + `overdue_orders` + `balance_due` + `_hr_due_rows`
- **返回**：四个维度各取 Top5 + 总数

### 4.8 `tool_project_status`（行 225-280）
- **功能**：按项目编号查进度
- **实现**：查 `Project`（大小写两次尝试）→ 关联 `DeptOrder` → `PurchaseItem` 未收货 → `SalesLedger` 尾款
- **参数**：`code`（必填）
- **返回**：基本信息 + 部门任务列表 + 未到货采购 + 销售台账

---

## 五、工具注册表（LLM Function Calling Schema）

定义在行 283-350。

### 5.1 TOOL_LABELS（行 285-293）
中文友好名映射，用于前端展示「数据来源」标签：
```
morning_report → "晨报聚合"
po_arrival_overdue → "采购到期未到货"
po_arriving → "预计到货"
po_overdue_by_supplier → "未到货·按供应商汇总"
balance_due → "尾款到期清单"
overdue_orders → "部门逾期任务"
project_status → "项目进度查询"
```

### 5.2 _TOOL_SUGGESTIONS（行 296-304）
每个工具调用后，映射固定的追问建议 chips（去重保序，取前 3 条）：
| 工具 | 追问建议 |
|------|----------|
| `morning_report` | 采购未到货明细、尾款到期清单、部门逾期任务 |
| `po_arrival_overdue` | 按供应商汇总未到货、未来 7 天到货、今日晨报 |
| `po_arriving` | 采购未到货、今日晨报 |
| `po_overdue_by_supplier` | 采购未到货明细、未来 7 天到货 |
| `balance_due` | 今日晨报、部门逾期任务 |
| `overdue_orders` | 今日晨报、采购未到货 |
| `project_status` | 该项目未到货采购、尾款到期、今日晨报 |

### 5.3 TOOL_SCHEMAS（行 316-350）
OpenAI function calling 格式的工具定义数组，每个包含 `type: "function"`、`function.name`、`function.description`、`function.parameters`（JSON Schema）。被 `_llm_request` 在每轮请求中作为 `"tools"` 字段传入。

---

## 六、聊天流程详解

### 6.1 完整流程图

```
POST /api/agent/chat
  │
  ├─ 参数校验（message 非空，model 白名单校验）
  ├─ 历史截断（最近 10 轮/20 条）
  ├─ _effective_llm_config() 读生效配置（DB > .env）
  │
  ├─ api_key 存在？ 
  │   ├─ YES → _chat_with_llm() LLM 主路径
  │   │         │
  │   │         ├─ 构建 messages: [system] + history + [user]
  │   │         ├─ 循环最多 4 轮（行 414）:
  │   │         │   ├─ _llm_request() → POST {base_url}/chat/completions (30s 超时)
  │   │         │   ├─ 如果有 tool_calls → _run_tool() 执行 → 回灌 tool 消息 → 继续循环
  │   │         │   └─ 无 tool_calls → 返回 content + tool_names
  │   │         └─ 异常 → 转规则降级
  │   │
  │   └─ NO 或异常 → _rule_chat() 规则降级
  │                   │
  │                   ├─ 关键词匹配（行 594-615）
  │                   ├─ 执行对应工具 → 模板格式化
  │                   └─ 返回 (reply, tool_names)
  │
  └─ 统一返回格式 {reply, fallback, sources, suggestions}
```

### 6.2 LLM 主路径（行 405-438）

`_chat_with_llm()` 关键设计：

1. **System Prompt**（行 373-380）：每次注入用户姓名、角色、今天日期
2. **Tools**：每次请求都传入全部 `TOOL_SCHEMAS`，`tool_choice: "auto"`
3. **Temperature**：固定 0.2（低随机性，偏好事实性回答）
4. **工具轮次上限**：最多 4 轮循环，防止死循环
5. **安全性**：httpx 异常只透出状态码和异常类名，不泄露请求信息（尤其是 API Key）

### 6.3 规则降级路径（行 594-615）

`_rule_chat()` 关键词匹配优先级：

| 匹配关键词 | 触发工具 | 格式化函数 |
|------------|----------|------------|
| 晨报/早报/早会/要盯/风险/汇报 | `tool_morning_report` | `_morning_text()` |
| 供应商（且非"未到货/到货"场景优先） | `tool_po_overdue_by_supplier` | `_po_by_supplier_text()` |
| 未到货/采购/到货 → 未来/预计/下周（不含"未到货/超期"） | `tool_po_arriving` | `_po_arriving_text()` |
| 未到货/采购/到货（默认） | `tool_po_arrival_overdue` | `_po_overdue_text()` |
| 尾款/回款/欠款 | `tool_balance_due` | `_balance_text()` |
| 逾期 | `tool_overdue_orders` | `_overdue_orders_text()` |
| 项目编号正则（如 `TH-2501`） | `tool_project_status` | `_project_text()` |
| 都不匹配 | — | `_CAPABILITY_TEXT`（能力说明） |

---

## 七、系统提示词（System Prompt）

完整文本在 `agent_router.py` 行 373-380：

```
你是制造业 ERP 项目管理系统内置的数据分析助手（只读），当前服务对象：「{user_name}」（角色：{roles}）。严格遵守：
1. 只能根据工具返回的真实数据回答，严禁编造任何数字、日期、金额、项目编号、人名；
2. 工具没有返回的信息就如实说"系统里查不到"，不要推测、不要举例；
3. 回答用中文、Markdown 格式：先一句话结论概览，明细数据（≥2 条）一律用 Markdown 表格呈现，最后给 1-2 条可执行的建议或跟进方向；
4. 表格列从工具字段里挑最有用的 4-6 列（如物料/供应商/预计到货/超期天数/项目），超期严重的用 **加粗** 标出；金额保留原始数值，日期原样引用；
5. 需要数据时先调用工具，可连续调用多个；拿到工具结果后直接总结，不要重复调用同一工具；
6. 你只能查询，不能修改任何数据；用户要求改数据时明确拒绝。
今天日期：{today}（中国时区）。
```

两个占位符：`{user_name}`（用户姓名）、`{roles}`（角色列表，如 `admin、manager`）。

---

## 八、配置机制

### 8.1 双层配置优先级

```
┌──────────────────────────────────────────────────────┐
│  数据库 app_settings 表（页面配置，PUT /api/agent/config） │  ← 最高优先级
│  键名格式：agent_llm.{base_url|api_key|model|models}    │
├──────────────────────────────────────────────────────┤
│  settings(.env) 环境变量                                │  ← 默认值/回退值
│  AGENT_LLM_BASE_URL / API_KEY / MODEL / MODELS          │
└──────────────────────────────────────────────────────┘
```

### 8.2 环境变量（`config.py` 行 60-64）

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `AGENT_LLM_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容接口地址 |
| `AGENT_LLM_API_KEY` | `""`（空） | 留空 = 纯规则降级模式 |
| `AGENT_LLM_MODEL` | `deepseek-chat` | 默认模型 |
| `AGENT_LLM_MODELS` | `deepseek-chat,deepseek-reasoner` | 逗号分隔的白名单 |

### 8.3 数据库存储（`AppSetting` 模型，行 1055-1064）

`app_settings` 表结构：
```sql
key VARCHAR(64) PRIMARY KEY,   -- 如 "agent_llm.base_url"
value TEXT,                    -- 存储值
updated_at TIMESTAMP
```

由 `Base.metadata.create_all` 启动时自动建表，无需迁移脚本。

### 8.4 配置读取流程（行 637-648）

`_effective_llm_config()` 每次请求实时读 `app_settings` 表（4 行，量极小），与 `.env` 默认值合并：
```python
stored = read_db_app_settings()   # 读 4 个 key
base_url = stored["base_url"] or settings.agent_llm_base_url
api_key  = stored["api_key"]  or settings.agent_llm_api_key
# ... 其余同理
```

### 8.5 安全性

- API Key **永远不输出明文**：GET /config 返回打码值 `****<后4位>`，不足 4 位全 `****`
- 日志只记"配置已由谁更新，改了什么字段"，不记值
- httpx 异常掐断异常链（`from None`），防止请求信息（含 Key）泄露到日志

---

## 九、前端实现

### 9.1 API 模块（`frontend/src/api/agent.ts`）

定义 4 个接口 + TypeScript 类型：
- `agentApi.chat(message, history, model?)` → `POST /api/agent/chat`
- `agentApi.getModels()` → `GET /api/agent/models`
- `agentApi.getConfig()` → `GET /api/agent/config`
- `agentApi.saveConfig(body)` → `PUT /api/agent/config`

### 9.2 聊天页面（`AgentView.vue`）

**组件结构**：
- **页面头部**：标题 + 模型选择下拉（← `GET /models`）+ admin 配置按钮
- **快捷问题 bar**：4 个圆角按钮（今日晨报/采购未到货/尾款到期/逾期任务）
- **消息列表**：滚动容器，用户消息（右对齐蓝色气泡）+ 助手消息（左对齐白色气泡）
  - 助手消息：Markdown 渲染（`markdown-it`，`html: false` 防 XSS）
  - 每条助手消息带 meta 行：`fallback` 显示"规则模式"标签 + `sources` 显示数据来源
  - 追问建议 chips：点击直接发送
- **输入区**：textarea（Enter 发送，Shift+Enter 换行）+ 发送按钮
- **配置弹窗**（仅 admin）：Base URL / API Key / 默认模型 / 可选模型列表

**状态管理**：
- 模型选择持久化到 `localStorage`（key: `pms_agent_model`）
- 对话历史只保留前端内存中的 10 轮（前端自己截断 20 条 + 后端同样截断）
- 每次对话前回拉最近 10 轮上下文作为 history 上传

### 9.3 路由与菜单

- 路由：`/agent`，组件 `AgentView.vue`，meta `menuKey: 'agent'`
- 菜单：归入管理组（`ADMIN_EXTRA` 数组），仅 admin/manager 可见
- 图标：`MagicStick`（Element Plus 图标）

---

## 十、测试覆盖

测试文件 `backend/tests/test_agent_chat.py`（321 行）覆盖：

1. **权限测试**：非 admin/manager（buyer 角色）访问 → 403
2. **降级路径测试**（强制无 Key 模式）：
   - "采购未到货吗" → 回复含真实物料名 + `fallback=true` + sources 含"采购到期未到货"
   - "今日晨报" → 回复含部门名/项目名
   - "AGT-2501 项目进度" → 回复含项目信息
3. **工具口径测试**：`tool_po_arrival_overdue` 查得出预计昨天到货且未收货的明细；查不出已收货的

---

## 十一、局限性、未实现功能与改进建议

### 11.1 架构层面

| 问题 | 详情 | 严重度 |
|------|------|--------|
| **只读限制硬依赖 LLM 遵守** | prompt 说"只能查询、不能修改"，但没有任何代码层面（非 LLM 依赖的）写操作拦截。如果 LLM 产生幻觉，它只能说"拒绝"——但工具层本身全是只读 SELECT，所以**实际无法写库**。这是安全的。 | ✅ 安全 |
| **无流式输出（streaming）** | 当前一次请求 → 等待完整回复 → 返回。LLM 调用 30s 超时，前端显示"思考中"三点动画。大模型调用时用户看不到部分回复，体验不如流式。 | 中 |
| **无 RAG/知识库** | 系统 prompt 是硬编码的，没有注入业务知识文档（如项目交接文档、SOP）。LLM 只能根据工具返回的原始数据来回答，缺乏业务背景解释。 | 中 |
| **工具返回数据截断** | 所有工具只返回前 20 条（晨报 Top5），LLM 看不到全量数据。用户问"列出全部"时 LLM 只能说"仅列前 20 条"。 | 低（故意设计） |
| **history 不持久化** | 对话历史仅在前端内存中，刷新页面丢失。多轮对话的"记忆"依赖前端每次回传最近 10 轮。没有服务端会话存储。 | 中 |
| **温度固定 0.2** | 不可配置，适合事实性查询但无法调整创造性/多样性 | 低 |

### 11.2 工具层面

| 问题 | 详情 | 严重度 |
|------|------|--------|
| **工具覆盖面窄** | 仅覆盖采购未到货、逾期任务、尾款、人事到期、单项目进度。缺少：仓库库存查询、物流状态、财务流水、销售线索进展、消息/通知等 | 高 |
| **无分页机制** | 工具直接截断为 Top20，没有 offset/limit 参数让 LLM 分页查询 | 低 |
| **人事到期工具非独立注册** | `_hr_due_rows` 没有对应的 TOOL_SCHEMAS 条目和 TOOL_LABELS，只能通过 `morning_report` 间接触发。LLM 无法单独查询"人事合同到期"。 | 中 |
| **项目编号正则不够强壮** | `[A-Za-z]{2,}-?\d+` 可能匹配到非项目编号的字符串（如 Excel 单元格引用），在降级模式中误触发项目查询 | 低 |
| **按供应商聚合是 Python 层聚合** | `tool_po_overdue_by_supplier` 复用 `tool_po_arrival_overdue` 的 Python 聚合，而非 SQL GROUP BY。数据量大时性能差，但目前只返回 Top20 所以影响有限 | 低 |

### 11.3 前端层面

| 问题 | 详情 | 严重度 |
|------|------|--------|
| **无输入建议/自动补全** | 输入框是纯 textarea，没有项目编号、供应商名等自动补全 | 低 |
| **配置弹窗不支持验证** | 模型列表输入错误（如中文逗号）没有实时校验，只在保存时后端返回错误 | 低 |
| **缺少使用帮助/引导** | 首次使用只有一条初始消息说明能力，没有更丰富的引导（如教程 steps、视频等） | 低 |

### 11.4 运维与监控

| 问题 | 详情 | 严重度 |
|------|------|--------|
| **无使用统计** | 没有记录谁什么时候问了什么问题、用了哪些工具、LLM 与降级的比例 | 中 |
| **无响应时间监控** | LLM 调用 30s 超时，但没有 P50/P95 延迟指标 | 低 |
| **配置变更无审计详情** | 日志只记"谁来改了哪些字段"，不记字段变更前后对比（出于安全不记值，但可记 hash） | 低 |

### 11.5 设计上的改进机会

1. **工具扩展框架抽象**：目前 7 个工具都在一个文件里用 if/elif 分发。可考虑：Tool 基类 → 自动注册 → 自动生成 OpenAI schema → 统一调度。
2. **Agent 多步推理**：当前就是"问 → 调工具 → 答"的单回合。如果要实现"先查进度再查尾款再查欠料"的链式推理，需要更复杂的 Agent 循环（当前已有 4 轮上限，但工具调用是并行的，不是根据结果的链式）。
3. **面向普通用户开放**：当前仅 admin/manager 可用，如果可以配合行级数据过滤（如 `restricted_dir_pids`），就可以安全地开放给部门负责人等角色。
4. **与消息系统集成**：Agent 可以在晨报定时任务中主动推送摘要到用户（Agent 二期方向，在 AGENTS.md 中提及）。

### 11.6 已知问题（来自代码注释）

- AGENTS.md 提到"16 个存量失败测试可另开一轮修"——`test_agent_chat.py` 可能需要在基线上验证是否为存量失败之一（当前版本是否 PASSED 不确定，因为第 15 批提到"7 个测试文件全 PASSED"，test_agent_chat 应该是其中之一）。

---

## 十二、快速参考卡片

| 事项 | 信息 |
|------|------|
| 后端端点 | `/api/agent/chat`, `/api/agent/models`, `/api/agent/config` |
| 权限 | chat+models: admin/manager; config: 仅 admin |
| 数据工具数 | 7 个（工具层 6 个 + 晨报聚合 1 个 + 人事到期 1 个内部函数） |
| LLM 接口 | OpenAI 兼容 `/chat/completions`，30s 超时，temperature=0.2 |
| 配置存储 | `app_settings` 表（DB） > `.env` 环境变量 |
| 安全红线 | API Key 永不输出明文；日志不记值；httpx 异常掐断异常链 |
| 降级保证 | 无 Key 或 LLM 异常 → 关键词匹配 + 模板格式化，永远可用 |
| 前端渲染 | Markdown（markdown-it, html: false 防 XSS） |
| 模型白名单 | 逗号分隔，默认模型自动并入，前端下拉选择持久化到 localStorage |
| 历史保留 | 最近 10 轮/20 条（前端+后端各截断一次） |
| 工具轮次上限 | 4 轮（防死循环） |
