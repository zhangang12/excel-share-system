# mobile/ 登录勘察报告

> 结论：**mobile/ 含完整的账号登录逻辑，但全部在 H5 前端包里（www/，Vite 构建产物），原生 Android 壳零登录代码。**

---

## 1. 壳类型判定：Capacitor 壳（前端打包进 APK + OTA 热更新）

mobile/ 是一个 Capacitor 应用，不是纯静态页面、也不是原生实现登录：

| 证据文件 | 内容 | 判定 |
|---|---|---|
| `mobile/capacitor.config.ts:19-32` | `appId: 'com.tonghui.pms'`、`webDir: 'www'`、`server.androidScheme: 'http'` | Capacitor 配置，前端从 `www/` 本地加载 |
| `mobile/android/app/src/main/java/com/tonghui/pms/MainActivity.java:27` | `extends BridgeActivity`，注释明确「前端资源**打在包里、从本地加载**（Capacitor）」 | 原生壳仅 WebView 容器 |
| `mobile/android/.../PmsUpdater.java` + `PmsUpdaterPlugin.java` | OTA 热更新插件（换前端包=换目录，不用重发 APK） | 登录与更新无关 |
| `mobile/android/.../PmsSpeechPlugin.java` | 语音识别插件 | 与登录无关 |

MainActivity.java 只做 4 件事（注释原文）：①启动前选这次加载哪个前端包 ②试用超时没人报平安→当场回滚 ③返回键页面内后退 ④附件下载交给系统下载器。**没有任何用户名/密码/token 相关代码。**

`capacitor.config.ts:13-17` 有一条关键设计：`androidScheme` 必须是 `http`（默认 https 会因页面是安全上下文而发明文 `http://8.141.123.141` 请求被**混合内容拦截**，每个接口全失败）。

## 2. 登录逻辑全链路（三层，都在 www/ 构建产物里）

www 是 Vue3 + Vite 打包结果，登录链路分布在 3 个压缩 JS 文件（单行文件，无法给行号，给「文件 + 字符偏移」）：

### 2.1 axios 层 —— `www/assets/_plugin-vue_export-helper-CWL1yK8K.js`

这是登录页 import 的 `h`（在 H5LoginView 里别名 `C`），偏移 @42489：

```js
_t = "http://8.141.123.141/api"                        // baseURL 硬编码生产服务器，非环境变量
Tt = A.create({baseURL:_t, timeout:6e4})               // axios 实例，60s 超时
// 请求拦截器：读 localStorage pms_token 注入 Authorization
Tt.interceptors.request.use(e=>{const t=localStorage.getItem("pms_token");return t&&(e.headers.Authorization=`Bearer ${t}`),e})
// 响应拦截器：401 → 清 pms_token/pms_user → 跳 #/login
Tt.interceptors.response.use(e=>e,e=>{... status===401 && (removeItem pms_token, removeItem pms_user, location.hash="#/login")})
```

导出：`Tt as h`（axios 实例）、`Hr as e`（错误消息提取，登录页 catch 用它显示「登录失败」/「验证码不正确」）、`qr = e => `${_t}${e}``。

注意：**mobile H5 不注入 X-PMS-* 头**（桌面客户端的 X-PMS-Client/X-PMS-Device/X-PMS-User 在此不存在，grep `X-PMS`/`deviceId` 零命中），所以手机端登录**不走桌面客户端的免闸**，走后端 `gate.py` 的浏览器外网闸门路径。

### 2.2 应用壳层 —— `www/assets/h5-DFWXYR0d.js`（token store + 路由守卫）

偏移 @94012-94455（token 状态与存储）：

```js
lf = () => JSON.parse(localStorage.getItem("pms_user")||"null")   // 读用户对象
Gs = ref(localStorage.getItem("pms_token")||"")                   // 初始 token
cf = computed(()=>!!Gs.value)                                     // 已登录标志
bf(e,t){ Gs.value=e; On.value=t; setItem("pms_token",e); setItem("pms_user",JSON.stringify(t)) }  // 写 token+user
Ef(){ Gs.value=""; On.value=null; removeItem("pms_token"); removeItem("pms_user") }                // 清 token+user
```

导出映射：`bf as t`（= 登录页的 `P`）、`Ef as c`（= 退出登录的 `fe`）、`cf`（登录态）、`Ms as q`（ref）。

路由（偏移 @95519）与守卫（偏移 @95942）：

```js
routes: [{path:"/login",name:"login",component:H5LoginView,meta:{public:!0}},
         {path:"/",name:"home",...}, {path:"/chat",name:"chat",...}, {path:"/:rest(.*)",redirect:"/"}]
Ws.beforeEach(e=>e.meta.public||cf.value?!0:{name:"login"})   // 非 public 路由且未登录 → 强制 /login
```

### 2.3 登录界面 —— `www/assets/H5LoginView-C8SpngEU.js`（Vue 组件，全文 1 行）

**表单字段**：`username`（初始化读 `localStorage.getItem("pms_h5_remember")`，记住账号）、`password`、`remember`（勾选态初始化 `!!localStorage.getItem("pms_h5_remember")`）。常量 `w="pms_h5_remember"`（偏移 @1797 附近）。

**登录流程 `g()`**（首行 `C.post`）：

```js
await C.post("/auth/login", {username, password, remember:d.value})
if (e.gate_required && e.pre_token) { 切换验证码界面; k.value=e.pre_token }
else await B(e)                        // 直接成功
```

**外网登录闸门验证码**：响应带 `gate_required && pre_token` → 进入 6 位 OTP 界面（`r=["","","","","",""]` 六格），输入后 `C.post("/auth/login/verify-gate", {username, pre_token:k.value, code, remember})`，满 6 位自动提交；失败清空六格重来，错误显示「验证码不正确」。

**成功落地 `B(a)`**：

```js
P(a.access_token, a.user)              // bf → localStorage 写 pms_token + pms_user
d.value ? setItem(w, o.username) : removeItem(w)   // 记住账号
await T.replace("/")                   // 路由进首页
```

**退出登录**在 `H5ChatView-B-ZkROkM.js` 偏移 @103464（聊天页顶栏「···」按钮，aria-label 退出）：`W(){ fe(); e.replace("/login") }` —— `fe` 即 `Ef`，清 token/user 后跳登录页。

## 3. 数据流与字段对照表

| 事项 | 值 | 位置 |
|---|---|---|
| API 根地址 | `http://8.141.123.141/api`（硬编码） | helper js @42489 |
| 登录端点 | `POST /api/auth/login` `{username,password,remember}` | H5LoginView.js |
| 闸门验证端点 | `POST /api/auth/login/verify-gate` `{username,pre_token,code,remember}` | H5LoginView.js |
| token 存储 key | `localStorage.pms_token` | helper js 拦截器 / h5-DFWXYR0d.js |
| 用户对象 key | `localStorage.pms_user`（JSON） | h5-DFWXYR0d.js @94012 |
| 记住账号 key | `localStorage.pms_h5_remember` | H5LoginView.js 常量 `w` |
| 请求鉴权头 | `Authorization: Bearer <pms_token>` | helper js 请求拦截器 |
| 401 处理 | 清 token+user → 跳 `#/login` | helper js 响应拦截器 |
| 路由守卫 | 非 public 路由无 token → `/login` | h5-DFWXYR0d.js @95942 |
| 登录态判定 | `computed(()=>!!pms_token)` | h5-DFWXYR0d.js @94082 |

## 4. 与桌面客户端 / 浏览器登录的差异

- **无 X-PMS-Client 头** → 不享受 `gate.py` 里桌面客户端的免闸（见 AGENTS.md 外网登录闸门），手机 H5 与浏览器同属「外网需验证码」一档（内网 IP 仍免）。
- 桌面客户端由 preload 注入 `window.pmsDesktop`，mobile 无此机制。
- baseURL：桌面客户端走 Vite 的 `VITE_API_BASE`，mobile H5 **写死** `http://8.141.123.141/api`（与浏览器构建同走 `/api` 的相对路径不同，mobile 是绝对地址）。

## 5. 被排除的猜想（下一棒不用再试）

- **假设原生层有独立登录** → 排除：`grep` MainActivity/PmsUpdater/PmsSpeech 无 username/password/token，BridgeActivity 只承载 WebView。
- **假设 token 存 Capacitor 原生存储（Preferences/Cookie）** → 排除：纯 `localStorage`，三处读写（拦截器读、bf 写、Ef 清）均走 WebView 的 localStorage。
- **假设有 X-PMS 设备识别** → 排除：全仓 grep `X-PMS`/`deviceId` 在 www 下零命中（那是桌面客户端的契约）。
- **假设 mobile 有自己的登录服务端** → 排除：登录端点与主仓后端同域（`/api/auth/*`），无独立鉴权服务。

## 6. 待核实

- 无。以上均直接读 `mobile/www/assets/*.js` 与 `mobile/android/.../*.java` 原文确认。
