# n15 核实报告：改密后 JWT 不吊销

**说法**：改密(POST /api/auth/change-password)不吊销已签发 JWT：token 无版本号/无黑名单，改密后旧 token 在 exp 前仍可通过 get_current_user(仅 is_active=False 才拦截)

**判决**：PASS（成立）

---

## 核实过程

### 1. change_password 端点（auth_router.py:150-164）

打开文件逐行确认：

- **151-154**：接收 ChangePasswordIn + get_current_user 依赖注入
- **156-157**：验旧密码是否正确
- **158-159**：新旧密码相同则拒绝
- **160**：`current.password_hash = hash_password(data.new_password)` — 只换哈希
- **161**：设置 `password_must_change = False`
- **162**：`await db.commit()`
- **163**：写审计日志

**确认：没有以下任一吊销手段**：
- 没有写入 token 黑名单表
- 没有递增用户 token_version 字段
- 没有删除或标记已签发 session
- 没有任何使旧 token 失效的代码

### 2. get_current_user（deps.py:11-28）

- **15-16**：提取 Authorization header → Bearer token
- **18**：`decode_token(token)` 解码 JWT
- **21-23**：提取 payload.sub（user_id）
- **24-25**：`select(User).where(User.id == int(user_id))` — 按 ID 查用户
- **26-27**：`if not user or not user.is_active: raise 401` — **只检查 is_active**

**确认：不检查**：
- token 版本号（JWT payload 里根本没有）
- 黑名单
- 密码最近修改时间
- 任何与改密相关的状态

### 3. JWT 生成（auth.py:26-36）

- payload 结构：`{"sub": str(subject), "exp": expire}`
- `extra` 参数可为空扩展，但登录时不传版本号/密码哈希等字段

**确认：JWT 无法事后失效** — payload 只含 sub+exp，没有可吊销锚点。

### 4. 全仓搜索 token_version / token_blacklist / revoke

```
grep pattern: token_version|token_blacklist|jwt.*version|revoke|blacklist
```

结果：7 条匹配，`revoke` 全部指向 `invoice_revoke`（发票撤销），与 token 无关。
无 token_version、无 token_blacklist、无 JWT 吊销机制。

---

## 结论

**说法完全成立**。该系统改密后旧 token 在过期前（默认 8 小时）仍可通过 `get_current_user` 验证通过，唯一的拦截条件是被手动设置 `is_active=False`。

### 证据清单

| # | 证据 | 内容 |
|---|------|------|
| 1 | `auth_router.py:150-164` | change_password 只换 hash，无任何 token 吊销 |
| 2 | `deps.py:26-27` | get_current_user 仅检查 is_active，不查版本号/黑名单 |
| 3 | `auth.py:26-36` | JWT payload 只有 sub+exp，无可吊销锚点 |
| 4 | 全仓 grep | token_version/token_blacklist 无任何匹配 |

### 实际影响

- 用户改密后，旧 JWT 在过期时间（默认 8h）内继续有效
- 要强制让某用户所有 session 失效，目前只能设 `is_active=False`（相当于禁用账号）
- 桌面客户端"记住我"模式 token 有效期 30 天，影响更大
