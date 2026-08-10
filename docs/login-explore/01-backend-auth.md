# 后端认证主链路（登录 / token / 当前用户 / 登出）

> 探索日期：2026-08-09。边界：不涉及 `backend/app/gate.py` 外网闸门内部机制（另片），仅记录 login 端点与 gate 的交互关系。所有结论附 `文件:行号` 证据。

## 1. 涉及文件清单

| 文件 | 职责 |
|---|---|
| `backend/app/routers/auth_router.py` | `/api/auth` 全部端点：login / login/verify-gate / me / menus / change-password / logout |
| `backend/app/auth.py` | 密码 bcrypt 哈希+校验、JWT 签发/解码 |
| `backend/app/deps.py` | `get_current_user` 依赖与各级权限依赖（401/403 语义在此统一） |
| `backend/app/config.py:28-30,33-34` | `secret_key` / `access_token_expire_minutes`(=480 分钟) / `algorithm`(HS256) / 默认管理员账号密码 |
| `backend/app/seed.py:45-103` | 启动时种角色 + admin/manager 两个默认账号 |
| `backend/app/models.py:18-101` | `Role` / `UserRole` / `User` 表与 `has_role()` 并集语义 |
| `backend/app/models.py:538-550` | `AuditLog` 审计表 |
| `backend/app/utils.py:10-34` | `write_audit` 异步写审计（失败仅记日志不抛） |
| `backend/app/routers/ws_router.py:45-49` | WebSocket 用同一种 JWT 鉴权（`_verify_token`） |
| `backend/app/main.py:159` | `app.include_router(auth_router.router)` 注册 |

## 2. 登录接口

### 2.1 `POST /api/auth/login`（auth_router.py:73-109）

入参 `schemas.LoginIn`（schemas.py:700-704）：
```json
{ "username": "str", "password": "str", "remember": false }
```
`remember=true` 只延长令牌有效期到 30 天（`REMEMBER_MINUTES = 30*24*60`，auth_router.py:62），**密码不落客户端**（auth_router.py:59-61 注释明示）。

流程（auth_router.py:74-109）：
1. 按 `username` 查 `users` 表（select，auth_router.py:75）。
2. `verify_password` 校验，用户不存在或密码错误统一返回 **401 "用户名或密码错误"**（auth_router.py:77-78，不泄露账号是否存在）。
3. 账号 `is_active=False` → **403 "账号已停用"**（auth_router.py:79-80）。
4. 更新 `u.last_login` 并 commit（auth_router.py:82-83）。
5. 外网闸门判定：非 admin 角色时取 `X-PMS-Client` 头（桌面客户端）与 `X-PMS-Device` 头（设备闸），调 `gate.get_gate_config/is_intranet/desktop_exempt`；若闸门开且不豁免 → 返回 `GateRequiredOut`（不发 token，只回 `pre_token`，走两步验证码登录），并写审计 `login_gate_issue`（auth_router.py:92-108）。
6. 免闸路径 → `_issue_token` 直接发 token（auth_router.py:109）。

**客户端真实 IP**：优先 `X-Real-IP`（nginx `$remote_addr` 覆写不可伪造），次取 `X-Forwarded-For` **末段**（首段可被客户端伪造），兜底 `request.client.host`（auth_router.py:48-56）。

### 2.2 `POST /api/auth/login/verify-gate`（auth_router.py:112-130）

外网登录第二步：入参 `GateVerifyIn{username, pre_token, code, remember}`（schemas.py:721-725）。校验 6 位随机码，通过后同样更新 last_login 并 `_issue_token` 发 token。验证码相关机制（`gate.issue_code`/`verify_code`，gate.py:129,161）归外网闸门片。

### 2.3 token 签发（auth_router.py:65-70 `_issue_token`）

```python
token = create_access_token(u.id, minutes=REMEMBER_MINUTES if remember else None)
await write_audit(db, user=u, action="login", ip=ip or None)
return schemas.TokenOut(access_token=token, user=_user_to_out(u))
```
登录成功必写审计 `audit_logs.action='login'`（带 ip）。

## 3. Token 结构与有效期

`backend/app/auth.py:26-45`：

- 算法 HS256，密钥 `settings.secret_key`，payload = `{"sub": str(user_id), "exp": <utc 过期时间>}`，可选 `extra` 扩展（auth.py:33-35）。
- 默认有效期 `settings.access_token_expire_minutes` = **480 分钟（8 小时）**（config.py:29）。
- 「记住我」时 `minutes=REMEMBER_MINUTES` = **43200 分钟（30 天）**（auth_router.py:62, auth.py:31）。
- 无 refresh_token、无滑动续期机制：token 过期即需重新登录（H5 外网场景靠「记住我」延命）。`decode_token` 对任何 PyJWTError 均返回 None（auth.py:39-45）。
- **Token 不携带密码**，仅含 `sub`（用户 id）+ `exp`。

## 4. 当前用户依赖 `get_current_user`（deps.py:11-28）

- 从 `Authorization` 请求头取 `Bearer <token>`（大小写不敏感前缀，deps.py:15-17）。
- 解码失败 / 过期 → **401 "登录已过期"**（deps.py:19-20）；缺头 → **401 "未登录"**（deps.py:15-16）；无 `sub` → **401 "无效凭证"**（deps.py:22-23）。
- 按 `sub` 查库，用户不存在或 `is_active=False` → **401 "账号已禁用"**（deps.py:24-27）。注意：**这里禁用账号也是 401 而非 403**，与 login 端点停用返回 403 的语义不同。
- 返回完整 `models.User` ORM 对象，路由可直接用 `current.role_codes` / `has_role()`。
- 未做 token 黑名单/吊销校验：改密后旧 token 仍有效（JWT 无状态，仅登出依赖前端清 token）。

**401/403 语义汇总**（deps.py + auth_router.py）：

| 场景 | 状态码 | 消息 | 出处 |
|---|---|---|---|
| 缺 Authorization 头 | 401 | 未登录 | deps.py:15-16 |
| token 解码失败/过期 | 401 | 登录已过期 | deps.py:19-20 |
| payload 无 sub | 401 | 无效凭证 | deps.py:22-23 |
| 用户不存在 / 禁用 | 401 | 账号已禁用 | deps.py:24-27 |
| login 用户名或密码错 | 401 | 用户名或密码错误 | auth_router.py:77-78 |
| login 账号停用 | 403 | 账号已停用 | auth_router.py:79-80 |
| 角色不足（require_admin / require_roles 等） | 403 | 无权操作 等 | deps.py:31-41,52-62 |

## 5. 密码哈希与校验（auth.py:9-23）

- bcrypt：`hash_password` = `bcrypt.hashpw(plain, bcrypt.gensalt()).decode()`，产出 `$2b$...` 哈希串，存 `users.password_hash`（String(255)，models.py:45）。
- `verify_password` 用 `bcrypt.checkpw`，异常一律返回 False（auth.py:15-23）。
- 密码策略：新密码最小 6 位（`ChangePasswordIn.new_password` `Field(min_length=6, max_length=128)`，schemas.py:743-745）。

## 6. 其余端点

| 端点 | 说明 | 出处 |
|---|---|---|
| `GET /api/auth/me` | 返回当前用户 `UserOut`（`_user_to_out`） | auth_router.py:133-135 |
| `GET /api/auth/menus` | 当前用户一级菜单 key 清单 + `can_view_detail` | auth_router.py:138-147 |
| `POST /api/auth/change-password` | 验原密码 → bcrypt 存新密码 → `password_must_change=False` → 写审计 | auth_router.py:150-164 |
| `POST /api/auth/logout` | 仅返回 "已登出"，无服务端状态可清（JWT 无状态），靠客户端删 token | auth_router.py:167-170 |

`_user_to_out`（auth_router.py:17-45）：组装 `UserOut`，`role_codes` 用 `u.role_codes` property（含 `finance_lead ⊇ finance` 隐含，models.py:87-89）、`menus` 用账号配置菜单、`grant_menus` = menus ∩ 管理组 key（派生值，不再独立存储）。

## 7. 涉及的表与字段

**users**（models.py:39-101）：
- `username`(unique, index) / `password_hash` / `password_must_change`(bool，建号置 True 强制首登改密，admin_router.py:168) / `role_id`(FK roles，存量兼容锚点) / `is_active` / `wxid` / `can_export` / `hidden_tabs`(JSON) / `menus`(JSON，一级菜单账号配置) / `deputy_uid` / `created_at` / `last_login`(登录成功时写)。
- 权限判断统一走 `role_codes` property（`has_role()` 取并集），`role_id` 仅为兼容锚点（models.py:29-30, 78-101）。

**roles**（models.py:18-25）：`code`(unique) / `name` / `description` / `can_push`。
**user_roles**（models.py:31-36）：多对多关联表 `(user_id, role_id)`，唯一约束 `uq_user_role`。
**audit_logs**（models.py:538-550）：`user_id` / `username` / `action`(login 等) / `target_type` / `target_id` / `detail` / `ip` / `created_at`。

## 8. Seed 账号创建（seed.py:45-103）

- 启动时 `seed(db)` 先种 27 个角色（ROLES，seed.py:14-42，含 `can_push` 消息推送标记）。
- `users.username = settings.default_admin_username`（默认 `admin`）不存在则创建 `admin / admin123`，`password_must_change=True`（seed.py:62-83）。
- 同法创建 `manager / manager123`（管理层日常账号，seed.py:88-103）。
- 账号密码可经环境变量 `DEFAULT_ADMIN_USERNAME/DEFAULT_ADMIN_PASSWORD` 覆盖（config.py:33-34）。

## 9. 反例 / 排除路径

- **token 吊销/黑名单**：不存在。改密、禁用账号都不会使已签发 token 失效——禁用只在下次请求经 `get_current_user` 查库时生效（deps.py:26）。「登出」= 纯前端删 token（auth_router.py:169 注释）。
- **refresh token / 自动续期**：不存在。唯一延命手段是登录时带 `remember=true` 换 30 天 token。
- **session / cookie**：不存在，纯 Bearer token 无状态认证。
- **密码存储**：不是明文、不是简单哈希，是 bcrypt（每用户随机 salt），`verify_password` 异常吞掉防时序泄露（auth.py:22-23）。

## 10. WebSocket 复用同一 token

`ws_router.py:45-49` `_verify_token`：从 query 参数 `?token=` 取同一个 JWT 解码，仅验 `sub`，失败 close(1008)。不查库验 `is_active`（与 HTTP 的 get_current_user 不同——WebSocket 通道不校验账号禁用态）。

## 11. 与外部系统的衔接

- **前端**：登录拿 `TokenOut{access_token, user}`，axios 拦截器附 `Authorization: Bearer <token>`；桌面端统计头 `X-PMS-Client/X-PMS-Device/X-PMS-User` 在 main.py:139-157 中间件节流 upsert `desktop_clients` 表（仅统计，不影响认证）。
- **外网闸门**：login 端点 5 个 gate 调用点（auth_router.py:93-105），机制详情归外网闸门片。
- **审计**：`login` / `login_gate_issue` / `login_gate_fail` / `change_password` 均写 audit_logs，供登录审计追溯（auth_router.py:69,102,105,124,163）。

## 12. 一处值得注意的口径

- `get_current_user` 对**禁用账号返回 401**，而 `login` 对停用账号返回 **403**（deps.py:26 vs auth_router.py:80）。同是「账号停用」，两处状态码不一致；如需统一语义需改 deps.py:26（历史如此，未见 issue）。
