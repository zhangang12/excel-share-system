# 后端登录认证全链路（n3 探查）

> 边界：只读 `backend/app`；`gate.py`（外网登录两步闸的验码/发码/设备闸）归另一片，本文只述其在 `auth_router` 中的调用位置与免闸顺序，不展开内部实现。
> 证据格式：`文件:行号`。凡标「待核实」的均为推断，未混入结论。

## 1. 拓扑总览

```
POST /api/auth/login ──→ 查用户 → bcrypt 验密 → is_active → last_login
                            │
                            ├─ 非 admin 且命中 gate ──→ 返回 GateRequiredOut(pre_token)，等第二步
                            └─ 免闸/内网/桌面/admin ──→ _issue_token → JWT(HS256, exp) → TokenOut
POST /api/auth/login/verify-gate ──→ gate.verify_code → _issue_token
GET  /api/auth/me / menus    ← get_current_user（Bearer 头 → decode_token → 查 users 表）
POST /api/auth/change-password
POST /api/auth/logout        （无状态，仅返回提示）
```

- 所有端点挂在 `auth_router.py` 的 `APIRouter(prefix="/api/auth")`（`backend/app/routers/auth_router.py:14`）。
- **无 refresh token、无刷新端点、无服务端会话表**：全仓 grep `refresh_token|/refresh` 零命中。单次签发 JWT，过期只能重新登录（记住我会员续 30 天，见 §3）。
- 登出是无状态清理：`auth_router.py:167-170` 只返回 `Msg("已登出")`，注释明说「JWT 无状态，靠客户端清 token 即可」。

## 2. HTTP 端点逐条

### 2.1 `POST /api/auth/login`（`auth_router.py:73-109`）
- 入参 `schemas.LoginIn`（`schemas.py:700-705`）：`username: str`、`password: str`、`remember: bool=False`。**无最小长度/格式校验，密码不落客户端**。
- 返回 `TokenOut | GateRequiredOut`（`schemas.py:707-719`）：
  - `TokenOut{access_token, token_type="bearer", user: UserOut}`
  - `GateRequiredOut{gate_required=True, pre_token, message}`
- 逻辑顺序（`auth_router.py:75-109`）：
  1. `select(User).where(username==data.username)`；`verify_password` 失败或用户不存在 → 统一 `HTTPException(401, "用户名或密码错误")`（`auth_router.py:77-78`）。
  2. `is_active` 为 False → 403「账号已停用」（`auth_router.py:79-80`）。
  3. `u.last_login = datetime.now(timezone.utc)` 后 commit（`auth_router.py:82-84`）——**存的是 UTC**，非业务时区 UTC+8。
  4. 外网两步闸：`if not u.has_role("admin")` 才进 gate 判定（`auth_router.py:92`）；`admin` 角色**无条件跳过验证码**。免闸路径 = 内网 IP `gate.is_intranet` 或 桌面客户端免闸 `gate.desktop_exempt`（`auth_router.py:94-97`）。命中 `cfg["enabled"] and not exempt` 时 `gate.issue_code` 发码，返回 `GateRequiredOut`（`auth_router.py:98-108`）。
  5. 否则 `_issue_token` 直接发 token（`auth_router.py:109`）。
- 客户端真实 IP：`_client_ip` 优先 `X-Real-IP`（nginx `$remote_addr` 覆写、不可伪造），次取 `X-Forwarded-For` **末段**（nginx 把真实地址追加到链尾，取首段会被伪造的 XFF 骗过）（`auth_router.py:48-56`）。
- 桌面客户端判定：`x-pms-client` 头以 `desktop/` 开头（`auth_router.py:90`）。

### 2.2 `POST /api/auth/login/verify-gate`（`auth_router.py:112-130`）
- 外网登录第二步。入参 `GateVerifyIn{username, pre_token, code, remember}`（`schemas.py:721-726`）。
- 用户不存在或非活跃 → 400「验证码无效或已过期」（`auth_router.py:117-119`）。
- 调 `gate.verify_code(db, u, pre_token, code)`，失败 400/抛出；成功则更新 `last_login` 后 `_issue_token`（`auth_router.py:121-130`）。
- 两条路径共用签发函数：`_issue_token(db, u, ip, remember)` = `create_access_token` + `write_audit(action="login")`（`auth_router.py:65-70`）。

### 2.3 `GET /api/auth/me`（`auth_router.py:133-135`）
- 需 `get_current_user`；返回 `UserOut`（含 `role_codes`（finance_lead⊇finance 隐含）、`menus`、`grant_menus` 派生值等，`auth_router.py:17-45`）。

### 2.4 `GET /api/auth/menus`（`auth_router.py:138-147`）
- 需登录；返回当前用户可见一级菜单（`user_menu_keys`，admin/manager 全量 bypass）+ `can_view_detail`。这是前端侧边栏渲染的唯一权威。

### 2.5 `POST /api/auth/change-password`（`auth_router.py:150-164`）
- 需登录。`ChangePasswordIn{old_password, new_password: min_length=6, max_length=128}`（`schemas.py:743-746`）。
- 校验原密码（`verify_password(data.old_password, current.password_hash)`，错→400），新旧相同→400（`auth_router.py:156-159`）。
- 改后 `hash_password` 写入，并 `password_must_change=False`（首登强改标志清除），写审计 `change_password`（`auth_router.py:160-163`）。
- **改密不使已签发 token 失效**——JWT 无状态，没有 token 版本号/黑名单。

### 2.6 `POST /api/auth/logout`（`auth_router.py:167-170`）
- 需登录；无副作用，返回提示。

## 3. 密码哈希与 JWT（`backend/app/auth.py` 全文 45 行）

- **哈希**：bcrypt。`hash_password` = `bcrypt.hashpw(plain, bcrypt.gensalt())`；`verify_password` = `bcrypt.checkpw`，**任何异常（含哈希格式损坏）返回 False**，不抛错（`auth.py:9-23`）。
- **签发**：`create_access_token(subject, extra=None, minutes=None)` → `payload={"sub": str(subject), "exp": expire}`，`jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)`（`auth.py:26-36`）。
  - 默认 `exp` = 现在 + `settings.access_token_expire_minutes`（默认 480 分钟 = **8 小时**，`config.py:29`）。
  - 「记住我」：`REMEMBER_MINUTES = 30*24*60`（30 天，`auth_router.py:62`）；`_issue_token` 里 `minutes=REMEMBER_MINUTES if remember else None`（`auth_router.py:68`）。**只延长 token 有效期，密码不落客户端**。
- **解码**：`decode_token` 用同一 `secret_key`+`algorithm`，`jwt.PyJWTError` 及一切异常返回 None（`auth.py:39-45`）。
- **算法**：HS256（`config.py:30`）。对称密钥 = `settings.secret_key`。
- 无 token 吊销/黑名单/版本字段——token 一经签发在 exp 前恒有效，除非用户被 `is_active=False`（`get_current_user` 每请求查库拦截，见 §4）。

## 4. 鉴权依赖链

### 4.1 `get_current_user`（`backend/app/deps.py:11-28`）
- 要求 `Authorization: Bearer <token>` 头（大小写不敏感，`deps.py:15-16`）。
- `decode_token` → 取 `sub` → `int(user_id)` → `select(User).where(id==...)` → 校验 `is_active`（`deps.py:17-27`）。
- 三类 401：无/非 Bearer 头 →「未登录」；token 解码失败 →「登录已过期」；无 sub →「无效凭证」；用户不存在/停用 →「账号已禁用」（`deps.py:15-27`）。
- **每请求查一次 DB**，无缓存/无 JWT 白名单。
- 派生依赖：`require_admin` / `require_admin_or_manager`（`has_role("admin","manager")` 并集）、`require_roles(*codes)`（admin/manager 恒放行 + 并集语义）、`require_not_viewer`、`require_can_view_detail`（`deps.py:31-88`）。

### 4.2 WebSocket 鉴权
- WS 无法带 Authorization 头，`ws_router.py` 走 `?token=` query 参数 + `decode_token` 校验（桌面客户端 axios ws 不带统计头、靠 `?token=`，见项目知识）。此文件在 auth 之外但属认证链路；后续如需可单独核查 `ws_router.py` 的 token 校验行。

## 5. 登录限频与失败锁定

- **`/api/auth/login` 端点本身没有任何限频/失败计数/账号锁定**：全仓 grep `rate.?limit|限频|限流|failed_count|login_attempt|slowapi` 命中只有三处——`gate.py:21`（发码限频 1 条/分，属 gate 片）、`desktop_router.py:141`（桌面上报每设备每天 20 条，防滥用，与登录无关）、`models.py:1178`（故障上报的 detail 截断注释）。认证路径里无中间件级限流（`main.py` 仅有桌面统计中间件）。
- 密码错与用户不存在**返回同一条 401「用户名或密码错误」**（`auth_router.py:77-78`），不泄露账号是否存在；但**不计数、不锁定，可无限尝试**。这是现状，非缺陷描述，仅为排查登录暴力破解场景时的参考。
- 唯一登录相关限频在外网两步闸的发码端（`gate.issue_code` 同账号 1 条/分），**不在本文范围**（gate.py 归属另一片）。
- 审计侧有 `login_gate_fail` / `login_gate_issue` 写 `audit_logs`（`auth_router.py:102-105, 124-126`），但纯登录失败（密码错 401）**不写审计**。

## 6. 种子账号创建（`backend/app/seed.py`）

- 启动顺序：`main.py:81-86` 的 lifespan 内——`Base.metadata.create_all` → `ensure_schema_columns`（存量补列）→ `seed(db)` → `run_data_migrations(db)`。seed 在 create_all 之后。
- 角色：`ROLES` 共 24 个（admin/manager/designer/…/as_lead），幂等——存在则同步 name/description，`can_push` 仅空值时初始化（`seed.py:14-42, 47-58`）。
- **admin**：`username=settings.default_admin_username`（默认 `"admin"`），`password_hash=hash_password(settings.default_admin_password)`（默认 `"admin123"`），`password_must_change=True`，`role_id=admin 角色`，`is_active=True`（`seed.py:62-83` + `config.py:33-34`）。已存在则跳过。
- **manager**：username `"manager"`、密码 `"manager123"` **硬编码**，同样 `password_must_change=True`（`seed.py:88-103`）。
- **生产可覆盖 admin，不可覆盖 manager**：`docker-compose.prod.yml:34-35` 只透传 `DEFAULT_ADMIN_USERNAME/DEFAULT_ADMIN_PASSWORD` 环境变量；manager 无对应变量。
- `password_must_change=True` 意味着种子账号首登会被要求改密（前端据 `UserOut.password_must_change` 引导；`admin_router.py:168` 建账号也强制此标志）。改密入口只有 `POST /api/auth/change-password`（§2.5）；`ops/reset-admin-password.sh` 提供运维侧重置。

## 7. 开发（SQLite）vs 生产（Postgres）

| 项 | 开发默认 | 生产 | 证据 |
|---|---|---|---|
| 数据库 | `sqlite+aiosqlite:///./data/app.db`（单机演示） | `postgresql+asyncpg://...`（docker compose 注入） | `config.py:8` / `docker-compose.prod.yml:31` |
| `secret_key` | **`"demo-secret-key-change-in-prod"` 明文硬编码** | `SECRET_KEY` 由 `.env.prod` 注入；`deploy.sh:38-43` 首次部署自动生成强随机值写盘，并校验不留占位值 | `config.py:28` / `docker-compose.prod.yml:32` / `deploy.sh:56-57` |
| token 有效期 | 480 分钟（8 小时） | 环境变量 `ACCESS_TOKEN_EXPIRE_MINUTES` 可覆盖，默认仍 480 | `config.py:29` / `docker-compose.prod.yml:33` |
| 启动竞态 | 单进程，直接跑 | 4 worker 并发启动 → 用 `pg_advisory_lock(872193641)` 串行化 create_all/seed/migration，防 DuplicateColumn/UniqueViolation → 偶发 502 | `main.py:64-90` |
| 存量加列 | `create_all` 已按模型建对，`ensure_schema_columns` 跳过 | 需 `ALTER TABLE` 补列（幂等） | `data_migration.py:222` |
| 审计/用户表 | 同 schema（`users`/`audit_logs` 表两环境一致） | 同左 | `models.py:39-67`（users）、`utils.write_audit` |

- 认证逻辑本身**两环境零差异**：无方言分支，SQLite/Postgres 走同一套 bcrypt+JWT 代码。差异只在上表（配置来源 + 启动并发）。
- 外网两步闸对**浏览器外网**生效；本机/内网 IP 恒判内网（回环+私网），故本地测试不受 gate 影响（见项目知识，gate 片）。

## 8. 被排除的猜想 / 反例（下一棒不必再走）

- **「有 refresh token 双 token 机制」**：不存在。grep `refresh_token|/refresh` 零命中；登出即清 token、过期即重登。
- **「登录有失败锁定/限频」**：不存在于认证路径（§5）。早期 gate 曾有「错 5 次锁」，已按用户要求移除（`gate.py:21` 注释），且只在验证码环节。
- **「改密会吊销旧 token」**：不会。JWT 无状态、无版本号/黑名单，改密只换哈希；旧 token 在 exp 前仍可通过 `get_current_user`（仅当账号停用才拦截）。
- **「admin/manager 种子密码可随 env 全覆盖」**：admin 可（`DEFAULT_ADMIN_PASSWORD`），manager 不可（硬编码）。

## 9. 关键结论（供总览引用）

1. 登录 = 单端点单令牌：bcrypt 验密 → HS256 JWT（默认 8h，记住我 30d），无 refresh、无会话表、无吊销。
2. 认证门禁依赖 `get_current_user`（Bearer → decode → **每请求查 users 表** + `is_active` 检查），不是纯无状态验证。
3. `POST /api/auth/login` **无任何限频/失败锁定**——当前防暴力破解的唯一手段是外网两步验证码闸（gate，仅非 admin + 外网 + 非免闸路径），且该闸对 admin/内网/桌面免闸。
4. 开发默认 `secret_key` 是硬编码的明文 demo 值，**生产必须经 `.env.prod` 覆盖**（deploy.sh 已自动生成）；此密钥泄露=可离线伪造任意用户 token。
5. 种子账号 admin/admin123、manager/manager123 在启动时自动创建且首登强改密；生产 admin 密码可环境变量覆盖，manager 不可。
