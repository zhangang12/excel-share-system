# n19 · 核实#3 · 证伪 Electron 主进程登录事件

> 要核的说法：Electron 主进程完全没有登录成功/登出事件：崩溃自恢复与重启靠 localStorage 里的 pms_token 天然续命，主进程无法感知登录态，未来要做「注销时上报设备下线/清会话缓存」需新增 IPC
> 默认立场：先证伪；证伪不掉才算成立。
> 证据基于 2026-08-09 仓库当前代码。

## 判决：PASS（说法成立，证伪失败）

## 1. 主进程确实完全没有登录成功/登出事件

### 1.1 主进程全部 IPC 通道清单（main.js）

| 通道 | 类型 | 位置 | 用途 |
|------|------|------|------|
| `pms-desktop:paint-beat` | `ipcMain.on` | main.js:83 | 画面心跳 |
| `pms-desktop:app-ready` | `ipcMain.on` | main.js:170 | 前端挂载完成，亮主窗 |
| `pms-desktop:info` | `ipcMain.on` | main.js:196 | 返回 version/deviceId |
| `pms-desktop:enforce-version` | `ipcMain.handle` | main.js:350 | 登录前版本复检 |
| `pms-desktop:check-update` | `ipcMain.on` | main.js:614 | 布局按钮主动检查更新 |
| `pms-desktop:check-update-silent` | `ipcMain.on` | main.js:633 | 登录后静默检查更新 |
| `force-update:trigger` | `ipcMain.on` | main.js:720 | 强制更新页触发下载 |
| `force-update:quit` | `ipcMain.on` | main.js:725 | 强制更新页退出安装 |

**无 `login`、`logout`、`auth-changed`、`session` 等通道。** `check-update-silent` 是渲染进程调用的"检查更新"指令，不是"登录成功"事件——主进程只知道有人让它查更新，不知道用户是否真的登录了。

### 1.2 preload.js 暴露的全部通道

preload.js:8-42 `contextBridge.exposeInMainWorld` 暴露给渲染进程的方法：
`notifyReady`、`checkUpdate`、`onUpdateStatus`、`checkUpdateSilent`、`enforceVersion`、`onProgress`、`onDownloaded`、`onUpdateError`、`triggerUpdate`、`quitAndInstall`

**无 `onLoginSuccess`、`onLogout`、`onAuthChange` 等。**

### 1.3 主进程不读取 localStorage

grep `executeJavaScript|webContents|localStorage|pms_token` 在 main.js 的结果（共 13 处）：
- `webContents.on('did-fail-load')`（main.js:502）— 仅监听到加载失败事件
- `webContents.on('render-process-gone')`（main.js:566）— 仅监听渲染进程死亡
- `webContents.setWindowOpenHandler`（main.js:579）— 导航防护
- `webContents.on('will-navigate')`（main.js:583）— 导航防护
- `webContents.send` 若干处 — 仅推送状态给渲染进程
- `webContents.reload()`（main.js:556）— 崩溃恢复重载页面
- `localStorage` 三处（main.js:89,93,519）— 都**只在注释里**说明 token 存那里，主进程**从未执行过** `executeJavaScript('localStorage.getItem(...)')`

主进程对登录态的了解仅限注释中的陈述（"token 在 localStorage 里，重启回来不用重新登录"），这是主进程开发者对系统设计的认知，不是主进程运行时的状态感知。

## 2. 崩溃自恢复与重启确实靠 localStorage 里的 pms_token 天然续命

证据链：

1. **崩溃恢复 comment**（main.js:519）：
   > 登录 token 存在 localStorage，重载后不用重新登录。

2. **`relaunchApp` 注释**（main.js:89,93）：
   > token 在 localStorage 里，重启回来不用重新登录。

3. **健康检查 comment**（desktop/lib/health.js:86）：
   > token 在 localStorage，重启不用重登。

4. **用户提示**（main.js:112,549）：
   - 画面卡死：`relaunchApp('画面无响应，正在自动重启', '重启后仍是登录状态，无需重新输入密码。')`
   - 反复卡死：`relaunchApp('程序反复无响应，正在自动重启', '重启后仍是登录状态，无需重新输入密码。')`

主进程的崩溃自恢复策略是 `reload()` → `relaunch()`（main.js:525-564），恢复后不需要走登录流程——因为前端的 axios 拦截器从 localStorage 读 token 自动带 Authorization 头（`frontend/src/api/index.ts:11`），`goLogin` 函数也读 `isLoggedIn()`（`api/index.ts:90-93`）。

## 3. 主进程确实无法感知登录态

- 主进程**不读** localStorage（见 1.3）
- 没有"登录成功"IPC 主进程侧处理函数（见 1.1）
- 没有"登出"IPC（见 1.1）
- `X-PMS-User` 统计头由**前端** axios 拦截器添加（`frontend/src/api/index.ts:24-27`），不是主进程注入的
- `check-update-silent` 虽有"登录触发"注释（main.js:629-630），但那是从渲染进程（LoginView）的视角写的——主进程收到的只是一个"检查更新"命令，30 分钟节流防频繁触发（main.js:636），跟登录状态无关

## 4. "注销时上报设备下线/清会话缓存需新增 IPC"——逻辑成立

因当前主进程没有登录/登出感知能力（见 1-3），要实现"注销时清理服务端会话/上报下线"，确实需要在 preload 新增一条 IPC（如 `pms-desktop:session-ended`），在渲染进程的登出逻辑（`frontend/src/api/index.ts:122-140` 的 401 处理 / `frontend/src/stores/auth.ts` 的 logout 函数）中调用，主进程收到后做上报/清理。

## 5. 证伪尝试（全不成立）

| 尝试的证伪路径 | 结论 |
|---------------|------|
| `check-update-silent` = login 事件？ | 否——它只管更新检查，与登录态无关；30 分钟节流更是证明它不是登录事件的时效性语义 |
| `enforce-version` = login 事件？ | 否——它在登录**前**触发，阻止登录而非感知登录 |
| 主进程通过 webContents 读 localStorage？ | 否——main.js 全文无 `executeJavaScript` 调用，只用 webContents 做事件监听和消息发送 |
| 主进程通过 `X-PMS-User` 头感知登录？ | 否——该头由前端 axios 拦截器添加（api/index.ts:24-27），主进程完全不知道它的存在 |
| preload 有 onLoginSuccess/onLogout 回调？ | 否——preload.js 暴露的 10 个方法无一与 auth 相关 |
| 403/401 错误触发主进程事件？ | 否——401 处理完全在前端（api/index.ts:122-140），主进程不介入 HTTP 请求 |

## 6. 相关文件

| 文件 | 关键行 |
|------|--------|
| desktop/main.js | 83,89,93,112,170,196,350,519,525-564,579,583,614,633,720,725 |
| desktop/preload.js | 13-42 |
| desktop/lib/health.js | 86 |
| frontend/src/api/index.ts | 11,24-27,90-93,122-140 |
| frontend/src/views/LoginView.vue | 52-58,78 |
