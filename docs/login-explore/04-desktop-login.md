# 04 · 桌面客户端登录接入

> 探索主题：Electron 壳如何登录（窗口加载 / preload 注入 / 请求头 / VITE_API_BASE 直连 / 强制升级 / 后端免闸与设备台账）。
> 边界：不涉及前端页面（LoginView）的登录交互逻辑本身。
> 证据为 desktop/、frontend/src、backend/app 当前代码（2026-08-09）。

## 0. 一句话结论

桌面客户端**没有独立的登录协议**：登录仍是前端页面在 `file://` 下跑标准 `POST /api/auth/login`，Electron 壳负责三件事——① 加载内置前端并注入 `window.pmsDesktop{isDesktop,version,deviceId}`；② 用 `X-PMS-Client/X-PMS-Device/X-PMS-User` 三个请求头把"这是桌面客户端 + 这台机器是谁"告诉后端（免闸 + 在线统计 + 设备名单三重用途）；③ 用 `VERSION_JSON_URL`（`/desktop/version.json`）的 `min_version`/`force_latest` 做强制升级闸门。**所有环节只有数据头与版本闸，登录密码校验、token 签发、验证码流程全部复用浏览器端同一套。**

## 1. Electron 壳总体结构（desktop/）

- `desktop/package.json:3,6`：`version: "1.0.40"`，`main: "main.js"`。版本号 = 仓库里桌面端版本（bump 随代码提交入库，见 AGENTS.md）。
- 进程分工（无 TypeScript，纯 JS 单文件）：
  - `desktop/main.js`（785 行）主进程：窗口创建、preload 注入、更新检查（常规 4h 轮询 + 登录前强制复检）、崩溃/卡死自恢复、故障上报。
  - `desktop/preload.js`：`contextBridge` 注入 `window.pmsDesktop`。
  - `desktop/lib/health.js`：版本比较工具 `compareVersions` / `requiredVersion`。
  - `desktop/renderer/{splash,force-update}.html`：启动页 / 强制更新页（都是独立静态页，不走前端构建）。
  - `desktop/app/`：**打包时**从 `frontend/dist` 拷入的内置前端（`release.sh:6-8,148`），运行时主进程 `loadFile(desktop/app/index.html)`。

## 2. 窗口加载与 webSecurity:false

窗口创建 `createWindow()`（`main.js:468-606`）：

- `webPreferences`（`main.js:477-491`）：
  - `preload: preload.js`、`contextIsolation: true`、`nodeIntegration: false` —— 渲染进程无 Node 权限，只有 preload 桥接的受限 API。
  - **`webSecurity: false`**（`main.js:481-483`）：注释写明理由——页面从 `file://` 加载、API 在 `http://8.141.123.141`，放开以绕开 CORS；壳为内部专用，窗口只加载打进包的前端，不加载线上 URL。
  - `backgroundThrottling: false`（485）：窗口切后台不让定时器降频（心跳/自动刷新被掐会看到死数据）。
  - `plugins: true`（490）：反馈 #345「技术文档无法下载」，Chrome 内置 PDF 预览器在 Electron 里算 plugin 且默认 false。
- `show: false`（475）先隐藏 + `backgroundColor:'#0f1d30'`（476）与登录页同底色，启动页挡住，就绪后才亮窗。
- 启动链：`createSplash()`（`main.js:747`）亮无框 400×470 启动页 → 前端 `main.ts:24` `window.pmsDesktop?.notifyReady?.()` → 主进程 `ipcMain.on('pms-desktop:app-ready')`（`main.js:170`）延时 300ms `revealMainWindow()`。兜底：`setTimeout(revealMainWindow, 10000)`（`main.js:496`）防止老版本内置页不发 app-ready。
- **加载失败兜底（#343 黑屏真身）**（`main.js:497-507`）：`did-fail-load` 主框架失败（非 `-3` ERR_ABORTED）→ `loadFile(indexHtml)` 退回内置首页。注释解释 #343 真身是前端 401 后 `location.href='/login'` 在 `file://` 下解析成 `file:///login`（不存在），前端已修，这里再加一道兜底。
- 渲染进程崩溃自恢复（`main.js:565-576`）：`render-process-gone` / `unresponsive`（15s 判死）→ `recover()` 重载；连续重载 3 次救不回来（5 分钟窗口）自动 `relaunchApp()`。GPU 崩溃标记 `.gpu-crashed`，下次启动 `disableHardwareAcceleration()` 降级（`main.js:37,52-53,71,111`）。
- 导航防护（`main.js:578-588`）——webSecurity:false 的补偿：
  - `setWindowOpenHandler`：http(s) 链接交给 `shell.openExternal`，一律 `deny` 不在窗口内新开。
  - `will-navigate`：非 `file://` 一律 `preventDefault()` + `openExternal`。
- 加载目标（`main.js:590-605`）：`forceMode` 时 `loadFile(renderer/force-update.html)` 并 `ready-to-show` 亮窗；否则 `fs.existsSync(app/index.html)` 才 `loadFile`，开发态没拷 dist 时给 `data:` URL 提示语。

## 3. preload 注入 window.pmsDesktop

- `desktop/preload.js` 用 `contextBridge.exposeInMainWorld('pmsDesktop', ...)`（配合 `contextIsolation:true` + `nodeIntegration:false`，这是渲染进程拿设备信息的唯一通道）。
- `ipcMain.on('pms-desktop:info')`（`main.js:196-198`）同步返回 `{version, deviceId, forceNotes}`；preload 把它桥接成 `window.pmsDesktop` 的 `isDesktop/version/deviceId` 等字段。
- 前端类型声明 `frontend/src/vite-env.d.ts:12` `pmsDesktop?: {...}`（浏览器端 undefined，所有调用都做空值判断）。
- **deviceId 持久化**（`loadDeviceId`，`main.js:176-193`）：存 `app.getPath('userData')/device.json`，首次用 `crypto.randomUUID()` 生成，写失败置 `deviceIdPersisted=false`。启动时 `!deviceIdPersisted` 上报 `sendReport('error', ...)`（`main.js:755-759`，注释：杀毒软件拦 `%APPDATA%` 写入 → 每次启动换 ID → 服务端「客户端设备限制」永远认不出这台机器）。

## 4. 前端请求头注入（axios + ws）

- **baseURL**（`frontend/src/api/index.ts:4-9`）：`import.meta.env.VITE_API_BASE ?? ''` + `/api`。桌面构建注入绝对地址 → `http://8.141.123.141/api`；浏览器构建不设 → `/api` 走 Vite 代理/nginx。
- **请求拦截器**（`api/index.ts:11-30`，注释 `:14`）：有 `window.pmsDesktop` 时加三个统计头——
  - `X-PMS-Client: desktop/<version>`
  - `X-PMS-Device: <deviceId>`
  - `X-PMS-User: <username>`（从 localStorage 存的登录用户解析）
  另加 `Authorization: Bearer <pms_token>`。桌面端 `blob` 下载 `timeout: 0`（#188，PDF/文档大文件）。
- **401 处理 goLogin**（`api/index.ts:91,107-120`）：`HASH_ROUTER = !!VITE_API_BASE`；桌面（file://）用 hash 路由跳 `#/login`，浏览器用 `location.href='/login'`。注释详述 #343：`location.href='/login'` 在 file:// 下解析成 `file:///login` → 整片深蓝黑屏。
- **ws 直连**（`frontend/src/composables/useRealtime.ts:39-49`）：桌面构建 `VITE_API_BASE` 推导 `http→ws`、`https→wss`；浏览器用 `location.host`。ws 不带 X-PMS 头，靠 `?token=` 传参。另有 `localStorage.pms_disable_ws==='1'` 可关。

## 5. VITE_API_BASE 构建约定（桌面/浏览器两套产物）

- **桌面构建**（`desktop/release.sh:143-148`）：`VITE_API_BASE=http://8.141.123.141 npm run build --prefix frontend` → `cp frontend/dist desktop/app/`（打进安装包的内置页）。API 直连服务器绝对地址。
- **vite 相对路径**（`frontend/vite.config.ts`）：`base = VITE_API_BASE ? './' : '/'`——file:// 下必须相对路径否则白屏。
- **hash 路由**（`frontend/src/router/index.ts:8`）：`VITE_API_BASE ? createWebHashHistory() : createWebHistory()`。桌面 `file://` 下 hash 路由才能工作（配合 401 跳转 #343 的修法）。
- **浏览器/服务器构建**：不设 `VITE_API_BASE`，保持 `/api` 走 nginx 反代。AGENTS.md 明确「前端 npm run dev 与 docker 里的构建是两回事；发版构建在服务器上做」——但桌面端内置前端是发版时用 release.sh 在本机/CI 打的，不是服务器构建。
- **API 兼容红线**（AGENTS.md）：API 只增不改（老客户端长期并存），破坏性变更只能走 `--min-version` 强制升级流程。

## 6. 后端：免闸判定 + 设备台账统计中间件

- **免闸判定链**（`backend/app/routers/auth_router.py:86-109`，login 验密后）：非 admin 角色 →
  1. `is_desktop = x-pms-client 头 startswith("desktop/")`（`:90`）；`device_id = x-pms-device 头`（`:91`）。
  2. `cfg = gate.get_gate_config(db)`（`:93`）。
  3. `exempt = is_intranet(ip, cfg["cidrs"]) or desktop_exempt(is_desktop, device_id, device_gate=cfg["device_gate"], device_ids=cfg["device_ids"])`（`:94-97`）。
  4. `cfg["enabled"] and not exempt` → `gate.issue_code()` 下发 6 位码 + 返回 `GateRequiredOut`（`gate_required=True`），否则 `_issue_token()` 直接发 token。
  - 免闸顺序（注释 `:86-88` + `gate.py` 头注释 `:3`）：admin 角色 → 桌面客户端（X-PMS-Client 头；`device_gate` 打开后还要求 `X-PMS-Device` 落在手工名单里）→ 内网 IP → `gate_enabled=0`。
- **`gate.desktop_exempt`**（`backend/app/gate.py:67-85`）：`device_gate` 关（默认）→ 装了客户端就免闸；开 → 还要 `device_id` 在 `device_ids` 名单里，否则照样走验证码。配置存 `app_settings`：`gate_enabled`（默认开）、`device_gate_enabled`（默认关）、内网网段、设备名单（管理→外网访问页维护）。
- **统计中间件**（`backend/app/main.py:132-157`）：
  - `@app.middleware("http")` `desktop_client_stats`：`x-pms-client` 以 `desktop/` 开头且非 OPTIONS → 读 `x-pms-device` → **60s 节流**（模块级 `_desktop_last_write: dict[str,float]`，`main.py:40-43`，先占位再写库防并发）→ `_upsert_desktop_client(device_id, version, username)`。
  - 写库异常只 `log.exception`，绝不影响业务请求（`:156`）。
  - 挂在 CORS 中间件之后注册（执行更靠外），不改动 CORS 本身（`:137-138`）。
- **`DesktopClient` 表**（`backend/app/models.py:1157-1167`）：`desktop_clients`，`device_id` 唯一 + `version` + `username`（最近登录名，仅展示）+ `last_seen` + `created_at`；由 `Base.metadata.create_all` 自动建，无迁移脚本。注释：管理页展示在线版本分布。
- 管理页读取：`backend/app/routers/desktop_router.py:35-36`（GET admin/desktop-clients）、`61-62`（GET admin/desktop-reports）、`96`（POST .../handled）。

## 7. --min-version 强制升级流程

- **版本源**：服务器 nginx 静态目录 `/desktop/`（`desktop/release.sh:10,164-170` 上传）提供 `version.json` + `latest.yml` + `*.exe(.blockmap)`。仓库内 `desktop/version.json` 现值为 `{"min_version":"1.0.33","notes":"…","force_latest":true}`。
- **客户端常量**（`main.js:23-24`）：`UPDATE_BASE_URL='http://8.141.123.141/desktop/'`、`VERSION_JSON_URL=UPDATE_BASE_URL+'version.json'`。
- **`checkForceUpdate()`**（`main.js:315-329`）：`fetch(version.json, {cache:'no-store', timeout:5000})` → `requiredVersion(j, force_latest ? await latestChannelVersion() : '', true)` → 当前版本低则返回 `{...j, need}`。**网络不通视为不强制**（注释：宁可漏拦，不因服务器抖一下把人锁在门外）。
  - 两种口径（`main.js:311-314`）：`min_version` 手工地板；`force_latest`「必须是通道最新版」，判定取两者更高（`lib/health.js:45-50` `requiredVersion`；`:28-36` `compareVersions` 简易 x.y.z 数字段比较）。
- **两个检查时机**：
  1. 启动时（`main.js:762-769`）：打包模式先 `checkForceUpdate()` → `forceMode` 则 `createWindow()` + `setupForceUpdateIpc()`（加载 `renderer/force-update.html`），否则 `createWindow()` + `setupAutoUpdate()`。
  2. **登录按钮按下前**（`enforceVersionBeforeLogin`，`main.js:334-350`）：`ipcMain.handle('pms-desktop:enforce-version')`。注释：客户端可能连开好几天不重启（有 30 天免登录），期间发版一直查不到；前端 LoginView.vue:78 登录前 `await window.pmsDesktop?.enforceVersion?.()`，过期直接切强制更新页。`enforcing` 防重入。
- **前端调用点**：`frontend/src/views/LoginView.vue:25`（取 deviceId）、`:63`（登录成功后 `checkUpdateSilent()`）、`:78`（登录前 `enforceVersion()`）；`MainLayout.vue:94-95`（布局「检查更新」按钮，浏览器端按钮不渲染）。
- **强制更新页逻辑**（`setupForceUpdateIpc`，`main.js:691-726`）：
  - 先 `clearInterval(autoUpdateTimer)` + `autoUpdater.removeAllListeners()`（`:695-696`，注释：登录前复检切进强制模式时，常规监听不摘会两套逻辑双跑，用户先看到常规「立即重启更新」框、点取消就卡死）。
  - `force-update:trigger` → `checkForUpdates()`；`update-downloaded` → 写 `PENDING_FILE()` + `send('force-update:downloaded')` + **`autoUpdater.quitAndInstall()` 自动重启安装，不给绕过出口**（`:704-708`）。
  - `update-not-available`/`error` → 提示稍后重试（无绕过，只能重试）；`force-update:quit` → `quitAndInstall`（`:725`）。
  - `forceIpcBound` 防 `ipcMain.on` 重复注册（`:718`）。
- **常规自动更新**（`setupAutoUpdate`，`main.js:643-687`）：`autoDownload=true`，启动即查一次 + 4h 轮询（`UPDATE_INTERVAL_MS`）；`update-downloaded` 写 `PENDING_FILE` 后弹原生对话框「立即重启更新/稍后」（`:660-676`）；`error` 静默落 `crash.log`。
  - **PENDING_FILE 回溯机制**（`:661-663,705` + `reportPendingUpdateFailure` `main.js:265-285`）：下完记目标版本到 `userData/pending.json`；下次启动若当前版本没变 = 安装失败（old-uninstaller 崩溃），上报 `update_failed`。启动不 await（`:752`）。
  - 登录后静默检查（`main.js:630-640`）：`pms-desktop:check-update-silent`，**30 分钟节流**，不往布局按钮推状态。
  - 手动检查（`main.js:611-627`）：`pms-desktop:check-update`，`manualChecking` 标记下才推 `pms-desktop:update-status` 给前端（后台轮询静默）。
- **发布侧**：`desktop/release.sh --min-version 1.1.0` 改 `version.json` 的 `min_version` 后上传（`:16,44,133-141`）；`--upload-only <目录>` 只传不打包（配合 GitHub Actions Windows 原生打包，见 AGENTS.md）。

## 8. 故障上报链路（刻意不要求认证）

- **`POST /api/desktop/report`**（`backend/app/routers/desktop_router.py:120-160`）：**故意不认证**——最需要抓的场景（升级失败、启动崩溃）发生在登录之前，挂鉴权就永远收不到。
- 防滥用三道（`:125-130` 注释）：① `kind` 白名单（`update_failed/crash/error`，`:131`）；② `detail` 截断 64KB 只留尾部（`:154`）；③ 每 `device_id` 每天最多 20 条，超出**返回 200 而不是 429**（不给探测者限流阈值反馈）（`:134-142`）。只存文本，不解析、不回显。
- 用 device_id 反查台账带 `username`（纯展示用，上报时可能还没登录）（`:144-148`）。
- 客户端侧 `sendReport`（`main.js:228-249`）：POST `http://8.141.123.141/api/desktop/report`，8s 超时，失败吞掉不影响主流程。触发点：崩溃（`:68,109,532,547,566`）、`uncaughtException`（`:290`）、`did-fail-load`（`:507`）、更新失败（`:276`）、deviceId 未落盘（`:756`）。
- `DesktopReport` 表（`models.py:1169-1186`）：`desktop_reports`，`kind` 索引、`handled` 处理标记、`extra` JSON。注释记载起因：old-uninstaller 崩溃导致部分机器永远升不了级，排查时无转储无日志。

## 9. 反例 / 排除项（下一棒不必重走）

- **不存在「桌面登录协议」**：没有 `/api/auth/desktop-login` 之类的独立端点；桌面登录 = 标准 `login` + `X-PMS-Client` 头免闸。找了 `backend/app/routers/` 下所有 `X-PMS`/`desktop` 引用，除统计中间件、gate 免闸、desktop_router（上报+管理页）外无第三处。
- **ws 不带头**：`useRealtime.ts` 的 ws 地址从 `VITE_API_BASE` 推导，但不带 `X-PMS-*` 头，身份靠 `?token=`——所以 ws 连不上时统计会缺该设备的最近在线，但登录本身不受影响。
- **webSecurity:false 不是无防线**：CORS 绕开只对内置 `file://` 页面有效，导航防护（will-navigate/setWindowOpenHandler）把一切外链挡在窗口外、交系统浏览器；此点 `main.js:9,578-588` 双保险。
- **`--min-version` 不是每次启动都强制**：只有启动时 + 登录按钮按下前两个时机检查；且网络不通放行。注释 `main.js:310-314` 明说设计取舍。

## 10. 分歧 / 遗留

- **gate 内网名单仍未配置**（AGENTS.md「当前状态」注明）：`is_intranet` 依赖管理端配置 `intranet_cidrs`，默认空；外网场景下免闸实际靠「桌面客户端头」与「admin/manager 角色」。
- `device_gate`（客户端设备名单限制）默认关闭；开启后 X-PMS-Device 必须落名单才免闸。若曾出现「用户说批了还是要验证码」，先查 `device.json` 是否被杀软拦截（启动上报 `device.json 写入失败` 会进 `desktop_reports`）。
- 桌面端 `force_latest: true` 当前在 `version.json`（`desktop/version.json:3`）——意味着每次发新版旧客户端都会被强制，与「老客户端长期并存」存在张力，属有意为之（notes 说明），未进一步考证。

## 11. 相关文件索引

| 文件 | 关注段 |
|---|---|
| `desktop/main.js` | 23-24 常量；52-111 GPU 降级；134-198 启动页/deviceId/info；228-308 上报/版本工具；315-350 强制检查+登录前复检；468-606 createWindow；611-726 更新三套逻辑；729-785 生命周期 |
| `desktop/preload.js` | contextBridge 注入全文件 |
| `desktop/lib/health.js` | compareVersions/requiredVersion |
| `desktop/renderer/{splash,force-update}.html` | 启动页/强制页 |
| `desktop/release.sh` | 143-148 桌面打包；133-141 min_version；164-170 上传 |
| `desktop/version.json` | min_version/notes/force_latest |
| `frontend/src/api/index.ts` | 4-9 baseURL；11-30 拦截器三头；91,107-120 401/hash 跳转 |
| `frontend/src/composables/useRealtime.ts` | 39-49 ws 地址推导 |
| `frontend/src/views/LoginView.vue` | 25/63/78 桌面接入点（登录交互逻辑本身不在本片） |
| `frontend/vite.config.ts` | base='./' 桌面相对路径 |
| `frontend/src/router/index.ts` | 8 hash 路由判据 |
| `backend/app/gate.py` | 67-85 desktop_exempt；54 is_intranet |
| `backend/app/routers/auth_router.py` | 86-109 免闸判定链 |
| `backend/app/main.py` | 40-43 节流状态；132-157 统计中间件 |
| `backend/app/routers/desktop_router.py` | 120-160 report 不认证上报 |
| `backend/app/models.py` | 1157-1167 DesktopClient；1169-1186 DesktopReport |
