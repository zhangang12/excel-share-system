# n7 · 登录相关测试与坑

> 范围：只读 `backend/tests` 与 AGENTS.md（不跑测试）。登录**实现细节**见 `02-login-gate.md`（闸门流程）与 `backend-login-flow.md`（全链路），本文聚焦**测试侧全景**：哪些测试在测登录/鉴权、基线挂的测试里哪些与登录相关、SQLite vs Postgres 差异、历史登录 bug 与修复线索。
> 边界声明：按任务边界未运行任何测试，所有"挂/不挂"结论均引用 AGENTS.md 记录或测试断言本身，标 `待核实` 处需跑测试确认。

## 0. 一句话结论

登录/鉴权测试分两层：**登录闸门专属测试**（`test_login_gate.py` + `test_gate_device_ids.py`，覆盖两步验证码、免闸四路径、限频、设备闸）与**以登录为前置的鉴权矩阵测试**（m01 角色菜单、e2e #91 详单闸门、user_feedback 403 等，全部用 `POST /api/auth/login` 拿 token 后测越权）；AGENTS.md 的 13 个基线挂测试中与登录/鉴权直接相关的是 m01、两个 e2e、user_feedback。**所有登录测试都在临时 SQLite 上跑，SQLite 与 Postgres 的 datetime 行为差异只有 gate.py 的 `_aware` 一处兜底。**

## 1. 登录/鉴权相关测试文件总览

| 文件 | 归属层 | 测什么 | 与登录的关系 |
|---|---|---|---|
| `test_login_gate.py` | 闸门专属 | 外网两步验证码全流程（见 §2） | 核心 |
| `test_gate_device_ids.py` | 闸门专属 | 设备闸 + `desktop_exempt` + 配置读写 + 脏数据容错 | 核心 |
| `test_desktop_clients.py` | 统计（非登录） | `X-PMS-Client/X-PMS-Device/X-PMS-User` 三头 upsert + 60s 节流 | 验证桌面免闸判定所依赖的头契约 |
| `test_desktop_report.py` | 崩溃上报（非登录） | `POST /api/desktop/report` **刻意免认证** | 登录前场景；鉴权豁免的对照样本 |
| `test_smoke_startup.py` | 冒烟 | 启动 + seed + admin 登录 | AGENTS.md 注明"实测本就能过，旧清单偏旧" |
| `test_m01_roles_menus.py` | 鉴权矩阵 | 各角色菜单清单 + 详单 403 + #91 子端点统一闸门 | 登录后 token 的鉴权行为 |
| `test_user_feedback.py` | 业务鉴权 | 销售员标记已处理/导出被拒（403） | 鉴权豁免对照 |
| 其余 m 系 / e2e / 其它 ~90 个文件 | 前置 | 全部以 `POST /api/auth/login` 拿 token 作入口 | 共用登录，不测登录本身 |

### 1.1 所有登录测试共享的环境约定

`test_login_gate.py:15`、`test_gate_device_ids.py:15`、`test_desktop_clients.py:12`、`test_desktop_report.py:14` 都在 import 早期设 `os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"` 与 `FILES_DIR`，即**每个测试在独立临时 SQLite 上跑**，互不污染。意味着 SQLite 上通过 ≠ Postgres 必然等价（见 §4）。

## 2. 登录闸门专属测试逐项（test_login_gate.py）

`test_login_gate.py:15` 用临时 SQLite + `chk()` 断言，流程分段（行号以当前 HEAD 为准）：

1. **`is_intranet` 纯函数**（`test_login_gate.py:47-57`）：CIDR 命中/不命中、单 IP 按 `/32`、非法条目跳过后续仍匹配、空名单公网不命中、非法 IP 不命中、**回环地址恒内网**（`127.0.0.1`/`::1`/`10.x`/`172.16.x` 空名单也判内网）。
2. **内网登录免闸**（`:95-101`）：内网 IP → 直接发 `access_token`、`gate_required` 不出现，且 `login_gate_codes` 表无行（不发码）。
3. **外网浏览器登录 → 两步闸**（`:103-121`）：返回 `gate_required=true + pre_token`，无 token；管理层（manager 角色）收到企微/站内消息，正文含 6 位码（`kind=="warn"`、前缀"【外网登录验证】"）；库中存 `code_hash`（`sha256`），不存明文（`:121`）。
4. **错码/对码/重放**（`:125-144`）：错码 400 + `fail_count+1`；对码换 token 且 `used=True`；**已用的码再验 400**（一次一码）。
5. **错码不锁定**（`:146-162`）：连错 6 次仍 400 非 429，正确码仍可登录——对应 AGENTS.md/`446e33b`"已按用户要求去掉错 5 次锁定"。
6. **过期码**（`:163-171`）：`expires_at` 减 1 分钟 → 400"已过期"。
7. **桌面客户端免闸**（`:174-177`）：带 `X-PMS-Client: desktop/...` 头 → 直接发 token。
8. **admin 免闸**（`:180-183`）：admin 外网直进。
9. **`gate_enabled=0`**（`:186-192`）：关闸后外网浏览器直进。
10. **限频**（`:198-203`）：1 分钟内对同一用户重复发码 → 429（对应"仅保留 1 条/分钟间隔"）。
11. **配置端点权限**（`:205-214`）：普通角色 `GET /api/admin/gate-config` 403、未登录 401、管理组菜单含 `gate-config`。

### 2.1 设备闸（test_gate_device_ids.py）

- `desktop_exempt` 真值矩阵（`:76-85`）：`device_gate` 关 → 一切客户端免闸；开 → 仅在名单内免闸；不带设备 ID → 不免闸；**浏览器（非 desktop）永远不走这条免闸**。
- `is_intranet` 补充（`:86-87`）：`192.168.31.23` 恒内网；**RFC 5737 文档段 `203.0.113.9` 被判内网**（Python `ipaddress` 判定，文档段命中私有判定逻辑）。
- 端到端（`:93-152`）：未配置时 `device_gate` 默认关、名单空；**外网 + 未登记客户端 → 照样直接进**（开关默认关即无强制）；浏览器仍要验证码（不受设备闸影响）；名单内设备免闸 / 名单外要码 / 不带 ID 要码；**只伪造 `X-PMS-Client` 头、没有在册设备 ID → 被拦**（`:113`）；名单填错锁死时 admin 仍能进来改回（`:118`）；内网 + 名单外设备仍免闸（走 `is_intranet`）；保存 `device_ids` 不冲掉 `cidrs`（`:133`）；**脏数据（非 JSON / 非数组）当空名单执行 → 拦住而非误放行**（`:143-146`，对应 gate.py 配置解析的防御分支）；不带新字段的旧配置仍可保存（`:151-152`）。

## 3. 与登录鉴权相邻的测试

- **`test_desktop_report.py`**：`POST /api/desktop/report` **不要求认证**（`test_desktop_report.py:57`，200 被接受）——设计使然：崩溃上报发生在登录之前，认证就抓不到目标场景。防滥用靠单条 detail 截断 64KB（`:74-78`）+ 每设备每天封顶 20 条、**超限返回 200 而非 429**（`:88-91`，不给探测者反馈阈值）。列表读取需 admin（普通用户 403，`:95`）。**这是全系统唯一"刻意免认证 + 主动隐藏限流阈值"的写端点，新增类似登录前端点时抄这个模式。**
- **`test_desktop_clients.py`**：三统计头 upsert（`:51-57`）与 60s 节流（`:63-69`）。免闸判定 `auth_router.py:90` 的 `is_desktop = x-pms-client.startswith("desktop/")` 依赖同源头。
- **`test_m01_roles_menus.py`**：鉴权矩阵——admin 全菜单（`:30-39`）、各角色菜单清单（销售 `catalog/sales/leads/oa/agent/messages`、电工 `catalog/electric/...`、售后 `aftersales/...`、设计师 `catalog/list/design/...`，`:63-116`）、角色被收紧的详单子端点统一 403（`:70-79`）、**#91 详单子端点闸门**：`fields/records/cell/create` 对 sales/electrician 全部 403（`:81-102`）。
- **`test_user_feedback.py`**：销售员调"标记已处理"403（`:95`）、导出 403（`:106`）。

## 4. 登录逻辑在开发 SQLite 与生产 Postgres 下的行为差异

| 维度 | 结论 | 证据 |
|---|---|---|
| datetime aware/naive | **唯一已识别差异**。`DateTime(timezone=True)` 在 Postgres 读回 aware；SQLite 读回 naive。`LoginGateCode.expires_at`（10 分钟过期判断）在 SQLite 下必须补时区，为此 gate.py 有 `_aware()`（`gate.py:33-35`，注释明写"SQLite 读回的是 naive 时间，统一按 UTC 补齐"）。**Postgres 下 `_aware` 是空操作，行为无差** | gate.py:33-35；test_login_gate.py:163-171 |
| JWT 签发/校验 | 纯内存（`auth.py:26-36`，默认 8h、记住我 30 天），无 DB 依赖，两库无差异 | auth.py:26-36 |
| 密码哈希 | bcrypt 纯 Python 比对，无差异 | auth.py:9-23 |
| IP 内网判定 | Python `ipaddress` 标准库，无 DB 依赖 | gate.py:54-64；test_login_gate.py:47-57 |
| 配置存储 | `app_settings` 用 `Text` 存 JSON 字符串（`models.py:1145-1151`），两库同为字符串，无差异；脏数据解析已防御 | test_gate_device_ids.py:143-146 |

**风险提示**：登录测试全部只跑 SQLite，`_aware` 是 SQLite 与 Postgres 差异的唯一兜底点。今后写任何涉及 `login_gate_codes` / 登录会话时间的比较，**必须复用 `_aware` 或走 `expires_at` 的 ORM 比较**，不要裸比 naive datetime。

## 5. AGENTS.md 已知坑对照（13 个基线挂测试中与登录相关的）

AGENTS.md「已知坑」列出的挂测试（原话）：
`m01`(剩4个#91详单闸门)`/m02/m04/m07/m08/m12/m13/m14/m15`、e2e 两个、`outsourcing_template`、`user_feedback`、`void_sales_order`。

- **与登录/鉴权直接相关**：`test_m01_roles_menus.py`（剩 4 个 #91 详单闸门 403 断言）、`test_e2e_business_flows.py` / `test_e2e_full_lifecycle.py`（e2e 全流程含登录）、`test_user_feedback.py`（403 越权断言）、`test_m13_feedback.py`（feedback 域）。
- **已修好/实能过**：`mr_probe_menu`（2026-07-21 菜单重构顺手修好）、`smoke_startup`（AGENTS.md 注明实测本就能过、旧清单偏旧）。
- **与登录无关**：m02/m04/m07/m08/m12/m14/m15、`outsourcing_template`、`void_sales_order`（业务域，登录仅作前置）。
- **数量矛盾（待核实）**：AGENTS.md 标题称"**13 个**测试在基线 HEAD 上就挂"，但清单实际罗列 **14 个名字**（9 个 m 系 + 2 个 e2e + outsourcing_template + user_feedback + void_sales_order）。差额可能是某名字已在后续修复但 AGENTS.md 未更新。**未跑测试，无法判定"13"与"14"哪个对。**

## 6. 历史登录 bug 与修复线索（git log）

| 提交 | 日期 | 内容 | 对测试/鉴权的影响 |
|---|---|---|---|
| `393c7f7` | 2026-07-28 | 外网上线两步闸门：浏览器外网登录需随机码（发管理层企微），admin/客户端/内网免闸 | test_login_gate.py 首建 |
| `446e33b` | 2026-07-28 | 闸门放宽：去掉每日 10 条发码上限与错 5 次锁定，保留 1 分钟 1 条 | test_login_gate.py:146-162 断言随之改为"不锁定" |
| `0f2fe39` | 2026-07-28 | 外网访问文案：去掉 admin 账号字样 + 同步限频口径 | 文案层 |
| `168ba23` | 2026-07-26 | 登录页记住用户名（桌面端 1.0.9） | 前端登录态 |
| `756433d` | 2026-07-28 | 客户端登录成功自动检查更新（30 分钟节流） | 登录后动作 |
| `8fe9688` | 2026-08-03 | 客户端设备限制：按设备 ID 控制登录，开关默认关 | test_gate_device_ids.py 首建 |
| `7e6264e` | 2026-08-03 | 桌面 1.0.28：登录页显示设备 ID + 写盘失败上报 | 设备闸的可诊断性 |
| `c2a2d5b` | 2026-08-03 | H5 30 天免登录 | 对应 `REMEMBER_MINUTES`（auth_router.py:62） |
| `714daa5` | 2026-08-03 | **#343 真身：401 跳登录页在客户端解析成 `file:///login`** | 前端 axios 401 拦截器 `location.href='/login'` 相对跳转被 `file://` 协议解析成 `file:///login` 空页；桌面端（`webSecurity:false` + file:// 内置页）下 401 处理与浏览器不同，改后需避免裸相对跳转 |
| `5d585cf` | 2026-07-21 | 桌面客户端一期（Electron 壳 + 自动更新 + 强制最低版本 + 在线版本分布） | test_desktop_clients 相关基础设施 |

## 7. 反例 / 排除的猜想

- ~~登录闸门限频依赖消息推送频控~~：限频在 `gate.py` 内部按 `user_id + 当日` 落 `login_gate_codes` 表（1 条/分钟），与 `push_message` 的企微频控是两套独立逻辑（`test_login_gate.py:198-203` 直连发码接口验证 429，不依赖企微侧）。排除"企微频控误伤登录限频"。
- ~~验证码明文入库~~：库中只存 `sha256`（`test_login_gate.py:121` 断言 `code_hash == hashlib.sha256(...)`），明文只在 push_message 正文里走企微。排除数据库拖库直接拿码的风险。
- ~~device_gate 默认开启~~：默认关（`test_gate_device_ids.py:93-94` 断言 `cfg["device_gate"] is False`），因此**现网外网桌面登录当前并不强制设备名单**——AGENTS.md 已记录该风险（X-PMS-Client 可被伪造，真正防线是默认关闭的设备闸）。

## 8. 待核实 / 遗留

1. AGENTS.md "13 个挂" vs 清单 14 个名字的差额——需跑一遍测试核实（本片边界禁止跑测试）。
2. `test_m13_feedback.py` 挂因是否与 user_feedback 同源（403 断言 vs 其它）——未深读，需跑测试看挂点。
3. m 系编号无 `test_m03`、`test_m10`（m09_m10 合并），编号与挂清单的对应以文件名为准。
