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

# 发版（唯一路径）
bash ops/release.sh --push   # 本地 push main → SSH 服务器 upgrade.sh：备份→拉码→docker 重建→健康检查→失败自动回滚
                             # 部署配置在 .deploy.local（gitignored）；构建在服务器做，本地不构建
```

种子账号：`admin / admin123`、`manager / manager123`（seed 自动建）。运维脚本说明见 `ops/README.md`。

## 铁律

- **git 任何变更（commit/push/reset/rebase）必须用户当场明确授权**，每次单独确认；授权不过夜
- **图文一起读，先给附件归位**：反馈的截图不是配图，是内容本身——先确认每张截图挂在哪条反馈下、图上的标注（红框/箭头/圈）指向什么，再下结论（教训：#265「这个不用处理了」的截图就是她自己红框框出的 #263，单读文字才会觉得指代不明）
- **先质疑需求再实现**：对「让计算列可编辑」这类设计上就危险的诉求，先反问是否合理、是否有更简单的满足方式，再写代码
- 只改与任务相关的文件；不主动新建文档（用户要求除外）；改接口时同步更新调用方与注释
- Agent 助手（`backend/app/routers/agent_router.py`）的所有数据工具**永远只读 SELECT**，不提供收货/付款等强职责命令

## 关键约定

- **日期**：多为 ISO 字符串（`"2026-07-21"`），可直接字典序比较；业务时区 UTC+8，复用 `app/overdue.py` 的 `_CN_TZ` / `_cn_date`
- **消息幂等键** = `biz_type + biz_id + 当日`（参考 `overdue.py` 各 `scan_*`）；周期任务在 `--workers 4` 下必须 flock 单实例（同文件 `_try_acquire_scheduler_lock`）
- **菜单可见性唯一权威** = `backend/app/menus.py`：`MENU_DEFS`（业务区）/ `ADMIN_MENU_DEFS`（管理组）/ `ROLE_MENUS`（角色矩阵）。前端 `MainLayout.vue` 的 `ADMIN_EXTRA` 决定哪些 key 渲染进「管理」分组；auth store 的 `isAdmin` = admin **或** manager
- **建表**：新表靠 `Base.metadata.create_all` 启动自动建；存量表加列走 `app/data_migration.py`
- **角色**：admin/manager 全可见；其余走 ROLE_MENUS（未知角色默认 catalog+list）
- **桌面客户端（desktop/）**：Electron 壳，内置打包 `frontend/dist`（`webSecurity:false` 绕 CORS，窗口只载内置页面+外链全交系统浏览器作补偿）；版本号 = `desktop/package.json`。前端以 `VITE_API_BASE` 区分：桌面打包设 `http://8.141.123.141`（axios baseURL/ws 直连服务器），浏览器构建不设（保持 `/api`）。统计头契约：preload 注入 `window.pmsDesktop{isDesktop,version,deviceId}` → axios 加 `X-PMS-Client/X-PMS-Device/X-PMS-User` → 后端中间件 60s 节流 upsert `desktop_clients` 表（main.py 模块级）。**API 只增不改**（老客户端长期并存），破坏性变更只能走 `--min-version` 强制升级流程

## 已知坑

- **16 个测试在基线 HEAD 上就挂**（历史欠账，与新改动无关）：`m01/m02/m04/m07/m08/m12/m13/m14/m15`、e2e 两个、`mr_probe_menu`、`outsourcing_template`、`smoke_startup`、`user_feedback`、`void_sales_order`。验证某失败是否你引入的：`git worktree add /tmp/es-base HEAD` 后在基线上跑同一测试对比
- 前端 `npm run dev` 与 docker 里的构建是两回事；发版构建在服务器上做
- `docs/` 下的 HTML 设计稿是历史需求稿，不代表当前实现；`README.md` 内容偏旧（v2 时代），以本文件和 `docs/项目交接文档.md` 为准

## 当前状态（2026-07-20）

- **桌面客户端（未提交，待用户授权发版）**：`desktop/` Electron 壳 + 自动更新（electron-updater，generic 通道 = nginx `/desktop/` → 服务器 `$DEPLOY_PATH/desktop-releases/`，compose 已加卷）。发安装包：`bash desktop/release.sh`（--set-version/--min-version/--dry-run；读同一套 `.deploy.local`；Apple Silicon 已固化 `--x64`+npmmirror 镜像）。`version.json` 的 min_version 控制强制升级（低于它客户端只给「立即更新」一条路）。管理页「管理→桌面端」看在线版本分布（接口 `GET /api/admin/desktop-clients`，测试 `tests/test_desktop_clients.py`）。**注意：更新通道要先跑一次 `ops/release.sh --push` 部署（让 nginx 挂上 desktop-releases 卷）才生效**；首版 exe 已试打出（`desktop/dist/同辉项目管理 Setup 1.0.0.exe`，77MB，图标 `desktop/build/icon.png` 已嵌入）
- 最近三期交付：
  1. **采购预计到货全链路**（`6de4548`/`d47afa1`）：`PurchaseItem.expected_arrival` 行级字段，清单下单逐行维护并回写五张项目详单，到期未到货每日提醒（`scan_po_arrival_overdue`）
  2. **Agent 助手 POC**（`86a1fa1`）：`POST /api/agent/chat` 只读问数，OpenAI 兼容 function calling，未配 Key 自动规则降级；页面化配置（admin 专属，存 `app_settings`，优先级 DB > env），模型白名单选择
  3. **Agent 优化**（`ae2c95d`）：回复 Markdown 渲染、追问建议 chips、按供应商聚合工具、菜单归入「管理」组
- **Agent 助手运维**：页面「管理→Agent 助手→配置」填 Base URL/API Key/模型即全局生效；env 为 `AGENT_LLM_BASE_URL/API_KEY/MODEL/MODELS`；Key 只回打码值、日志不记值
- 待办线索：16 个存量失败测试可另开一轮修；Agent 二期方向（写操作闭环/每日晨报主动推送/手机 App）方案在仓库外 `../Agent设计方案_ERP_CLI.html`
