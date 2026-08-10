# 登录逻辑分析（n9 汇总蒸馏）

> 基于 n3-n8 六片上游产出交叉印证、去重、合成。每条结论标注证据来源（文档:行号 或 代码文件:行号）。本文件是登录分析的总索引，细节各归各片。

---

## 1. 登录整体流程图（文字版）

### 1.1 单协议、单端点：全部入口汇聚到 `POST /api/auth/login`

```
┌──────────────────────────────────────────────────────────────────┐
│ 五端入口（前端代码各不同，但调的是同一接口）                       │
│                                                                  │
│  ├─ 浏览器网页版:  LoginView.vue, /login 路由                     │
│  ├─ H5/手机浏览器:  H5LoginView.vue, h5.html                      │
│  ├─ 手机APP:        同一套 H5 代码（Capacitor 壳）                 │
│  ├─ 桌面客户端:     内置 LoginView.vue（file:// 加载）             │
│  └─ demo:           127.0.0.1:8000, 同浏览器                      │
│                                                                  │
│  登录协议唯一: POST /api/auth/login + POST /api/auth/login/verify-gate │
│  没有 /api/auth/desktop-login 之类独立端点。                       │
│  桌面/H5/APP 都不例外——H5LoginView.vue:3-4 注释明说:              │
│    「后端一行没改，走的还是 /auth/login + /auth/login/verify-gate」 │
└────────────────────────────────┬─────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│ POST /api/auth/login  (backend/app/routers/auth_router.py:73-109)│
│                                                                  │
│ 入参: LoginIn{username, password, remember:bool=False}           │
│       (schemas.py:700-705, 无最小长度/格式校验)                   │
│                                                                  │
│ 1. 查用户 → `select(User).where(username==data.username)`        │
│    `verify_password` 失败或用户不存在 → 统一 401 "用户名或密码错误"│
│    (auth_router.py:77-78, 不泄露账号存在性)                       │
│                                                                  │
│ 2. is_active == False → 403 "账号已停用"  (auth_router.py:79-80)  │
│                                                                  │
│ 3. 更新 last_login = datetime.now(timezone.utc)  —— 存 UTC,       │
│    非业务时区 UTC+8  (auth_router.py:82-84)                       │
│                                                                  │
│ 4. IP 取址: _client_ip (auth_router.py:48-56)                    │
│    ① X-Real-IP 头优先 (nginx $remote_addr 覆写, 外部不可伪造)     │
│    ② X-Forwarded-For 末段 (首段可被客户端伪造, 取末段才准)       │
│    ③ 兜底 request.client.host (直连)                             │
│                                                                  │
│ 5. 桌面判定: is_desktop = x-pms-client 头 startswith("desktop/") │
│    (auth_router.py:90)                                           │
│                                                                  │
│ 6. 闸门判定链 (见 §3): 仅非 admin 进入                          │
│    ┌─ admin 角色 (has_role("admin")) → 跳闸门, 直接发 token      │
│    │  (auth_router.py:92)                                        │
│    ├─ get_gate_config(db) 实时读库 (auth_router.py:93)            │
│    ├─ exempt = is_intranet(ip, cidrs) 或 desktop_exempt(...)     │
│    │  (auth_router.py:94-97)                                     │
│    └─ enabled and not exempt → issue_code 发码,                  │
│       返回 GateRequiredOut{gate_required, pre_token}              │
│       (auth_router.py:98-108)                                    │
│                                                                  │
│ 7. 未命中闸门 → _issue_token 直接发 JWT (auth_router.py:109)     │
│                                                                  │
│ _issue_token(db, u, ip, remember) (auth_router.py:65-70):       │
│   └─ create_access_token + write_audit(action="login")            │
│       remember=True → minutes=REMEMBER_MINUTES(30天, auth_router.py:62) │
│       remember=False → minutes=None(默认8h, config.py:29)         │
└──────────────────────┬───────────────────────────────────────────┘
                       │
            ┌──────────┴──────────┐
            ▼                     ▼
┌───────────────────┐  ┌─────────────────────────────────────────────┐
│ 直接发 TokenOut   │  │ 外网两步闸（浏览器外网 + 非admin + 闸开）    │
│ {access_token,    │  │                                             │
│  token_type,      │  │ issue_code (gate.py:129-158):                │
│  user}            │  │   ① 同账号1分钟限频→429                      │
│ → 客户端写        │  │   ② 作废旧未用码                             │
│   localStorage    │  │   ③ code = 6位数字 (secrets.randbelow)       │
│   → 进主页        │  │   ④ pre_token = 192bit随机                   │
└───────────────────┘  │   ⑤ 存 LoginGateCode: user_id +              │
                       │      code_hash(sha256) + pre_token +         │
                       │      expires_at(now+10min)                    │
                       │   ⑥ push_message(to_role="manager") 发码     │
                       │      (站内Message + 企微双通道)               │
                       │                                             │
                       │ 返回 GateRequiredOut{gate_required:true,     │
                       │   pre_token, message:"已通知管理层..."}       │
                       │                                             │
                       │ POST /api/auth/login/verify-gate             │
                       │ (auth_router.py:112-130):                    │
                       │   入参: GateVerifyIn{username, pre_token,    │
                       │     code, remember}                          │
                       │   ① 查用户 + is_active, 不验密码              │
                       │   ② gate.verify_code (gate.py:161-177):      │
                       │      找行: user_id+pre_token+used=False       │
                       │      → 不存在/过期 → 400                      │
                       │      → 哈希不符 → fail_count+1 → 400         │
                       │      → 成功 → used=True                      │
                       │   ③ 通过 → last_login + _issue_token         │
                       └─────────────────────────────────────────────┘
```

### 1.2 鉴权链路（每次请求）

```
请求 ──→ axios 拦截器: Authorization: Bearer <localStorage.pms_token>
          (frontend/src/api/index.ts:12-13)
          (+ 桌面端额外三个统计头, :18-22)
     ──→ FastAPI 后端: get_current_user (backend/app/deps.py:11-28)
          ① decode_token(HS256, secret_key) → 取 sub → int(user_id)
          ② select(User).where(id==...) → 校验 is_active
          ③ 三类 401: 无/非 Bearer 头 → "未登录"
                       token 解码失败 → "登录已过期"
                       用户不存在/停用 → "账号已禁用"
          【每请求查一次 DB，无缓存、无 JWT 白名单】
```

### 1.3 登出

```
POST /api/auth/logout (auth_router.py:167-170)
  → 无状态，服务端仅返回 Msg("已登出")
  → 客户端 auth.logout() (auth.ts:102-110):
       try { authApi.logout() } catch { /* ignore */ }
       然后清 localStorage 四 key: pms_token/pms_user/pms_menus/pms_can_view_detail
  → 登出请求失败被静默吞掉，若后端审计依赖 logout 记录会丢日志
```

> 证据：backend-login-flow.md §1-4；gate-analysis.md §1；frontend-login-flow.md §1

---

## 2. 端点清单

| 端点 | 文件:行号 | 入参 | 返回 | 说明 |
|---|---|---|---|---|
| `POST /api/auth/login` | auth_router.py:73-109 | LoginIn{username, password, remember} | TokenOut 或 GateRequiredOut | 单端点登录，无其他协议入口 |
| `POST /api/auth/login/verify-gate` | auth_router.py:112-130 | GateVerifyIn{username, pre_token, code, remember} | TokenOut | 外网两步闸第二步 |
| `GET /api/auth/me` | auth_router.py:133-135 | Bearer 头 | UserOut（含 role_codes, menus, can_view_detail 等派生值） | 刷新用户信息 |
| `GET /api/auth/menus` | auth_router.py:138-147 | Bearer 头 | 菜单键列表 + can_view_detail | 前端侧边栏渲染唯一权威 |
| `POST /api/auth/change-password` | auth_router.py:150-164 | ChangePasswordIn{old_password, new_password: min_length=6, max_length=128} | Msg | 校验原密码→新旧相同拒 400→hash_password。改密不吊销已有 token |
| `POST /api/auth/logout` | auth_router.py:167-170 | Bearer 头 | Msg("已登出") | 无副作用，仅返回提示 |
| `GET /api/admin/gate-config` | admin_router.py:425-427 | Bearer（admin/manager） | GateConfigOut | 闸门配置读取 |
| `PUT /api/admin/gate-config` | admin_router.py:434-437 | Bearer（admin/manager） + GateConfigIn | GateConfigOut | 闸门配置写入，四 key 一并 upsert |
| `POST /api/desktop/report` | desktop_router.py | 无认证 | 200 | 崩溃上报，**故意不认证**（登录前崩溃也要收得到），防滥用：kind 白名单 + detail 截断 64KB + 每 device 每天 20 条超限返 200 |

### 2.1 关键端点详解

**login 端点（auth_router.py:73-109）**：
- 密码错/用户不存在统一 401，不泄露账号存在性
- 纯登录失败（密码错 401）不写审计日志
- `last_login` 存 UTC（非业务时区 UTC+8）

**verify-gate 端点（auth_router.py:112-130）**：
- 验码时**不重查闸门**：发码后 10 分钟内管理员关闸或 IP 变化，老码仍有效。设计合理（验码期间 IP 可能漂移）
- 验码**不验密码**：凭 pre_token + code 即可
- 用户不存在或非活跃 → 统一 400 "验证码无效或已过期"（不区分原因）
- 错误码只 fail_count + 1 并 commit，不再锁定（2026-07-28 起）

**change-password 端点**：
- 校验原密码（错 → 400），新旧相同 → 400
- 改后 `password_must_change=False`（首登强改标志清除）
- 写审计 `change_password`
- **改密不使已签发 token 失效**——JWT 无状态，没有 token 版本号/黑名单

> 证据：backend-login-flow.md §2-3；gate-analysis.md §5

---

## 3. 免闸顺序与判定链

### 3.1 判定链（唯一权威：auth_router.py:86-108）

按代码执行顺序，优先级从高到低：

```
1. admin 角色 (has_role("admin"))
   └─ 恒免闸，根本不进入闸门判定。代码: if not u.has_role("admin"): ...
      不满足此条件才继续往下走。
      (auth_router.py:92)

2. 桌面客户端 (X-PMS-Client 头 startswith "desktop/")
   └─ desktop_exempt (gate.py:67-84):
      ├─ device_gate 关（默认 False）→ 带客户端头即免闸
      └─ device_gate 开（True）→ 还要求 X-PMS-Device 在 device_ids 名单
      浏览器侧不带 X-PMS-Client 头 → 永远不命中此分支。
      (gate.py:74-76 注释自认: device_gate 默认关)

3. 内网 IP (is_intranet, gate.py:54-64)
   └─ 回环/私网 IP 恒判内网（ipaddress.is_loopback/is_private）:
      127/8、10/8、172.16/12、192.168/16、::1、fc00::/7
      ——注释: 不可公网路由，天然不可能是外网来源
   └─ intranet_cidrs 名单额外覆盖办公网公网出口 IP
      匹配逻辑 _ip_in (gate.py:38-51): 单 IP 按 /32、CIDR 按网段、非法条目 continue

4. gate_enabled=0
   └─ 关闸，所有来源免闸
   └─ 默认值: get_gate_config 返回 enabled=True（gate.py:102 注释: "gate_enabled 默认开"）
      seed 不预置此 key → 全新环境闸门默认打开
```

### 3.2 关键设计决策

- **manager 角色不免闸**（gate.py:1-2 docstring 写明）——manager 恰是验证码审核接收方，免闸则破坏模型。
- **免闸 vs 验码路径共用签发函数**：`_issue_token`（auth_router.py:65-70）对两条路径一模一样，不同仅在于中间是否触发 `issue_code`。
- **客户端真实 IP 信赖链**：X-Real-IP 可信的前提是「所有公网流量必经 nginx」。若绕过 nginx 直连 uvicorn，可设 `X-Real-IP: 127.0.0.1` 伪造内网 IP 绕过闸门——这是依赖部署拓扑的隐含前提，代码层无任何反绕过措施（gate-analysis.md §3）。
- **验证码存储**：`LoginGateCode` 表只存 sha256（gate.py:147），明文只走 push_message 正文（gate.py:155-156）→ 站内 Message 表 + 企微应用消息双通道。**码明文滞留 Message 表，到期后未查见清理任务**（gate-analysis.md §13）。

> 证据：gate-analysis.md §2-4；backend-login-flow.md §2.1；multi-entry.md §二

---

## 4. 各端登录差异矩阵

### 4.1 维度对照表

| 维度 | 浏览器网页版 | H5/手机浏览器 | 手机APP(Capacitor) | 桌面客户端(Electron) | demo |
|---|---|---|---|---|---|
| **登录页面** | LoginView.vue | H5LoginView.vue | H5LoginView.vue（同一套） | 同一 LoginView.vue（file:// 内置） | 同浏览器 |
| **API 地址** | 相对 `/api` (nginx 代理) | 相对 `/api` | `VITE_API_BASE=http://8.141.123.141`（构建时注入） | `VITE_API_BASE=http://8.141.123.141` | 127.0.0.1:8000 |
| **路由模式** | createWebHistory() | hash 模式 | hash 模式 | createWebHashHistory()（file:// 兼容） | createWebHistory() |
| **Vite base** | `/` | `'./'` | `'./'` | `'./'` | `/` |
| **axios 实例** | `@/api/index.ts`（含 element-plus） | `h5/http.ts`（轻量，无 element-plus） | `h5/http.ts` | `@/api/index.ts` | `@/api/index.ts` |
| **外网免闸** | ❌ 必走验证码闸（闸开时） | ❌ 同浏览器 | ❌ 同浏览器 | ✅ X-PMS-Client 头免闸 | ✅ 回环 IP 恒内网 |
| **统计头** | 无 | 无 | 无 | X-PMS-Client/X-PMS-Device/X-PMS-User | 无 |
| **强制升级** | 无 | 无 | H5 热更新（OTA） | 启动时 + 登录前两个时机 | 无 |
| **401 跳转** | location.href='/login' | location.hash='#/login' | 同 H5 | location.hash='#/login' + reload() | 同浏览器 |
| **WS 鉴权** | ?token= | ?token= | ?token= | ?token=（ws 不带头） | 同浏览器 |
| **token 存储** | localStorage pms_token | 同上（共享 key） | 同上 | 同上 | 同上 |
| **后端起别** | 浏览器，按 IP 判定 | 浏览器（服务端无法区分网页版） | 浏览器（服务端无法区分） | is_desktop=True | 127.0.0.1 恒内网 |

### 4.2 关键差异详解

**H5 与网页版的隐性耦合**（frontend-login-flow.md §10, §13；multi-entry.md §八）：
- 两者共享同一对 localStorage key（`pms_token`/`pms_user`）。同一浏览器先登网页版再开 H5 可复用登录态（注释说是故意的）。
- 但任一方 logout/401 清除 key 会牵连另一方。axios 实例是两份（`@/api/index.ts` vs `h5/http.ts`），互不知道对方状态。
- H5 的 401 只清 2 个 key（不碰 pms_menus）；网页版 store.logout 清 4 个。

**#343 桌面端黑屏真因**（desktop-login.md §6）：
- 桌面端早期 401 跳 `location.href='/login'` 在 `file://` 下解析成 `file:///login`（不存在），Chromium 换空错误页，只剩 backgroundColor 深蓝。每次 token 过期（8h）必踩一次。
- 已修：桌面走 hash 跳转 `location.hash='#/login' + location.reload()`。

**桌面端主进程不感知登录态**（desktop-login.md §8, §10）：
- 崩溃自恢复、重启都不关心是否已登录（token 在 localStorage 天然续命，30 天有效期）。
- 若未来需"退出登录时清空会话缓存/上报注销设备"，需要新增 IPC，当前没有。

**手机 APP 外网照样要验证码**（multi-entry.md §三）：
- APP 走 H5 的 `http.ts`，不带 `X-PMS-Client` 头，服务端视角与浏览器无异，没有桌面那条免闸通道。
- `H5LoginView.vue:3-4` 注释明说「后端一行没改」。

> 证据：multi-entry.md §一-三；frontend-login-flow.md §7, §10, §13；desktop-login.md §4, §6, §8-10

---

## 5. 密码哈希与 JWT

### 5.1 bcrypt（backend/app/auth.py:9-23）

- `hash_password` = `bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt())`
- `verify_password` = `bcrypt.checkpw(plain.encode("utf-8"), stored_hash.encode("utf-8"))`
- **任何异常（含哈希格式损坏）返回 False**，不抛错（`except Exception: return False`）
- 两环境（SQLite/Postgres）零差异——纯 Python 无 DB 依赖

### 5.2 JWT 签发与解码（auth.py:26-45）

- 算法：HS256（config.py:30），对称密钥 = `settings.secret_key`
- 签发：`payload={"sub": str(subject), "exp": expire}` → `jwt.encode(payload, secret_key, algorithm)`
- 解码：`jwt.decode`，`PyJWTError` 及一切异常返回 None
- **无 refresh token、无刷新端点、无服务端会话表**：全仓 grep `refresh_token|/refresh` 零命中
- **无 token 吊销/黑名单/版本字段**——token 在 exp 前恒有效，除非账号被 `is_active=False`（get_current_user 每请求查库拦截）
- 有效期：默认 8 小时（480 分钟，config.py:29），记住我 30 天（auth_router.py:62，只延长 exp，密码不落客户端）

### 5.3 `_issue_token` 完整细节（auth_router.py:65-70）

```python
async def _issue_token(db, u, ip, remember=False, audit_action="login"):
    minutes = REMEMBER_MINUTES if remember else None  # 480 或 30*24*60
    token = create_access_token(str(u.id), minutes=minutes)
    write_audit(db, u.id, audit_action, ip=ip, ...)   # 同步写审计
    return TokenOut(...)
```

- login 端点与 verify-gate 端点**共用此函数**，仅 audit_action 不同：login→"login"，verify-gate→"login_verify"
- 两个审计动作都可作为闸门码行的隐式 commit 锚点（见 §9.3）
- 中间无 push_message/发码/锁 —— 签发后的 token 就是最终凭证

### 5.3 密钥安全

- 开发默认值：**`"demo-secret-key-change-in-prod"` 硬编码**（config.py:28）
- 生产覆盖：`deploy.sh:38-43` 首次部署自动生成强随机值写盘，并校验不留占位值（deploy.sh:56-57）
- 泄露后果：可离线伪造任意用户 JWT

> 证据：backend-login-flow.md §3, §7

---

## 6. 鉴权依赖链

### 6.1 get_current_user（deps.py:11-28）

```python
# 每次请求完整执行:
scheme, token = header.split()
if scheme.lower() != "bearer" or not token: → 401 "未登录"
payload = decode_token(token)
if not payload or "sub" not in payload: → 401 "登录已过期"/"无效凭证"
user = await db.get(User, int(payload["sub"]))
if not user or not user.is_active: → 401 "账号已禁用"
return user
```

**每请求查一次 DB**，无缓存、无 JWT 白名单。

### 6.2 派生依赖（deps.py:31-88）

- `require_admin` = `require_roles("admin")`：单一角色
- `require_admin_or_manager` = `require_roles("admin", "manager")`：或关系
- `require_roles(*codes)`（deps.py:44-58）：admin/manager 恒放行 → user.has_role(any) 任一命中即通过。**并集语义**（不是交集）
- `require_not_viewer`（deps.py:61-66）：当前用户角色不含 "viewer" 即放行
- `require_can_view_detail`（deps.py:69-77）：读取 `user_menu_keys()` 中 submenu_show 控件的键，判定行/详单可点性
- 所有依赖的共同特征：接收 `current: User = Depends(get_current_user)` —— 即每请求先查库验证 token → user 存在 → 再走角色判定

### 6.3 WebSocket 鉴权

WS 无法带 Authorization 头，走 `?token=` query 参数 + decode_token 校验。桌面客户端 axios ws 不带 X-PMS 头，身份靠 `?token=`。

> 证据：backend-login-flow.md §4

---

## 7. 前端登录完整链路

### 7.1 LoginView 提交到进主页

```
LoginView.onSubmit (LoginView.vue:68-92)
  ├─ (桌面端) enforceVersionBeforeLogin → 版本低则切强制更新页，return
  └─ authApi.login(username, password)
       ├─ 直接 token → finishLogin(resp)  (LoginView.vue:79-86)
       └─ gate_code → 进两步闸 → gateApi.verify → finishLogin  (:114-120)

finishLogin(resp) (LoginView.vue:52-66)
  ├─ localStorage.setItem('pms_token', resp.access_token)
  ├─ localStorage.setItem('pms_user', JSON.stringify(user))
  ├─ localStorage.removeItem('pms_menus') —— 清旧菜单缓存
  ├─ (桌面) checkUpdateSilent —— 30分钟节流静默检查更新
  └─ router.push('/overview')
```

### 7.2 MainLayout 初始化（MainLayout.vue:125-145）

```
onMounted 序列:
  1. auth.fetchMe() —— GET /auth/me，401时内部 logout()
  2. 强制改密弹窗（password_must_change）
  3. auth.fetchMenus() —— GET /auth/menus，写 pms_menus + pms_can_view_detail
  4. refreshUnread() + 60s 轮询（消息角标）
  5. checkFeedbackReplies() —— 反馈回复弹窗
```

### 7.3 auth store（stores/auth.ts, 117 行）

| store 字段 | 初始化来源 | key |
|---|---|---|
| token | localStorage | pms_token |
| user | localStorage (JSON) | pms_user |
| menus | localStorage | pms_menus |
| canViewDetail | localStorage | pms_can_view_detail |

- `isLoggedIn = !!token` —— 只看 localStorage 有无 token，**无前端过期时间戳**。过期/失效全依赖后端 401，由三处之一清态：axios 响应拦截器、fetchMe 内部、H5 独立拦截器
- `isAdmin = hasRole('admin','manager')` —— 与 AGENTS.md 一致

### 7.4 401 三处处理路径

1. **axios 响应拦截器**（api/index.ts:122-140）：`status===401 && !isLoginRequest` → 清 pms_token/pms_user → goLogin()
2. **fetchMe 内部**（auth.ts:89）：401 时直接 logout()
3. **H5 独立拦截器**（h5/http.ts:27-36）：401 → 只清 pms_token/pms_user → hash 跳 #/login（不碰 pms_menus，H5 不用菜单）

### 7.5 VITE_API_BASE 一处开关的四处分叉

| 落点 | 浏览器（不设） | 桌面打包（=http://8.141.123.141） |
|---|---|---|
| axios baseURL (api/index.ts:7) | `/api` (Vite 代理/nginx) | `http://8.141.123.141/api` (直连) |
| 路由模式 (router/index.ts:8) | createWebHistory() | createWebHashHistory() |
| 401 跳转 (api/index.ts:91,107-119) | `pathname==='/login'` | `hash.startsWith('#/login')` |
| Vite base (vite.config.ts:10) | `/` | `'./'` (file:// 相对路径) |

### 7.6 WS 实时连接

- 从 localStorage 取 pms_token（无则直接不连）
- 地址：桌面从 VITE_API_BASE 推导（http→ws/https→wss）；浏览器按 location 拼
- 401 不主动断 ws：靠服务端踢（onclose）后重读 token，token 已清则停止重连。被动收敛，非主动登出联动

> 证据：frontend-login-flow.md §1-9

---

## 8. 桌面客户端登录集成

### 8.1 三件核心介入

桌面登录 = 标准 POST /api/auth/login，Electron 壳只做三件事：

1. **preload 注入 `window.pmsDesktop`**（preload.js:8-42）：`contextBridge.exposeInMainWorld` 注入 `{isDesktop:true, version, deviceId, ...}`，配合 `contextIsolation:true` + `nodeIntegration:false`——渲染进程无 Node 权限
2. **axios 三统计头**（api/index.ts:14-25）：`X-PMS-Client: desktop/<version>`、`X-PMS-Device: <deviceId>`、`X-PMS-User: <username>`
3. **强制升级闸门**（main.js:310-350）：启动 + 登录按钮按下前两个时机，网络不通放行

### 8.2 deviceId 持久化

- 存 `userData/device.json`，首次 `crypto.randomUUID()` 生成
- 写失败置 `deviceIdPersisted=false`，本次仍可用但**每次启动换 ID** → 服务端设备名单永远认不出此机
- 启动时上报 `sendReport('error',...)`（main.js:753-759，注释点名杀软拦 `%APPDATA%` 写入）

### 8.3 webSecurity:false 与安全补偿

- `webSecurity:false`（main.js:481-483）让 `file://` 直连 HTTP API 绕 CORS
- 配套三件套（main.js:578-588）：① `setWindowOpenHandler` 外链交系统浏览器 ② `will-navigate` 非 file:// 拒绝 ③ 内置前端本身不加载线上 URL
- **X-PMS-Client 头可被伪造**：`curl -H "X-PMS-Client: desktop/x"` 即绕闸门。真防线 `device_gate` 默认关。

### 8.4 黑屏自恢复（三道防线）

| 层 | 触发条件 | 恢复策略 |
|---|---|---|
| did-fail-load | 主框架加载失败（非 ERR_ABORTED） | 退内置首页 loadFile(indexHtml) |
| render-process-gone/unresponsive | 渲染崩溃/被杀/OOM/卡 15s | reload()；5min 内 3 次 → relaunchApp() |
| GPU 进程崩 + 画面心跳 | `child-process-gone` type='GPU'，画面心跳（rAF 每 2s）超 45s | relaunchApp()（GPU 崩不能用 reload 救） |

- 重启后自动降级：`.gpu-crashed` 标记 → 下次启动 `app.disableHardwareAcceleration()`
- token 在 localStorage，重启后不用重登

### 8.5 PENDING_FILE 安装失败回溯

安装器在 app 退出后跑，崩溃时无法当场上报。`main.js:263-285`：下载完成记目标版本到 `userData/pending-update.json`，下次启动当前版本没变 = 失败，上报后累加 attempts。

### 8.6 窗口启动链路（创建到用户可见）

```
main.js:595-606 createWindow:
  show:false + backgroundColor:'#0f1d30'  # 与登录页底色一致防闪白
  → createSplash() (main.js:135-156)       # 无框 400×470 PNG 启动图
  → mainWindow.loadFile(app/index.html)
  → 前端挂载后 Vue main.ts 调 notifyReady
  → ipcMain 'pms-desktop:app-ready'        # 300ms 延时防白屏
  → revealMainWindow(): splash.destroy() + mainWindow.show() + focus()
  → 兜底: 10s 后 setInterval 保底 show   # main.js:496，防老版不发 app-ready
```

用户看到启动页→启动页消失亮出主窗。若 10s 内前端未发 app-ready（老版/加载慢），强制亮窗不卡死。

> 证据：desktop-login.md §1-7

---

## 9. 安全风险与改进点

### 9.1 已识别的安全边界

| 风险 | 严重度 | 现状 | 证据 |
|---|---|---|---|
| 开发 `secret_key` 硬编码 | 高 | 生产 deploy.sh 自动生成强随机值覆盖。但若漏配则泄露 = 可伪造任意用户 token | backend-login-flow.md §7 |
| X-PMS-Client 头可 curl 伪造绕过验证码闸 | 高 | device_gate 默认关、intranet_cidrs 默认空 → 外网免闸实际只依赖"谁都知道怎么伪造的客户端头" | gate-analysis.md §8; desktop-login.md §9 |
| POST /api/auth/login 无任何限频/失败锁定 | 中 | 防暴力破解唯一手段是外网两步验证码闸。admin/桌面/内网免闸路径完全无速率限制 | backend-login-flow.md §5 |
| 码明文滞留 Message 表 | 中 | 6 位码写进 Message 文本（gate.py:155-156），到期后未查见清理任务 | gate-analysis.md §9.2 |
| verify-gate 无限频 | 低 | 攻击前提：先拿到 pre_token（login 第一步 192bit 随机），之后 10 分钟内理论上可穷举 1e6 码 | gate-analysis.md §5 |
| 改密不吊销已有 token | 低 | JWT 无状态、无版本号/黑名单；改密后旧 token 在 exp 前仍有效（仅 is_active=False 才拦截） | backend-login-flow.md §3, §8 |
| 绕过 nginx 直连 uvicorn 时 X-Real-IP 可伪造 | 中 | 设 `X-Real-IP: 127.0.0.1` → is_loopback 恒免闸。代码无反绕过措施，依赖部署拓扑假设 | gate-analysis.md §3, §8 |
| H5 与网页版共享 token key 导致双向登出耦合 | 低 | 任一方登出清 key，另一方掉线。设计上的"复用"与"耦合"并存 | frontend-login-flow.md §13 |
| nginx 限频只挂 /login，verify-gate 无限频 | 低 | verify-gate 走通用 /api/ 位置；限频靠 gate.py 发码 1 条/分兜底 | multi-entry.md §四 |

### 9.2 过时注释

| 位置 | 原文 | 实际行为 | 影响 |
|---|---|---|---|
| models.py:1204 | `fail_count` 注释"连续错码次数（>=5 锁定）" | 2026-07-28 已去掉锁定 | 误导 |
| models.py:1146 | AppSetting docstring "仅 Agent 助手用来存 LLM 配置" | gate 四个配置键也存此表 | 误导 |

### 9.3 码行落库的隐式契约

`issue_code`（gate.py:129-158）add 码行后不显式 commit，依赖**调用方的 audit commit 副作用**将行真正写入：

```
gate.issue_code(db, user_id, pre_token, code)
  → db.add(LoginGateCode(...))                        # 悬而未提交
  → push_message(to_role="manager")                    # 通知管理层
      └─ 若无 manager active → early return, 不 commit (notify.py:60-62)
  → 回到调用方 login 端点:
      write_audit(db, ..., action="login_gate_issue")  # 此时才 commit
```

码行 + 审计日志 **atomatically 作为同一事务提交**（auth_router.py:103-105）。若 write_audit 因某些原因不执行（不会发生，current 无此代码路径），码行就丢了。这一约定还意味着：改变 login 端点的事务结构时，不能把 write_audit 移到 issue_code 之前。

### 9.4 闸门配置存储细节

四 key 都存在 `app_settings` 表（models.py:1145-1151）：
- `gate_enabled`（bool）：默认 `get_gate_config` 无值时返回 True（gate.py:102）
- `gate_intranet_cidrs`（str, JSON 数组）：seed 不预置 → 初始为空，需用户配置
- `gate_device_ids`（str, JSON 数组）：设备名单，脏数据当空名单执行（gate.py:112-116）
- `gate_device_gate`（bool）：默认 False（未配置时 false-ifies，gate.py:120-122）

这些 key 在 `get_gate_config` 方法（gate.py:96-127）统一读取：每请求一次 select + 解析 JSON + 返回字典，无缓存热路径。`put_gate_config` 四次 upsert（gate.py:110-126）。

### 9.5 已知未完成项

- `intranet_cidrs` 名单待用户配置（AGENTS.md 注明）
- `device_gate` 默认关；如需强制设备名单验证需管理层手动打开（前端 GateConfigView.vue 有二次确认提示）
- 码明文在 Message 表的清理逻辑未查见

> 证据：gate-analysis.md §8-9, §13；backend-login-flow.md §5, §7

---

## 10. 测试现状与已知坑

### 10.1 登录/鉴权专属测试

| 测试文件 | 长度 | 覆盖 | 状态 |
|---|---|---|---|
| test_login_gate.py | 222 行 | 闸门全流程：免闸四路径、两步闸、错码/对码/重放/过期/不锁定、限频 429、配置权限 | 通过（AGENTS.md 未列挂） |
| test_gate_device_ids.py | 160 行 | 设备闸 desktop_exempt 真值矩阵、脏数据当空名单、内网恒免、保存不冲 cidrs | 通过 |
| test_desktop_report.py | — | 崩溃上报免认证、超限返 200 而非 429 | 通过 |
| test_smoke_startup.py | — | 启动+seed+admin 登录冒烟 | AGENTS.md 注"实测本就能过" |

### 10.2 基线挂测试中与登录/鉴权相关

AGENTS.md 标题称"**13 个**测试在基线 HEAD 上就挂"，清单实际罗列 **14 个名字**（m01/m02/m04/m07/m08/m12/m13/m14/m15 + 2个e2e + outsourcing_template + user_feedback + void_sales_order）。差额未核实。

直接相关：
- test_m01_roles_menus.py（剩 4 个 #91 详单闸门 403 断言）
- test_e2e_business_flows.py / test_e2e_full_lifecycle.py（全流程含登录）
- test_user_feedback.py（403 越权断言）
- test_m13_feedback.py（feedback 域）

### 10.3 历史登录 bug 与修复

| 提交 | 日期 | 内容 |
|---|---|---|
| 393c7f7 | 2026-07-28 | 外网上线两步闸门：浏览器外网登录需随机码（发管理层企微），admin/客户端/内网免闸 |
| 446e33b | 2026-07-28 | 闸门放宽：去掉每日 10 条发码上限与错 5 次锁定，保留 1 分钟 1 条 |
| 8fe9688 | 2026-08-03 | 客户端设备限制：按设备 ID 控制登录，开关默认关 |
| 714daa5 | 2026-08-03 | #343 真身：401 跳登录页在文件协议下解析成 file:///login |

### 10.4 测试环境 SQLite vs Postgres 差异

登录测试全部只在临时 SQLite 上跑。DateTime(timezone=True) 在 SQLite 读回 naive、Postgres 读回 aware——**唯一兜底：gate.py 的 `_aware()`（gate.py:33-35）** 按 UTC 补齐时区。Postgres 下 `_aware` 是空操作，行为无差。今后写涉及 expires_at 的时间比较必须复用 `_aware` 或走 ORM 比较，不能裸比 naive datetime。

> 证据：login-tests.md §1-5；gate-analysis.md §12

### 10.5 测试覆盖缺口

以下边界场景在当前测试中未覆盖，属已知盲区：

| 缺口场景 | 风险 | 说明 |
|---|---|---|
| verify-gate 无限频 | 中 | test_login_gate 只测发码端限频，验码端无对应断言；nginx 也放过 verify-gate（§12.1），防线仅剩码 10 分钟 TTL |
| X-PMS-Client 伪造验证 | 中 | test_gate_device_ids 测了 device_ids 矩阵，但未构造 X-PMS-Client 头来验证"没有配套 device_id 时仅凭头也能免闸" |
| 多 worker 并发 seed | 中 | 仅有 pg_advisory_lock 串行化，无并发登录测试——两个 worker 同时启动并同时处理 login 请求的竞态未被验证 |
| remember token 跨端冲突 | 低 | H5 与网页版共享 localStorage key "pms_token"（frontend-login-flow.md §13），两端登出互不通知——无测试验证 H5 登出是否会让网页端 401 |
| 生产 PG 的 _aware 兜底 | 低 | 所有闸门测试在 SQLite 上跑（gate-analysis.md §12），expires_at 的 DateTime(timezone=True) 在 PG 下的 aware 行为未在测试中验证——全靠 gate.py `_aware()` 设计上兜底 |
| 闸门配置竞态 | 低 | AppSetting 无乐观锁/ETag，管理员 A、B 同时调 PUT /gate/config 会静默覆盖——无并发写测试 |

---

## 11. 种子账号与启动

- **创建时机**：main.py lifespan 中 `seed(db)`（每次启动，power on 型，已存在则跳过）
- **admin**：username=settings.default_admin_username（默认 "admin"），password=settings.default_admin_password（默认 "admin123"），password_must_change=True。生产可用环境变量覆盖
- **manager**：硬编码 `manager / manager123`，password_must_change=True。**无对应环境变量覆盖**
- **角色表**：24 个角色（ROLES），幂等（存在则同步 name/description，can_push 仅空值时初始化）
- **demo**：同 seed，只是 SQLite + 无 nginx → 回环 IP 天然免闸

### 11.1 启动并发与 pg_advisory_lock

生产环境 `--workers 4` 下，FastAPI 多 worker 同时执行 lifespan 的 `seed()`，不加锁会撞 DuplicateColumn。main.py:64-90 的三次锁时序：

1. **第一次 `pg_advisory_lock(872193641)`**：在 `create_all` 前获取。串行化建表——若 worker A 正创建新表、worker B 也读到 metadata 相同，持锁排队。锁内执行 `conn.run_sync(Base.metadata.create_all)`，完成后释放。
2. **第二次加锁**：在 `seed(db)` 前重新获取同一个咨询锁（872193641）。seed 内批量 upsert 角色/用户/默认配置，任何写冲突（如两个 worker 同时 `INSERT ... ON CONFLICT DO NOTHING`）都被锁串行消除。
3. **第三次加锁**：在 `run_data_migration(db)` 前最后一次获取同锁。data_migration.py 按版本号顺序执行增量迁移脚本，加锁确保不会两个 worker 同时执行同一脚本导致重复变更。

三次加锁用的是同一个 `872193641` 锁 ID（硬编码 magic number），锁在 worker 进程生命周期内由 PG 自动释放（session 断开即释）。SQLite 下无此保护——但 SQLite 本身是单连接单写，天然不含并发启动问题。

> 证据：backend-login-flow.md §6-7；multi-entry.md §五

---

## 12. nginx 与登录相关

### 12.1 限频

```nginx
# default.conf:2-3
limit_req_zone $limit_req_key zone=login_limit:10m rate=10r/m;

# _shared-locations.inc:39-41
location = /api/auth/login {
    limit_req zone=login_limit burst=5;
    limit_req_status 429;
    proxy_pass http://backend;
}
```

- 精确匹配 `= /api/auth/login`，**不匹配 `/api/auth/login/verify-gate`**
- verify-gate 走通用 `location /api/` → 不触达 `login_limit` zone → 无限频
- 10r/m = 每分钟做多约 10 次请求（实际会更高：10r/m 允许约每 6 秒一次，burst 5 可缓冲峰值）
- 所有使用 `KEY_FRAG_COUNT` 拼出的 `$limit_req_key`（按 IP+登录端点聚合）

### 12.2 X-Real-IP

每个 proxy location 都设 `proxy_set_header X-Real-IP $remote_addr`（_shared-locations.inc:43），是 `_client_ip()` 取 IP 的唯一信任源。重点：`$remote_addr` 是 nginx 直连的 TCP 对端，不可伪造。

### 12.3 企微域名验证 + SSL

- `~ ^/WW_verify_.*\.txt$` 服务 `nginx/wecom-verify/` 目录（docker-compose 挂 `/var/www/wecom:ro`）
- ACME http-01，`./enable-https.sh` / `./renew-cert.sh` 管理
- SSL 证书挂载：`docker-compose.prod.yml:73-74`

> 证据：multi-entry.md §四

---

## 13. 跨文档一致性校验

| 检查项 | n3 | n4 | n5 | n6 | n7 | n8 | 一致? |
|---|---|---|---|---|---|---|---|
| 免闸顺序 admin→桌面→内网→开关 | 一致 | 一致 | 一致（前端不参与判定） | 一致 | 一致（断言覆盖） | 一致 | ✅ |
| JWT 有效期 8h/30d | 一致 | 间接 | 8h（注释） | 30天免登录 | — | 一致 | ✅ |
| IP 取址链 X-Real-IP→XFF 末段→直连 | 一致 | 一致 | 不涉及 | 不涉及 | — | 一致 | ✅ |
| verify-gate 不重查闸门 | 隐式（共用签发） | 明确 | — | — | — | — | ✅ |
| X-PMS-Client 可伪造 | — | 明确 | 明确 | 明确 | 断言验证 | 明确 | ✅ |
| 无 refresh token | 明确 | 间接 | 明确（无过期时间戳） | 明确 | — | — | ✅ |
| 种子账号 | admin/admin123, manager/manager123 | — | — | — | — | admin/manager 硬编码 | ✅ |
| H5 与网页版共享 token key | — | — | 发现此耦合 | — | — | 明确 | ✅ |
| manager 不免闸 | — | 明确 | — | — | — | — | ✅ |
| nginx 限频只挂 /login | — | — | — | — | — | 明确 | 待核实 |
| fail_count 注释过时 | — | 发现 | — | — | 发现 | — | ✅ 同发现 |
| AppSetting docstring 过时 | — | 发现 | — | — | — | — | ✅ |
| 13 vs 14 个挂测试 | — | — | — | — | 发现 | — | ✅ 同发现 |

**结论：6 片文档口径一致，无互相矛盾的关键结论。** 仅 `nginx 限频只挂 /login` 一项未交叉验证。

---

## 14. 分歧 / 遗留

1. **AGENTS.md "13 个"挂测试 vs 清单 14 个名字**：数量矛盾，需跑测试核实确切数量（login-tests.md §8.1）。
2. **`force_latest:true` 与 AGENTS.md「老客户端长期并存」存在张力**（desktop-login.md §10）：当前 `version.json` 判定口径是"必须通道最新版"——每次发新版所有旧客户端都被强制升级。属有意为之还是临时配置——待确认。
3. **码明文在 Message 表的清理**：`login_gate` 消息无清扫任务（gate-analysis.md §9.2, §13），长期积存。
4. **nginx 对 verify-gate 无独立限频**：验码端点的实际防线只有发码 1 条/分 + 码 10 分钟 TTL（multi-entry.md §七）。
5. **H5 与网页版 token 共享耦合是否是设计目标**（frontend-login-flow.md §13）：注释说是"刻意复用"，但登出连坐在代码里无区分标记。
6. **X-Real-IP 反绕过完全依赖部署拓扑**：绕过 nginx 直连 uvicorn 时回路 IP 可伪造（gate-analysis.md §3）——代码无任何防范。
7. **修改任何涉及登录会话时间的比较必须复用 `_aware`**：SQLite naive 与 Postgres aware 的唯一兜底点（gate.py:33-35），此后写测试/改门禁必须注意。

---

## 15. 相关文档索引

| 文档 | 内容 | 路径 |
|---|---|---|
| n3 后端认证全链路 | bcrypt+JWT+端点逐条+deps 链 | `backend-login-flow.md` |
| n4 外网闸门分析 | 免闸判定链/发码验码/配置/IP取址/安全边界 | `gate-analysis.md` |
| n5 前端登录与鉴权 | LoginView→store→axios→路由守卫→WS→H5 | `frontend-login-flow.md` |
| n6 桌面客户端登录 | Electron 壳/pmsDesktop/统计头/强制升级/黑屏自恢复 | `desktop-login.md` |
| n7 登录测试与坑 | 测试清单/基线挂测/历史bug/SQLite vs PG | `login-tests.md` |
| n8 多端入口差异 | 五端全景/nginx/ops/种子账号 | `multi-entry.md` |
| 早期探索 01-06 | 各独立探索轮次 | `01-backend-auth.md` 等 |
| 核验节点 n11-n32 | 发现复核/单点验证 | `n11-falsify-*.md` 等 |
