# AGENTS.md — 项目记忆

> 会话开始先读我。只记「不看代码就不知道」的事实和约定，细节以代码为准。

## 项目是什么

同辉项目管理 ERP：制造业项目全流程内部系统（销售→设计→电工→生产→采购→仓库→物流→财务→售后→人事+管理层）。

- 后端：FastAPI + async SQLAlchemy，生产 PostgreSQL / 开发测试 SQLite（`backend/app/config.py` 的 `database_url`）
- 前端：Vue 3 + TS + Element Plus + vxe-table，Vite
- 服务器：`root@8.141.123.141`，目录 `/opt/pms/excel-share-system-main`，docker compose（容器 `pms2_backend / pms2_frontend / pms2_nginx / pms2_postgres`）

## 常用命令

```bash
# 开发
cd backend && .venv/bin/python -m uvicorn app.main:app --reload   # 后端
cd frontend && npm run dev                                        # 前端（Vite 代理 /api）

# 测试（独立 asyncio 脚本 + 临时库，不是 pytest，不要用 pytest 跑）
cd backend && .venv/bin/python tests/<name>.py                    # 必须用 .venv 的 python（系统 python3 缺 aiosqlite）
cd frontend && node_modules/.bin/vue-tsc -b                       # 前端类型检查（1-2 分钟）
cd frontend && npm run build                                      # = vue-tsc + vite 打包

# 发版（唯一路径）—— 服务器 + 桌面客户端一体发，缺一不可（2026-07-21 用户定的规矩）
bash ops/release.sh --push   # 本地 push main → SSH 服务器 upgrade.sh：备份→拉码→docker 重建→健康检查→失败自动回滚
                             # 部署配置在 .deploy.local（gitignored）；构建在服务器做，本地不构建
                             # GitHub 直连不通时先手动 git -c http.proxy=http://127.0.0.1:7890 push，再跑本脚本（不带 --push）
bash desktop/release.sh      # 同步发桌面客户端：版本号 bump 应随代码提交一起入库（先 npm version patch --no-git-tag-version），
                             # 再 --set-version 同号执行（幂等跳过 bump）打包上传；客户端重启即自动更新
# 反馈修复后必须逐条自动回复（2026-07-23 用户定的规矩）：发版完成后，对本批每条反馈调
#   POST /api/user-feedback/{id}/reply 写处理结论（回复即自动标已处理，提出人下次登录右下角弹提醒）；
#   回复内容说人话：改了什么、在哪看效果；未改的要给原因（如 #283 已是旧版修复）
```

种子账号：`admin / admin123`、`manager / manager123`（seed 自动建）。运维脚本说明见 `ops/README.md`。

## 铁律

- **git 任何变更（commit/push/reset/rebase）必须用户当场明确授权**，每次单独确认；授权不过夜
- **图文一起读，先给附件归位**：反馈的截图不是配图，是内容本身——先确认每张截图挂在哪条反馈下、图上的标注（红框/箭头/圈）指向什么，再下结论（教训：#265「这个不用处理了」的截图就是她自己红框框出的 #263，单读文字才会觉得指代不明）
- **解析反馈导出 HTML：按 article 卡片边界切，条数对不上就停**：导出文件每条一卡（卡内顺序 元信息→正文→页面URL→截图），归位必须按 `<article>` 切，禁止按零散 div 切；导出声明「共 N 条」与解析出的 ID 数不符时**立即停下重查**，不许编解释填坑（教训：07-21 批次按 page div 切，全部文字与截图错位一条、#271 丢失，两条实现做错返工；每条结论必须 ID+原文+页面字段+截图四者一致）
- **红框强制入境，文图冲突图赢**：带标注截图的反馈，结论句必须把红框元素当主语（"她圈的是X列/X按钮，所以…"），红框塞不进结论=理解错了，停下重来；文字字面指向子系统 A、红框指向子系统 B 时按 B 实现（用户打字随手、画圈深思）（教训：#283 她说"不用推送采购信息"但红框在指定采购员列，真意是数据可见性跟指定人走，我按字面改了推送，返工+更正回复）
- **「不用改」加倍举证**：结论是"已修好/不用动"时，必须给出可验证证据（时间线/数据）且完整解释用户为何仍遇到问题；只能给出"可能/也许"时按需要改处理——"不用改"的证据标准高于"要改"（同一 #283 教训：我拿#242旧修复+未证实的"旧消息"假设就放行了自己）
- **先质疑需求再实现**：对「让计算列可编辑」这类设计上就危险的诉求，先反问是否合理、是否有更简单的满足方式，再写代码
- 只改与任务相关的文件；不主动新建文档（用户要求除外）；改接口时同步更新调用方与注释
- Agent 助手（`backend/app/routers/agent_router.py`）的所有数据工具**永远只读 SELECT**，不提供收货/付款等强职责命令

## 关键约定

- **日期**：多为 ISO 字符串（`"2026-07-21"`），可直接字典序比较；业务时区 UTC+8，复用 `app/overdue.py` 的 `_CN_TZ` / `_cn_date`
- **消息幂等键** = `biz_type + biz_id + 当日`（参考 `overdue.py` 各 `scan_*`）；周期任务在 `--workers 4` 下必须 flock 单实例（同文件 `_try_acquire_scheduler_lock`）
- **菜单可见性唯一权威** = `User.menus`（按账号 JSON 清单，业务+管理组 key 混合）：`user_menu_keys()`（menus.py）对 admin/manager 全量 bypass，其余读 `User.menus`（NULL→`DEFAULT_ACCOUNT_MENUS`=catalog/list/messages/oa）。`ROLE_DEFAULT_MENUS`（原 ROLE_MENUS）**仅是建号预填/backfill 的默认模板，运行时不读**；建号后改角色不影响菜单。管理端配置入口：用户管理→「菜单权限」弹窗（`PUT /admin/users/{uid}/menus`）；`PUT /grant-menus` 是桌面端旧版兼容包装（只增删管理组 key）。前端 `MainLayout.vue` 全部菜单项（含 dict-admin/管理组硬编码三项）已 menus 驱动；auth store 的 `isAdmin` = admin **或** manager
- **建表**：新表靠 `Base.metadata.create_all` 启动自动建；存量表加列走 `app/data_migration.py`（存量数据回填也在这，模板 `backfill_user_menus`）
- **角色**：不再管菜单（2026-07-21 起按账号配置）；角色仍管业务归属（部门工作台、downstream 推送、restricted_dir_pids 行级过滤、finance_lead⊇finance 隐含）
- **外网登录闸门（`backend/app/gate.py`）**：只卡**浏览器外网**登录——免闸顺序：admin 角色 → `X-PMS-Client` 头（桌面客户端）→ 内网 IP → `gate_enabled=0`。过闸流程：login 验密码后 `issue_code`（6 位码只存 sha256、10 分钟、1条/分+10条/天限频、错5次锁）→ 码经 push_message 发 **manager 角色**企微（管理层核实后告知用户）→ `login/verify-gate` 验码发 token。配置：管理→外网访问（app_settings `gate_enabled`/`intranet_cidrs`；回环+私网 IP 恒判内网，故测试/本地不受影响）。**客户端真实 IP 优先取 X-Real-IP（nginx $remote_addr 覆写不可伪造），次取 XFF 末段**（首段可被伪造）
- **桌面客户端（desktop/）**：Electron 壳，内置打包 `frontend/dist`（`webSecurity:false` 绕 CORS，窗口只载内置页面+外链全交系统浏览器作补偿）；版本号 = `desktop/package.json`。前端以 `VITE_API_BASE` 区分：桌面打包设 `http://8.141.123.141`（axios baseURL/ws 直连服务器），浏览器构建不设（保持 `/api`）。统计头契约：preload 注入 `window.pmsDesktop{isDesktop,version,deviceId}` → axios 加 `X-PMS-Client/X-PMS-Device/X-PMS-User` → 后端中间件 60s 节流 upsert `desktop_clients` 表（main.py 模块级）。**API 只增不改**（老客户端长期并存），破坏性变更只能走 `--min-version` 强制升级流程

## 已知坑

- **13 个测试在基线 HEAD 上就挂**（历史欠账，与新改动无关）：`m01`(剩4个#91详单闸门)`/m02/m04/m07/m08/m12/m13/m14/m15`、e2e 两个、`outsourcing_template`、`user_feedback`、`void_sales_order`。验证某失败是否你引入的：`git worktree add /tmp/es-base HEAD` 后在基线上跑同一测试对比。（2026-07-21 菜单重构顺手修好 `mr_probe_menu`；`smoke_startup` 实测本就能过，旧清单偏旧）
- 前端 `npm run dev` 与 docker 里的构建是两回事；发版构建在服务器上做
- `docs/` 下的 HTML 设计稿是历史需求稿，不代表当前实现；`README.md` 内容偏旧（v2 时代），以本文件和 `docs/项目交接文档.md` 为准

## 当前状态（2026-07-28）

- **外网登录闸门已上线**：浏览器外网登录需随机码（码发 manager 角色企微；**已按用户要求去掉每日10条上限与错5次锁定**，仅保留 1 条/分钟间隔），admin/桌面客户端/内网 IP 免闸；管理→外网访问 配置开关与内网网段。**内网名单仍待用户配置**
- **第 17 批反馈 9 条（未提交，发版中）**：#313 请款审批推送收窄 finance→finance_lead（生产实证唯一审批人=杨坛）；#314 编辑采购明细可直接维护已付款金额/日期/方式（PurchaseItemUpdate 放开两字段，现金场景免走请款）；#318 外协图纸名称展示去重（spec 列 `名称·名称` 去重为单份，根源=下单时 drawing+spec 折叠存储，仅展示层改）；#316/#317 收货采购单号列加宽+关截断；#315 收货筛选框同时匹配采购单号/项目编号（改前端过滤，不再传后端 po_no 参数）；#319/#320 电路图文案修正（宋朴 1.0.4 客户端太旧所致+过时 toast 两处）；#311 管理层待办附图（biz_type=management_todo 复用 OA 附件链路，创建弹窗选填+详情预览）。测试：batch14/recv_po_project_filter/management_todo_attachments/login_gate 全 PASSED，vue-tsc exit=0
- **第 16 批反馈 #306-#310 + #309 已上线**（`785840e`/`0b4c024`，客户端 1.0.10/1.0.11）：五表独立上传、合并收货开单即算、供应商下拉可搜索、从清单下单跨项目模糊搜索未下单零件（purchasable-cross）；登录成功自动检查更新（1.0.12）
- 第 15/14 批反馈：见 git log（钳工入装配、上传推送分离、采购/仓库/生产/销售/OA 五域）
- 最近三期交付：
  1. **采购预计到货全链路**（`6de4548`/`d47afa1`）：`PurchaseItem.expected_arrival` 行级字段，清单下单逐行维护并回写五张项目详单，到期未到货每日提醒（`scan_po_arrival_overdue`）
  2. **Agent 助手 POC**（`86a1fa1`）：`POST /api/agent/chat` 只读问数，OpenAI 兼容 function calling，未配 Key 自动规则降级；页面化配置（admin 专属，存 `app_settings`，优先级 DB > env），模型白名单选择
  3. **Agent 优化**（`ae2c95d`）：回复 Markdown 渲染、追问建议 chips、按供应商聚合工具、菜单归入「管理」组
- **Agent 助手运维**：页面「管理→Agent 助手→配置」填 Base URL/API Key/模型即全局生效；env 为 `AGENT_LLM_BASE_URL/API_KEY/MODEL/MODELS`；Key 只回打码值、日志不记值
- 待办线索：16 个存量失败测试可另开一轮修；Agent 二期方向（写操作闭环/每日晨报主动推送/手机 App）方案在仓库外 `../Agent设计方案_ERP_CLI.html`
