# 前端登录流程与鉴权链路（frontend-login-flow）

范围：`frontend/src`，只读前端，不涉及后端实现。

> 与 `03-frontend-login.md` 的关系：03 按文件拆解登录组件 / auth store / 拦截器 / 路由守卫的细节；本文聚焦**「输入密码 → 进主界面 → 请求鉴权 → 过期登出」的完整链路视角**，并把 03 未展开的 **VITE_API_BASE 构建分支、桌面统计头、WS token、登出闭环、H5 共享会话副作用** 串成一条线。两份互参。

## 1. 完整链路总览

```
LoginView 提交
  ├─ (桌面端) enforceVersionBeforeLogin → 旧版本被强制升级，登录前拦下        LoginView.vue:78
  └─ authApi.login(username, password)                                        LoginView.vue:79
       ├─ 浏览器外网 → 后端返回 gate_code（需验证码）→ gateApi.verify → finishLogin  LoginView.vue:114-120
       │   （闸门恰关闭 / 网络变内网 → 后端直接发 token，也走 finishLogin）
       └─ admin / 桌面端 / 内网 → 直接返回 token → finishLogin               LoginView.vue:79-86

finishLogin(resp)                                                             LoginView.vue:52-66
  ├─ 写 localStorage：pms_token = resp.access_token；pms_user = JSON(user)    :55-56
  ├─ 清 pms_menus（旧菜单缓存，强制下次重拉）                                :58
  ├─ (桌面端) checkUpdateSilent 静默查更新                                    :63
  └─ router.push('/overview')                                                 :65

MainLayout onMounted                                                          MainLayout.vue:125-145
  ├─ auth.fetchMe()（刷新用户信息；401 → 内部 logout）                       :126
  ├─ 强制改密弹窗（mustChangePassword）                                      :127-131
  ├─ auth.fetchMenus()（拉菜单 + can_view_detail，落 pms_menus）             :133
  ├─ refreshUnread() + 60s 轮询（消息角标）                                   :134-135
  └─ checkFeedbackReplies()（反馈回复弹窗）                                   :137

之后所有请求
  ├─ axios 请求拦截器：Authorization: Bearer <pms_token>                     api/index.ts:12-13
  ├─ (桌面端) X-PMS-Client / X-PMS-Device / X-PMS-User 统计头                api/index.ts:18-22
  └─ WS 连接：?token=<pms_token> query 参数                                  useRealtime.ts:36-48

鉴权失效（token 过期 / 被踢）
  ├─ axios 响应拦截器 401 → 清 pms_token/pms_user → goLogin() → 跳 #/login    api/index.ts:129-133, 107-119
  ├─ fetchMe 内 401 → logout()                                               auth.ts:89
  └─ H5 端 http 拦截器 401 → 清 pms_token/pms_user → hash 跳 #/login          h5/http.ts:27-31

登出
  └─ MainLayout.logout → auth.logout() → router.push('/login')               MainLayout.vue:78-81
```

## 2. 登录页两步闸门（LoginView.vue，366 行）

- **桌面端强制升级在登录前**：`enforceVersionBeforeLogin` 返回 truthy 就 return，不继续登录（`LoginView.vue:78`）。网络不通时放行不强制（后端 version.json 逻辑，见 desktop 卡）。
- **浏览器外网两步闸门**：`authApi.login` 若返回 `gate_code`，前端再调 `gateApi.verify(gate_code)` 换 token（`LoginView.vue:114-120`；`api/gate.ts` 只封了 `GET /login/gate-status` 与 `POST /login/verify-gate` 两个端点）。闸门状态变化（后端返回直接 token）时也走 `finishLogin`，不阻断。
- **登录成功动作**：写 `pms_token`/`pms_user`、清 `pms_menus` 强制重拉、记住用户名（REMEMBER_KEY）、跳 `/overview`（`LoginView.vue:52-66`）。
- **设备号**：桌面端读 `window.pmsDesktop.deviceId` 显示 + 一键复制（`LoginView.vue:25, 29-33, 214-216`），浏览器端为 `''` 整段跳过。

## 3. auth store（stores/auth.ts，117 行）

| store 字段 | 初始化来源 | localStorage key |
|---|---|---|
| `token` | `pms_token` | `pms_token`（:7） |
| `user` | `pms_user`（JSON，失败 null） | `pms_user`（:8-9） |
| `menus` | `pms_menus` | `pms_menus`（:12-13） |
| `canViewDetail` | `pms_can_view_detail !== '0'` | `pms_can_view_detail`（:15-16） |

- `isLoggedIn = !!token`（:19）——**只看 localStorage 有无 token，无本地过期时间戳**；过期靠后端 401 触发（见第 5 节）。
- `isAdmin = hasRole('admin','manager')`（:35）——「管理组」= admin **或** manager（与 AGENTS.md 一致）。
- `roleCodes()`/`hasRole()`：从 `user.role_codes` 取并集判定多角色（:22-33）。
- 动作：
  - `login`（:70-78）：调 authApi.login → 写 `pms_token`/`pms_user` → 清 `pms_menus` → `fetchMenus()`。
  - `fetchMe`（:81-91）：调 `GET /auth/me` 刷新 user；**401 时内部直接 logout()**——这是与 axios 拦截器并列的第二条 401 处理路径。
  - `fetchMenus`（:59-66）：拉 `GET /auth/menus`，写 `pms_menus` + `pms_can_view_detail`。
  - `logout`（:102-110）：`try { authApi.logout() } catch { /* ignore */ }` 然后清 4 个 key。**登出请求失败被吞，本地登出不被阻塞**；但若后端 logout 是服务端失效 token 的设计，失败时服务端 token 仍有效（危害有限：token 只存本机 localStorage，已清）。
  - `changePassword`（:94-99）：改密成功后回写 `pms_user`。

## 4. axios 拦截器（api/index.ts，140 行）

- **baseURL**：`(import.meta.env.VITE_API_BASE ?? '') + '/api'`（:7）；timeout 30000（:8），blob 下载置 0（:28）。
- **请求头**（:11-22）：
  - `Authorization: Bearer ${pms_token}`（:12-13），无 token 就不带（此时未登录请求，如登录本身）。
  - 桌面端统计头：`X-PMS-Client: desktop/<version>`、`X-PMS-Device: <deviceId>`、`X-PMS-User: <username>`（:16-22，从 `window.pmsDesktop` 读，浏览器端 `undefined` 自动跳过）。这三个头是后端免外网验证码闸 + 在线统计的依据（`X-PMS-Client` 存在即可被 curl 伪造，真防线在 device_gate，见 backend 卡）。
- **401 处理**（:122-133）：非登录请求收到 401 → 清 `pms_token`/`pms_user` → `goLogin()`。注意**只清这两个 key**，`pms_menus`/`pms_can_view_detail` 留着（重新登录时 `login()` 会清 `pms_menus` 重拉，:77）。
- **goLogin() 防风暴**（:107-119）：`HASH_ROUTER = !!VITE_API_BASE`（:91，桌面端 hash / 浏览器 history）；`redirecting` 标志保证一次 401 风暴只跳一次；`ElMessage.warning('登录已过期，请重新登录')`（:112）。

## 5. 路由守卫与路由模式（router/index.ts，216 行）

- **路由模式随构建分叉**（:8）：`import.meta.env.VITE_API_BASE ? createWebHashHistory() : createWebHistory()`——桌面 file:// 下 hash 路由，浏览器 history 路由。
- `login` 路由 `meta: { public: true }`（:12）。
- **beforeEach**（:185-208）顺序：
  1. `meta.public` 直接放行（:187）；
  2. 未登录 → `{ name: 'login' }`（:188）；
  3. `meta.requireAdmin && !auth.isAdmin` → 跳 overview（:189）；
  4. `meta.menuKey` 不在 `auth.hasMenu()` → `fallbackRoute`（:191-193）；
  5. `meta.canViewDetail` 且菜单已加载但 `!auth.canViewDetail` → `fallbackRoute`（:197-198）。
- `fallbackRoute`（:208）：菜单里第一个可访问路由兜底。
- **菜单可见性唯一前端权威** = `auth.hasMenu(key)`（读 `pms_menus`，后端 `/api/auth/menus` 下发），路由的 `meta.menuKey` 只是把每个模块路由绑到对应菜单 key。

## 6. token 存储与过期

- 存储：仅 localStorage，key `pms_token`（JWT）/ `pms_user`（用户对象）/ `pms_menus`（菜单缓存）/ `pms_can_view_detail`（详单可点性）。
- **无前端过期时间戳**：`isLoggedIn = !!token`，不做本地到期判断。过期/失效全部依赖后端 401，再由三处之一清态：
  1. axios 响应拦截器（api/index.ts:129-133）；
  2. `fetchMe` 内部（auth.ts:89）——App.vue onMounted 与 MainLayout onMounted 各触发一次 `fetchMe`（App.vue:6-8；MainLayout.vue:126）；
  3. H5 的独立 http 拦截器（h5/http.ts:27-31）。
- token 实际有效期由后端 JWT 签发（前端注释称 8 小时，见 api/index.ts 相关注释），不在 frontend/src 范围内核实。

## 7. VITE_API_BASE：一处开关，四处落点

| 落点 | 浏览器 / docker 构建（不设） | 桌面打包（设 `http://8.141.123.141`） |
|---|---|---|
| axios baseURL（api/index.ts:7） | `/api`（走 Vite 代理 / nginx） | `http://8.141.123.141/api`（直连服务器） |
| 路由模式（router/index.ts:8） | `createWebHistory()` | `createWebHashHistory()` |
| 401 跳登录（api/index.ts:91,107-119） | `pathname === '/login'` | `location.hash.startsWith('#/login')` |
| Vite `base`（vite.config.ts:10） | `/` | `'./'`（file:// 下资源相对路径，防白屏） |

- dev server 代理：`/api` → `VITE_DEV_API || http://backend:8000`（vite.config.ts:20-32）、`/ws` → 同源 ws 代理（:33-42）。
- 三者（baseURL / history / base）由同一个 `VITE_API_BASE` 开关联动，设计上保证桌面 file:// 与浏览器两种运行时各自正确，见各文件头注释（api/index.ts:5-6、vite.config.ts:7-9）。

## 8. WS 实时连接 token（useRealtime.ts，99 行）

- 从 `localStorage.getItem('pms_token')` 取 token（:36），无 token 直接不连（:37）。
- 地址：桌面端从 `VITE_API_BASE` 推导（http→ws、https→wss）并拼 `?token=`（:41-44）；浏览器端按 `location` 拼（:45-48）。
- **401 不主动断 ws**：无 token 失效监听，靠服务端踢（onclose）后 `connect()` 重读 token——token 已清则 `connect` 里 `if (!token) return` 自然停止重连（:37,69-75）。属被动收敛，不是主动登出联动。

## 9. 登出链路

- `MainLayout.logout`（MainLayout.vue:78-81）：`await auth.logout()` → `router.push('/login')`。
- `auth.logout`（auth.ts:102-110）：`authApi.logout()`（`try/catch ignore`，后端行为超出本文件范围）→ 清 `pms_token`/`pms_user`/`pms_menus`/`pms_can_view_detail` 四 key。
- 清完即回登录页，无二次确认（按钮直接执行）。

## 10. H5 独立登录体系（旁注，与主应用共享会话）

- 刻意不复用 `@/api/index.ts`：那文件 import 了 element-plus（连带 vxe-table），H5 只两页不该背体积（h5/http.ts:1-9 注释）。独立 `http` 实例（h5/http.ts:15），401 清 `pms_token`/`pms_user` + hash 跳 `#/login`（:27-31）。
- **token key 与网页版/桌面端共用**（h5/session.ts:6-7 注释明示）：`setSession`/`clearSession` 直接读写 `pms_token`/`pms_user`（h5/session.ts:23-35）。
- `API_BASE = VITE_API_BASE || '/api'`（h5/apiBase.ts:14），Capacitor APP 需构建时注入绝对地址（:4-13）。

## 11. MainLayout 菜单权限与 isAdmin

- **菜单全部由后端下发**：`auth.fetchMenus()`（MainLayout.vue:133）拉 `GET /auth/menus`，前端所有菜单项按 `auth.hasMenu(key)` 渲染，无硬编码项（除管理组三个传统项 admin-users/admin-perms/admin-audit 也走 hasMenu，MainLayout.vue:210-220）。
- 分组（MainLayout.vue:37-40）：`bizMenus = deptMenus 排除 messages + ADMIN_EXTRA`；`adminExtraMenus = deptMenus ∩ ADMIN_EXTRA`（导出审批/企微绑定等）。
- **管理区显示条件**：`auth.isAdmin || adminExtraMenus.length`（:206）——管理层全量显示管理组，非管理层被按账号授予管理组菜单（如 hr 的企微绑定）时仅显示已授权项。
- isAdmin 定义：`hasRole('admin', 'manager')`（auth.ts:35）——与 AGENTS.md「auth store 的 isAdmin = admin 或 manager」一致。
- onMounted 初始化序列（MainLayout.vue:125-145）：fetchMe → 强制改密弹窗 → fetchMenus → 未读角标 60s 轮询 → 反馈回复提醒 → 桌面更新回推订阅。

## 12. 反例 / 排除过的路

- **前端没有 token 过期时间戳**：搜遍 store/拦截器无 `expire`/`exp` 相关逻辑，「本地到期主动登出」这条路不存在，全部交给后端 401。想本地提前登出需要新加逻辑。
- **H5 不走主应用的拦截器**：曾被考虑复用 `@/api/index.ts`，因 element-plus 体积被否（h5/http.ts:1-9 注释），所以 401 清理逻辑是 H5 单独一套、只清 2 个 key 且不清 `pms_menus`——H5 根本不用菜单。
- **`X-PMS-Client` 头不是安全闸**：它只用于统计 + 免验证码，可被 curl 伪造（api/index.ts:18 只是无脑带头）；真正的设备防线在服务端 device_gate，超出前端范围。

## 13. 发现（待复核）

1. **H5 与网页版共享 token key 导致双向登出耦合**：两者都读写同一对 localStorage key（`pms_token`/`pms_user`）。同一浏览器先登网页版再开 H5 能复用登录态（注释是故意的），但任一方 `logout`/401 都会 `removeItem` 清掉 key（auth.ts:107-110、h5/http.ts:28-29、h5/session.ts:30-35）；另一方的内存 `ref` 仍持旧 token、`isLoggedIn` 仍 true，直到下一次请求因 localStorage 无 token 不带 Authorization 而 401 掉线。多 tab / 多端共享场景的隐性耦合。
2. **登出请求失败被静默吞掉**：`auth.logout` 里 `try { authApi.logout() } catch {}`（auth.ts:102-103），若后端 logout 是服务端失效设计，网络失败时服务端 token 仍有效。危害有限（token 不落磁盘、已本地清除），但若后端审计依赖 logout 记录，会丢日志。
