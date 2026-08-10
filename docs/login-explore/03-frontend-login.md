# 前端登录接入分析

范围：`frontend/src` 登录页面组件、auth store、路由守卫、axios 拦截器、登录成功跳转、顶部状态下拉默认值。只查前端行为，不涉及后端实现。

## 1. 登录入口与页面组件

| 入口 | 路由 | 组件文件 | 行数 |
|---|---|---|---|
| 网页端 / 桌面客户端 | `/login` | `frontend/src/views/LoginView.vue` | 366 |
| H5（手机） | `#/login`（hash） | `frontend/src/h5/H5LoginView.vue` | ~180 |

- 网页端路由注册：`router/index.ts:11-14`（`path:'/login'`，`meta:{public:true}`）。
- H5 独立入口 `frontend/src/h5/main.ts`，**不引 element-plus / vxe-table / pinia**，只用 vue + vue-router，构建产物走 `dist-h5/`（`main.ts:1-13` 注释）。原因：H5 是手机 4G 场景，只为登录 + AI 助手两页，不想背 element-plus 体积（`h5/http.ts:1-9` 注释）。
- H5 也用 hash 路由：H5 由 nginx 以 `/h5/` 前缀托管，hash 模式无需再配 try_files 深链接规则（`h5/main.ts:10-12`）。

## 2. auth store（`frontend/src/stores/auth.ts`，117 行）

Pinia store，名字 `'auth'`。所有持久化都走 localStorage，key 与 H5 端共用（`h5/session.ts:6-7` 注释明确说明「token 的 key 与桌面端保持一致」）。

### 2.1 状态与持久化 key（`auth.ts:7-17`）

| store 字段 | 初始化来源 | localStorage key |
|---|---|---|
| `token` | `pms_token` | `pms_token` |
| `user` | `pms_user`（JSON 解析，失败得 `null`） | `pms_user` |
| `menus`（可见菜单，`null`=未加载） | `pms_menus` | `pms_menus` |
| `canViewDetail`（详单可点性） | `pms_can_view_detail` !== `'0'` | `pms_can_view_detail` |

`isLoggedIn = computed(() => !!token.value)`（`auth.ts:19`）——**只要 localStorage 里有 token 就算已登录**，无过期判断，过期由接口 401 触发拦截器登出（见第 4 节）。

### 2.2 角色判断（`auth.ts:22-44`）

- `roleCodes()`：并集 `user.role_codes` 数组与 `user.role_code` 单值，去重（`auth.ts:22-28`）。
- `hasRole(...codes)`：并集判断，任一命中即 true（`auth.ts:30-33`）。前端按钮显隐统一走它。
- `isAdmin = hasRole('admin', 'manager')`（`auth.ts:35`）——**admin 或 manager 都算管理员**（AGENTS.md 也强调 auth store 的 isAdmin 是二选一）。
- `tabHidden(menuKey, tabName)` / `tabVisible`：二级菜单 tab 按账号授权，key 形如 `"finance:pay_payment"`；管理层（admin/manager）不受限（`auth.ts:39-43`）。数据来自 `user.hidden_tabs`。

### 2.3 菜单（`auth.ts:47-68`）

- `hasMenu(key)`：`menus === null` 时只放行老默认 `catalog`/`list`（避免闪烁），否则查 `menus` 数组（`auth.ts:47-50`）。
- `deptMenus`：过滤掉 `catalog`/`list`/`admin-users`/`admin-perms`/`admin-audit`/`dict-admin` 后的业务菜单（`auth.ts:53-57`）。
- `fetchMenus()`：调 `GET /auth/menus`（`authApi.menus()`，见 `api/auth.ts:26`），写入 `menus` + `canViewDetail`，并同步 localStorage（`auth.ts:59-68`）。接口失败保持现状、不阻塞页面。

### 2.4 动作（`auth.ts:70-116`）

- `login()`：调 `POST /auth/login` → 写 token/user + localStorage → `menus = null` 清缓存 → `fetchMenus()`（`auth.ts:70-79`）。**LoginView 走闸门流程时不用这个方法**（`LoginView.vue:51` 注释：闸门需先按响应分支，故走自定义 `finishLogin`，两套持久化逻辑一致）。
- `fetchMe()`：`GET /auth/me`，失败调 `logout()`（`auth.ts:81-92`）。
- `changePassword()`：改密后本地把 `password_must_change` 置 false（`auth.ts:94-100`）。
- `logout()`：`POST /auth/logout`（失败静默）→ 清空 token/user/menus + 4 个 localStorage key（`auth.ts:102-111`）。

## 3. 路由守卫（`frontend/src/router/index.ts`，216 行）

### 3.1 路由模式（`router/index.ts:4-9`）

```ts
history: import.meta.env.VITE_API_BASE ? createWebHashHistory() : createWebHistory()
```

**桌面客户端（file:// 加载）必须用 hash 模式**，浏览器构建不设 `VITE_API_BASE` 用 history。判据在拦截器的 `HASH_ROUTER` 处保持一致（`api/index.ts:91`）。

### 3.2 全局前置守卫 `beforeEach`（`router/index.ts:185-205`）

判定顺序：

1. `to.meta.public` → 放行（`/login` 是 public）。
2. `!auth.isLoggedIn` → `{ name: 'login' }`（未登录跳登录）。
3. `to.meta.requireAdmin && !auth.isAdmin` → `{ name: 'overview' }`。
4. `to.meta.menuKey` 存在且 `menus` 已加载且 `!hasMenu(menuKey)` → `fallbackRoute(auth)`。
5. 目标是 `project-detail`/`projects` 且无 `canViewDetail` → `fallbackRoute(auth)`。
6. 目标是 `overview` 且无 `catalog` 菜单（如纯人事/售后）→ `fallbackRoute(auth)`。

`fallbackRoute`（`router/index.ts:208-214`）：跳第一个可见菜单；`catalog`→overview、`list`→projects、其它→对应 name。

### 3.3 H5 守卫（`h5/main.ts:20-23`）

```ts
router.beforeEach((to) => to.meta.public ? true : (isLoggedIn.value ? true : { name: 'login' }))
```

只判断「有没有 token」，没有菜单/权限那套。

## 4. axios 拦截器（`frontend/src/api/index.ts`，140 行）

### 4.1 baseURL 与超时（`api/index.ts:4-9`）

```ts
baseURL: (import.meta.env.VITE_API_BASE ?? '') + '/api',
timeout: 30000,
```

- 桌面客户端打包时以 `VITE_API_BASE`（如 `http://8.141.123.141`）构建 → 直连后端；
- 浏览器构建不设 → baseURL 为 `/api`（走 Vite 代理 / nginx）。
- **注意 webSocket 也一样**（AGENTS.md：桌面打包 `VITE_API_BASE` 使 ws 直连服务器）。

### 4.2 请求拦截器（`api/index.ts:11-30`）

1. 从 `localStorage.getItem('pms_token')` 读 token，有则加 `Authorization: Bearer <token>`。
2. 桌面客户端（`window.pmsDesktop?.isDesktop`）加三个统计头：
   - `X-PMS-Client: desktop/<version>`（`api/index.ts:18`）
   - `X-PMS-Device: <deviceId>`（`api/index.ts:19`）
   - `X-PMS-User: <pms_user.username>`（`api/index.ts:20-23`，解析失败不带该头）
   - 后端中间件按 device_id 60s 节流 upsert `desktop_clients` 表。
3. `responseType === 'blob'` 时 `timeout = 0`（#188 文件下载不被 30s 掐断；服务端 nginx proxy_read_timeout=300s 兜底，`api/index.ts:25-28`）。

### 4.3 响应拦截器与 401 处理（`api/index.ts:122-140`）

- 401 且非 `/auth/login` → 清 `pms_token`/`pms_user` → `goLogin()`。
- 其它错误 → `extractErrorMessage(err)` 弹 `ElMessage.error`，再 `Promise.reject`。
- **登录接口本身 401 不跳页**（`isLoginRequest` 判断 `api/index.ts:127`），让 LoginView 自己展示错误。

### 4.4 `goLogin()` —— 黑屏教训（`api/index.ts:90-120`）

```ts
const HASH_ROUTER = !!import.meta.env.VITE_API_BASE
```

- **绝不能写 `location.href = '/login'`**：桌面端页面是 file:// 加载，绝对路径会解析成 `file:///login` 不存在的文件 → Chromium 换成错误空页 → 窗口整片深蓝无响应。这是反馈 #343「到 4 点多就黑屏」的真身：token 8 小时到期，早上 8:15 登录的人下午 16:15 踩中，近 14 天 8 人踩 23 次（`api/index.ts:94-105` 注释）。
- 正确做法（`api/index.ts:107-120`）：`redirecting` 防一次 401 风暴重复跳；hash 模式 `location.hash = '#/login'` + `location.reload()`（把残留 store 状态清干净）；history 模式 `location.href = '/login'`。

### 4.5 H5 独立 http 封装（`h5/http.ts`，42 行）

- 刻意不复用 `@/api/index.ts`：那个文件 import 了 element-plus 的 `ElMessage`，会把 element-plus（及 vxe-table）拖进 H5 包（`http.ts:1-9` 注释）。
- baseURL = `API_BASE` = `(VITE_API_BASE as string) || '/api'`（`apiBase.ts:14`）；timeout 60000。
- 请求头同样 `Authorization: Bearer pms_token`（`http.ts:17-21`）。
- 401 → 清 `pms_token`/`pms_user`，若非已在 login 页则 `location.hash = '#/login'`（`http.ts:26-32`，hash 路由下用 hash 跳转）。
- 错误文案从 `response.data.detail` 取（字符串或数组第一项 msg），兜底不显示 `[object Object]`（`http.ts:36-42`）。

## 5. 登录成功跳转（`frontend/src/views/LoginView.vue`）

### 5.1 两步闸门流程（`LoginView.vue:16-20, 68-108`）

- `step: 'pwd' | 'gate'` 两个阶段。
- `onSubmit`：桌面端先 `enforceVersion()` 强制版本检查（版本低于要求直接不登录，浏览器端 `window.pmsDesktop` 为 undefined 整个跳过；网络不通时主进程一律放行，宁可漏拦不锁人，`LoginView.vue:75-78`）→ `POST /auth/login`。
  - 响应带 `gate_required && pre_token` → 存 preToken，切到 `step='gate'`（第二步输 6 位码）。
  - 否则直接 `finishLogin(resp)`。
- `onVerify`：校验 6 位数字 → `POST /auth/login/verify-gate` → `finishLogin`。
- `onResend`：用当前账号密码重调 login 发码（后端限频 1 条/分）；若响应无 gate 字段（闸门恰被关闭/变内网）则直接 `finishLogin`（`LoginView.vue:110-127`）。
- 设备 ID 复制按钮：桌面端被设备闸拦下时展示 `window.pmsDesktop.deviceId`，用户可复制发给管理层录入名单（`LoginView.vue:22-43`）。

### 5.2 `finishLogin`（`LoginView.vue:51-66`）

```ts
auth.token = resp.access_token
auth.user = resp.user
localStorage.setItem('pms_token', resp.access_token)
localStorage.setItem('pms_user', JSON.stringify(resp.user))
auth.menus = null            // 切换账号清菜单缓存，登录后重新拉取
localStorage.removeItem('pms_menus')
await auth.fetchMenus()
if (remember) localStorage.setItem('pms_remember_name', username)
window.pmsDesktop?.checkUpdateSilent?.()   // 桌面端静默检查更新（30 分钟节流）
ElMessage.success('登录成功')
router.push('/overview')
```

- 记住用户名：key `pms_remember_name`，勾选存/取消删（`LoginView.vue:46-49, 60-61`）。
- **跳转目标固定 `/overview`**（概览页），不管用户原本想进哪个页——`/login` 是 public 页，守卫不拦截，没有「登录后回原页」的 redirect 参数逻辑。

### 5.3 H5 登录收尾（`H5LoginView.vue:35-39`）

```ts
setSession(resp.access_token, resp.user)   // h5/session.ts 的 token/user + localStorage
await router.replace('/')
```

- H5 同样支持两步闸门（`H5LoginView.vue:48-53`），走 `/auth/login` + `/auth/login/verify-gate`，后端一行没改（`H5LoginView.vue:4` 注释）。
- 登录后跳 `'/'`（H5Home），非 `/overview`。
- H5 登出在 `H5HomeView.vue:128`：`clearSession()` + `router.replace('/login')`。

## 6. 登录后的初始化（`frontend/src/layouts/MainLayout.vue`）

`onMounted`（`MainLayout.vue:125-137`）：
1. `if (auth.isLoggedIn && !auth.user) await auth.fetchMe()`——刷新 user。
2. `await auth.fetchMenus()`——拉菜单（侧边栏渲染权威）。
3. `refreshUnread()` + `setInterval(refreshUnread, 60_000)`——消息角标轮询。
4. `checkFeedbackReplies()`——登录后提醒未读反馈回复（只在 onMounted 调一次，无轮询，长期在线不刷新不弹，见 AGENTS.md 已知结论）。

退出登录：`MainLayout.vue:78-79` 调 `auth.logout()`。

## 7. 顶部状态下拉默认值

「顶部状态下拉」在各业务页面内部，登录后落在 overview 时即见：

| 页面 | 位置 | 默认值 | 说明 |
|---|---|---|---|
| 概览一览 | `OverviewView.vue:414` | `statusFilter = ref('进行中')` | 每次进入都默认「进行中」，不记忆上次选择；本次会话内可自由切换（`:412-414` 注释）。 |
| 设计部工作台 | `DeptWorkbenchView.vue:119` | `projStatusFilter = ref('进行中')` | 同默认「进行中」。 |

**历史教训（#334/#335，AGENTS.md 铁律）**：工作台一次拉 `orders` 供多个 tab 用，当初把顶部状态下拉默认「进行中」传成服务端 `proj_status` 参数，后端按 `status != done` 把已完成订单在服务端删光，客户端「已完成」tab 恒空。修复后**取数不带状态**，客户端各 tab 自己按 status 分流（`DeptWorkbenchView.vue:571-575` 注释）。

## 8. 关键契约汇总

| 契约 | 定义位置 | 内容 |
|---|---|---|
| `LoginResp` | `types/index.ts:22` | `{ access_token, token_type, user }` |
| `LoginResult` | `api/auth.ts:10-11` | `LoginResp & { gate_required?, pre_token?, message? }`（闸门字段可选，兼容旧响应） |
| `User` | `types/index.ts:5-14` | 含 `role_codes?`、`role_code?`、`is_active`、`password_must_change`、`hidden_tabs?` |
| `MenuItem` | `api/auth.ts:4` | `{ key, label }` |
| `pmsDesktop` 全局 | `vite-env.d.ts:12-25` | `isDesktop/version/deviceId/checkUpdateSilent/enforceVersion` |
| token 时长 | — | 8 小时过期（见 `api/index.ts:103-105` 注释的 #343 根因） |

## 9. 反例 / 排除过的路

- **不用 `location.href='/login'` 跳登录**：桌面端 file:// 下会解析成 `file:///login` 空错误页、整窗黑屏（#343）。已排除，改用 hash + reload（`api/index.ts:97-119`）。
- **H5 不复用 `@/api/index.ts`**：会拖入 element-plus/vxe-table 体积（`h5/http.ts:1-9`）。已排除，独立 `h5/http.ts`。
- **H5 不引 pinia auth store**：它带菜单/权限矩阵/详单闸门整套，H5 只有登录和助手两页用不上（`h5/session.ts:2-8` 注释）。已排除，独立 `h5/session.ts`。
