# n6 · 桌面客户端登录集成

> 探索主题：Electron 壳的登录集成——preload 注入 `window.pmsDesktop`、axios 三统计头、`webSecurity:false` 绕 CORS、API 只增不改 + `--min-version` 强制升级、登录态失效与黑屏自恢复。
> 边界：只读 `desktop/`；交叉引用 `frontend/src` 的 token 约定与登录接入点；不深挖发版脚本。
> 证据基于 2026-08-09 仓库当前代码。本文件为 n6 节点独立核实产物；同目录 `04-desktop-login.md` 为前一轮探索产物，行号证据一致，本文件侧重任务要求的六项接入点。

## 0. 一句话结论

桌面客户端**没有独立的登录协议**——登录仍是内置前端在 `file://` 下走标准 `POST /api/auth/login`，Electron 壳只做三件事：① preload 用 `contextBridge` 注入 `window.pmsDesktop{isDesktop,version,deviceId,...}`；② axios 拦截器凭这个全局加 `X-PMS-Client/X-PMS-Device/X-PMS-User` 三个统计头（后端用于免外网验证码闸 + 在线台账）；③ 用 `version.json` 的 `min_version`/`force_latest` 做强制升级闸门（启动时 + 登录按钮按下前两个时机）。登录态 = `localStorage` 里的 `pms_token`/`pms_user`，与浏览器完全同一套，所以渲染进程崩溃/整应用重启后**不需要重新登录**。

## 1. Electron 壳结构（desktop/）

- `desktop/package.json:3,6`：`version: "1.0.40"`（版本号 bump 随代码提交入库，见 AGENTS.md）、`main: "main.js"`。进程分工：`main.js`（785 行）主进程单文件、`preload.js` contextBridge 注入、`lib/health.js` 版本比较/崩溃判定（不依赖 Electron，可脱离 GUI 测试，`health.js:1-3`）。
- 内置前端 `desktop/app/`：打包时从 `frontend/dist` 拷入（`package.json:25-31` files 含 `app/**/*`），运行时 `createWindow` 里 `mainWindow.loadFile(app/index.html)`（`main.js:597-598`）。开发态没有 dist 时给 `data:` URL 人话提示（`main.js:600-604`）。
- 独立静态页 `desktop/renderer/{splash,force-update}.html`：启动页 / 强制更新页，都不走前端构建。

## 2. preload 注入 window.pmsDesktop

- 注入方式：`preload.js:8-42` 用 `contextBridge.exposeInMainWorld('pmsDesktop', {...})`，配合 `main.js:479-480` 的 `contextIsolation:true` + `nodeIntegration:false`——渲染进程无 Node 权限，`window.pmsDesktop` 是拿设备信息的唯一通道。
- 关键字段（`preload.js:13-16`）：`isDesktop:true`、`version`、`deviceId`，来自 `ipcRenderer.sendSync('pms-desktop:info')`（同步取，保证前端 axios 初始化时就能读到）。主进程 `ipcMain.on('pms-desktop:info')`（`main.js:196-198`）返回 `{version: app.getVersion(), deviceId, forceNotes}`。
- **deviceId 持久化**（`loadDeviceId`，`main.js:177-192`）：存 `userData/device.json`，首次 `crypto.randomUUID()` 生成；写失败置 `deviceIdPersisted=false`，本次仍可用但**每次启动换 ID**。启动时 `!deviceIdPersisted` 上报 `sendReport('error',...)`（`main.js:753-759`，注释点名杀毒软件拦 `%APPDATA%` 写入 → 服务端「客户端设备限制」永远认不出这台机器）。
- IPC 面（`preload.js:18-41`）：`notifyReady`（前端挂载完 → 关启动页亮主窗）、`checkUpdate`/`onUpdateStatus`（布局按钮主动检查）、`checkUpdateSilent`（登录成功后静默检查，30 分钟节流）、`enforceVersion`（登录前强制版本检查）、`forceUpdateNotes` + `onProgress/onDownloaded/onUpdateError/triggerUpdate/quitAndInstall`（强制更新页专用）。
- 画面心跳（`preload.js:44-55`）：每 2 秒一次 `pms-desktop:paint-beat`，用 `requestAnimationFrame` 而不是 setInterval——rAF 由合成器驱动，GPU 掉了 rAF 就停（注释 `preload.js:45-47` 说明为什么不能用定时器）。
- 前端类型声明：`frontend/src/vite-env.d.ts:11-26` `interface Window { pmsDesktop?: {...} }`，浏览器端 undefined，所有调用点都做 `?.` 空值判断。**类型里没有 `forceNotes`**（强制更新页是独立 HTML 不走 TS）。

## 3. axios 三统计头 + token 约定（交叉引用 frontend）

- **baseURL**（`frontend/src/api/index.ts:4-9`）：`(import.meta.env.VITE_API_BASE ?? '') + '/api'`。桌面构建注入绝对地址 → `http://8.141.123.141/api` 直连后端；浏览器构建不设 → `/api` 走 Vite 代理/nginx。
- **请求拦截器**（`api/index.ts:11-30`）：先加 `Authorization: Bearer <localStorage.pms_token>`，再当 `window.pmsDesktop?.isDesktop` 时加三个统计头——
  - `X-PMS-Client: desktop/<version>`
  - `X-PMS-Device: <deviceId>`
  - `X-PMS-User: <username>`（从 `localStorage.pms_user` 解析，解析失败不带该头，不影响请求）
  另：`config.responseType === 'blob'` 时 `config.timeout = 0`（`api/index.ts:28`，#188 大文件下载，nginx `proxy_read_timeout=300s` 兜底）。
- **token 约定**：登录成功后写 `localStorage.pms_token` + `pms_user`（`LoginView.vue:52-58` `finishLogin`，与 `stores/auth.login` 同一套持久化；闸门流程不走 store.login 而是按响应分支）。这就是"重启不重登"和桌面统计 `X-PMS-User` 的数据源。
- **ws 直连**（`frontend/src/composables/useRealtime.ts:38-49`）：桌面 `VITE_API_BASE` 推导 `http→ws/https→wss`，浏览器用 `location.host`。**ws 不带 X-PMS 头，身份靠 `?token=`**；`localStorage.pms_disable_ws==='1'` 可关。

## 4. webSecurity:false 与安全补偿

- **`webSecurity:false`**（`main.js:481-483`）：文件头注释（`main.js:6-10`）写明设计——页面从 `file://` 加载、API 在 `http://8.141.123.141`，放开才能让 axios 直连 HTTP 接口绕开 CORS。**配套约束**：壳为内部专用，窗口只加载打进包的 `frontend/dist`，不加载线上 URL。
- 相关 webPreferences（`main.js:477-491`）：`preload`、`contextIsolation:true`、`nodeIntegration:false`、`backgroundThrottling:false`（切后台不降频定时器）、`plugins:true`（#345 技术文档 PDF 预览，Chrome 内置 PDF 阅读器在 Electron 算 plugin 且默认 false）。
- **安全补偿三件套**（`main.js:578-588`）：
  1. `setWindowOpenHandler`：`http(s)` 链接交 `shell.openExternal`，一律 `deny` 不在窗口内新开。
  2. `will-navigate`：非 `file://` 一律 `preventDefault()` + `openExternal`。
  3. 内置前端本身不加载线上 URL（`main.js:5` 设计要点）。
- 启动链：`show:false` + `backgroundColor:'#0f1d30'`（`main.js:475-476`，与登录页同底色防闪白）→ `createSplash()`（`main.js:135-156`，无框 400×470 启动页）→ 前端 `main.ts` 调 `notifyReady` → `ipcMain.on('pms-desktop:app-ready')`（`main.js:170`，延时 300ms 防白屏闪一下）`revealMainWindow()`（`main.js:159-167`）。兜底：10s 后强制亮窗（`main.js:496`，防老版本内置页不发 app-ready）。

## 5. API 只增不改兼容约束 + --min-version 强制升级

### 5.1 兼容红线
AGENTS.md 明确：**API 只增不改**（老客户端长期并存），破坏性变更只能走 `--min-version` 强制升级流程。因此破坏性接口变更的合规路径不是直接改后端，而是先 `--min-version` 逼老客户端升级。

### 5.2 版本源与判定
- 版本源：服务器 nginx 静态目录 `/desktop/` 提供 `version.json` + `latest.yml` + `*.exe(.blockmap)`。客户端常量（`main.js:23-24`）：`UPDATE_BASE_URL='http://8.141.123.141/desktop/'`、`VERSION_JSON_URL=...+'version.json'`。
- 当前仓库 `desktop/version.json`：`{"min_version":"1.0.33","notes":"...","force_latest":true}`——**`force_latest:true` 生效中**，意味着判定口径是"必须是通道最新版"。
- 判定逻辑（`main.js:310-329` `checkForceUpdate`）：`fetch(version.json, {cache:'no-store', timeout:5000})` → `requiredVersion(j, j.force_latest ? await latestChannelVersion() : '', true)` → 当前版本低则返回 `{...j, need}`。`latestChannelVersion` 从 `latest.yml` 正则抠 `version:` 行（`main.js:297-308`）。
- `lib/health.js:45-50` `requiredVersion`：`min_version`（手工地板）与 `force_latest` 时的通道最新版**取更高者**；`health.js:27-36` `compareVersions` 简易 x.y.z 数字段比较。
- **网络不通一律放行**（`main.js:310` 注释）：宁可漏拦，不能因服务器抖一下把人锁在门外。

### 5.3 两个检查时机
1. **启动时**（`main.js:749-771`）：打包模式 `await checkForceUpdate()` → `forceMode=true` 则 `createWindow()` 加载 `renderer/force-update.html` + `setupForceUpdateIpc()`，否则 `createWindow()` + `setupAutoUpdate()`。
2. **登录按钮按下前**（`main.js:331-350` `enforceVersionBeforeLogin`，`ipcMain.handle('pms-desktop:enforce-version')`）：注释写明动机——客户端可连开好几天不重启（还有 30 天免登录），期间发新版只靠启动那一次查不到。命中则 `mainWindow.loadFile(force-update.html)` 直接切强制更新页。`enforcing` 防重入。前端调用点 `LoginView.vue:78`（`onSubmit` 第一步，浏览器端 `pmsDesktop` undefined 整个跳过）。

### 5.4 强制更新页逻辑（`setupForceUpdateIpc`，`main.js:689-726`）
- 先 `clearInterval(autoUpdateTimer)` + `autoUpdater.removeAllListeners()`（`main.js:695-696`）——注释：登录前复检切进强制模式时，常规更新监听不摘会两套逻辑双跑，用户先看到常规「立即重启更新」框、点取消就卡死。
- `update-downloaded` → 写 `PENDING_FILE()` + `autoUpdater.quitAndInstall()` 自动重启安装（`main.js:704-708`），**不给绕过出口**；`not-available`/`error` → 提示稍后重试，无绕过。`forceIpcBound` 防 `ipcMain.on` 重复注册（`main.js:718`）。
- 常规自动更新（`setupAutoUpdate`，`main.js:643-687`）：`autoDownload=true`，启动即查 + 4h 轮询（`UPDATE_INTERVAL_MS`，`main.js:25`）；`update-downloaded` 写 PENDING 后弹原生对话框「立即重启更新/稍后」（`main.js:660-676`）；`error` 静默落 crash.log。登录后静默检查 30 分钟节流（`main.js:629-640`）。

### 5.5 PENDING_FILE 回溯机制（old-uninstaller 崩溃）
安装器在 app 退出后才跑，崩溃时客户端已不在，无法当场上报。`main.js:263-285` `reportPendingUpdateFailure`：下载完成记目标版本到 `userData/pending-update.json`，下次启动若当前版本没变 = 安装失败，上报 `update_failed`（成功才清标记，失败累加 attempts 下轮再报）。启动不 await（`main.js:752`）。

## 6. 登录态失效（401）处理

- 401 拦截（`api/index.ts:122-140`）：`status===401 && !isLoginRequest` → `removeItem('pms_token'/'pms_user')` → `goLogin()`；登录请求本身 401 不跳转。
- **goLogin 分路由模式**（`api/index.ts:90-120`）：`HASH_ROUTER = !!VITE_API_BASE`（与 `router/index.ts:8` 判据完全一致）。
  - 桌面（file://，hash 模式）：`location.hash = '#/login'` + `location.reload()`（清残留 store 状态）。
  - 浏览器（history 模式）：`location.href = '/login'`。
- **#343 黑屏根因**（`api/index.ts:94-106` 注释）：桌面端**绝不能**写 `location.href='/login'`——file:// 下解析成 `file:///login`（不存在），导航失败后 Chromium 换空错误页、一屏不画，只剩 backgroundColor 的深蓝，每天 8h token 到期必踩一次（近 14 天 8 人 23 次）。已修：桌面走 hash 跳转。`redirecting` 防一次 401 风暴里跳多次（`api/index.ts:108`）。
- 免登录期：AGENTS.md / `main.js:332` 注释提及"30 天免登录"（token 有效期相关，详见 `01-backend-auth.md`），登录态失效后桌面端只能回登录页重新走 `POST /api/auth/login`（免闸见下）。

## 7. 黑屏自恢复（渲染崩溃 / GPU 崩 / 画面心跳）

分层三道（按触发条件从内到外）：

1. **did-fail-load 兜底**（`main.js:497-512`）：主框架加载失败（非 `-3` ERR_ABORTED）→ `logCrash` + 上报 + 退内置首页 `loadFile(indexHtml)`。注释点明 #343 真身是 401 后 `location.href='/login'`（前端已修），这里再加一道。`failLoadRecovering` 防失败风暴。
2. **render-process-gone / unresponsive**（`main.js:520-576`）：渲染进程崩溃/被杀/OOM → `recover()`；unresponsive 卡满 15s 仍不恢复 → `recover()`。`recover()`（`main.js:525-564`）先 `reload()`；连重载 `RELOAD_MAX_ATTEMPTS=3` 次仍不恢复（`RECOVER_WINDOW_MS=5min` 窗口内，`health.js:70-73`）→ `relaunchApp()` 整应用重启（`health.js:91-97` `reloadAction` 注释含 2026-08-04 李新新那台"重载完 0.5 秒又卡"的生产实测）。token 在 localStorage，重启后不用重登（`main.js:519` 注释）。
3. **GPU 进程崩 → 画面心跳**（`main.js:58-132`）：`child-process-gone` 里 `details.type==='GPU'` → 打 `.gpu-crashed` 标记 + 20s 后 `checkPaint`；画面心跳 `lastPaintBeat`（preload rAF 每 2s 上报）超 `PAINT_STALL_MS=45s` 且窗口有焦点、非最小化（`health.js:22-25`，注释：必须用 `focused` 不能用 `isVisible`，实测不在前台 rAF 会被节流）→ `relaunchApp`。息屏/锁屏/唤起先把计时清零再判（`main.js:124-127`）。**GPU 崩不能用 reload 救**（合成器活在 GPU 进程，`health.js:58-62` 注释）。
- 重启守卫：`RELAUNCH_GRACE_MS=120s` 刚启动不重启（防开机循环，`health.js:52-53,64-68`）；`paintRelaunching`/`reloadingAfterCrash`/`alreadyRelaunching` 防并发。
- GPU 降级：`.gpu-crashed` 标记存在 → 下次启动 `app.disableHardwareAcceleration()`（`main.js:48-56`）。
- 主进程异常：`uncaughtException` → crash.log + 上报（`main.js:287-291`）；`unhandledRejection` 只落日志（`main.js:292-294`）。
- 故障上报 `sendReport`（`main.js:226-249`）：POST `http://8.141.123.141/api/desktop/report`，8s 超时，失败吞掉；`POST /api/desktop/report` **故意不认证**（`desktop_router.py`，登录前崩溃也要收得到），防滥用三道：kind 白名单、detail 截断 64KB、每 device 每天 20 条超限返 200。

## 8. 登录成功后的桌面专属动作

- `LoginView.vue:25`：`deviceId` 仅桌面端显示，供用户复制发给管理层录设备名单（`file://` 下 clipboard API 可能不可用，退回 `document.execCommand('copy')`，`LoginView.vue:27-43`）。
- `LoginView.vue:63`：`finishLogin` 末尾 `window.pmsDesktop?.checkUpdateSilent?.()`（静默检查更新）。
- `LoginView.vue:78`：`onSubmit` 第一步 `await window.pmsDesktop?.enforceVersion?.()`，返回 true（版本过低、已切强制更新页）就直接 return 不再登录。
- 主进程登录感知只有两个点：`check-update-silent` IPC 与统计中间件（后端 `X-PMS-User` 头）——**主进程不知道用户是否登录成功**，没有"登录成功"事件给主进程。

## 9. 反例 / 排除项（下一棒不必重走）

- **不存在「桌面登录协议」**：没有 `/api/auth/desktop-login` 之类独立端点；桌面登录 = 标准 `login` + `X-PMS-Client` 头免闸（后端 `auth_router.py:90-97` 免闸判定链：admin 角色 → 桌面客户端头 → 内网 IP → `gate_enabled=0`；`gate.desktop_exempt`，`gate.py:67-85`：`device_gate` 默认关 → 装了客户端就免闸，开 → 还要 device_id 落名单）。
- **ws 不带头**：`useRealtime.ts` 的 ws 不带 X-PMS 头，身份靠 `?token=`——ws 断连只影响实时推送与统计最近在线，不影响登录。
- **webSecurity:false 不是无防线**：CORS 放开只对内置 `file://` 页有效；外链被导航防护挡在窗口外。但 **X-PMS-Client 头可被任何能发 HTTP 的客户端伪造**（如 curl 加 `X-PMS-Client: desktop/9.9.9`），`device_gate` 默认关 → 外网浏览器场景下验证码闸可被伪造头绕过（后端真防线是 device_gate 设备名单，默认关闭，`gate.py:67-85` 注释）。
- **`--min-version` 不是每次启动都强制**：只有启动时 + 登录按钮按下前两个时机；网络不通放行（`main.js:310-314` 设计取舍注释）。
- **登出/切换账号**：桌面端走与浏览器相同的 `POST /api/auth/logout` 与 store 清理（属前端交互逻辑，本片不深挖，见 `03-frontend-login.md`）。

## 10. 分歧 / 遗留

- **`force_latest:true` 与「老客户端长期并存」存在张力**（`version.json:3`）：当前判定口径是"必须通道最新版"，意味着每次发新版所有旧客户端都会被强制升级——与 AGENTS.md「API 只增不改、老客户端长期并存」并非同一套节奏，属有意为之（`notes` 字段说明），未进一步考证发版口径。
- **外网免闸实际依赖"客户端头"而非设备名单**：`device_gate` 默认关、内网 `intranet_cidrs` 默认空（AGENTS.md「当前状态」注明内网名单仍待配置）。因此外网浏览器场景下，免闸条件收敛为"admin/manager 角色"或"伪造得出来的客户端头"。
- **主进程不感知登录态**：崩溃自恢复、重启都不关心是否已登录（token 在 localStorage 天然续命）；这意味着若未来要"退出登录时清空会话缓存/上报注销设备"，需要新增 IPC，当前没有。

## 11. 相关文件索引

| 文件 | 关注段 |
|---|---|
| `desktop/preload.js` | 全文（contextBridge 注入 + 画面心跳） |
| `desktop/main.js` | 6-10 webSecurity 设计；48-56 GPU 降级；58-132 GPU/心跳；134-198 启动页/deviceId/info；226-308 上报/回溯/版本工具；310-350 强制检查+登录前复检；468-606 createWindow+崩溃自恢复；608-687 常规更新；689-726 强制更新；729-785 生命周期 |
| `desktop/lib/health.js` | 全文（shouldRecoverPaint/reloadAction/requiredVersion） |
| `desktop/version.json` | min_version/notes/force_latest |
| `desktop/package.json` | 3 version；6 main；25-31 files |
| `desktop/renderer/{splash,force-update}.html` | 启动页/强制更新页 |
| `frontend/src/api/index.ts` | 4-9 baseURL；11-30 三统计头；90-120 goLogin/hash；122-140 401 |
| `frontend/src/composables/useRealtime.ts` | 38-49 ws 地址推导 |
| `frontend/src/views/LoginView.vue` | 25 deviceId；52-66 finishLogin；68-92 onSubmit（enforceVersion/gate） |
| `frontend/src/vite-env.d.ts` | 11-26 pmsDesktop 类型 |
| `frontend/src/router/index.ts` | 8 hash 路由判据 |
| `frontend/vite.config.ts` | 10 base='./' 桌面相对路径 |
| `backend/app/routers/auth_router.py` | 90-97 免闸判定链 |
| `backend/app/gate.py` | 67-85 desktop_exempt；24-26 配置键；54 is_intranet |
| `backend/app/routers/desktop_router.py` | report 不认证上报 + 管理页 |
