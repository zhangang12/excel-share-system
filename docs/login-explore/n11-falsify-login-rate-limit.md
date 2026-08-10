# n11 核实报告：登录端点无限频/失败计数/账号锁定

> 角色：核实员·证伪视角（n11）
> 核法：打开证据源的每一行亲眼看，不替说法圆场
> 判决：PASS —— 说法全部成立

## 被核说法

> POST /api/auth/login 端点本身无任何限频/失败计数/账号锁定，密码错与用户不存在统一返回 401 但可无限重试；防暴力破解仅靠外网两步验证码闸(gate)，且该闸对 admin 角色/内网 IP/桌面客户端免闸

## 逐条核证

### 1. 登录端点无任何限频/失败计数/账号锁定 → 成立

**证据（auth_router.py:73-109）：**

`/login` 函数只有以下逻辑：
1. 查用户（76 行）
2. `verify_password` 失败或用户不存在 → 统一 `HTTPException(401)`（77-78 行）
3. `is_active` 检查（79-80 行）
4. 更新 `last_login`（82-84 行）
5. gate 闸门判定（92-108 行）
6. `_issue_token` 签发（109 行）

全函数无 `failed_count` 累加、无 `limit`/`limiter` 装饰器、无 `lock out` 逻辑、无任何计数/限频代码。

**排除的其他可能性：**
- `backend/app/main.py` 的 HTTP 中间件（139-157 行）：仅有桌面客户端统计（`desktop_client_stats`），非限频，与登录无关
- 全仓 `grep 'rate.?limit|限频|failed_count|login_attempt|slowapi|throttle|brute|lock'` 零命中 auth_router.py
- `User` 模型（models.py:39-101）：无 `failed_count`、`locked_until`、`login_attempts` 等列。仅 `is_active: bool`（手动启停用，非自动锁定）

**被排除的误报：**
- `gate.py:21` `_RATE_PER_MIN=1`：限的是**验证码发码频率**（`issue_code`），不是登录尝试。码是登录成功后（密码已验证正确）才发的，攻击者撞不进来
- `main.py` `pg_advisory_lock`：数据库 schema 迁移的启动串行化锁，与登录无关

### 2. 密码错与用户不存在统一返回 401 → 成立

**证据（auth_router.py:77-78）：**
```python
if not u or not verify_password(data.password, u.password_hash):
    raise HTTPException(401, "用户名或密码错误")
```

用户不存在（`u is None`）和密码不匹配返回完全一致的 401 和错误消息，外部无法区分。

### 3. 闸对 admin/内网 IP/桌面客户端免闸 → 成立

**admin 角色免闸（auth_router.py:92）：**
```python
if not u.has_role("admin"):
    # ... gate logic
```
admin 用户**无条件跳过整个 gate 判定块**，连 IP 检查都不做——`is_intranet`/`desktop_exempt` 在 `not has_role("admin")` 的 if 体内，永远不会对 admin 求值。

**内网 IP 免闸（gate.py:54-64）：**
```python
def is_intranet(ip, cidrs):
    addr = ipaddress.ip_address(ip)
    if addr.is_loopback or addr.is_private:
        return True
    return _ip_in(ip, cidrs)
```
回环（127/8、::1）+ 私网（10/8、172.16/12、192.168/16、fc00::/7）恒免闸；额外 CIDR 通过 `intranet_cidrs` 配置覆盖办公网公网出口场景。

**桌面客户端免闸（gate.py:67-84、auth_router.py:94-97）：**
- `is_desktop` 由 `X-PMS-Client` 头判（以 `desktop/` 开头，auth_router.py:90）
- `device_gate` 关（默认 `"0"`，gate.py:105）→ 装了客户端就免闸
- `device_gate` 开 → 还要 `X-PMS-Device` 在 `device_ids` 名单里
- 注释明言 `X-PMS-Client` 头可伪造（gate.py:74）：curl 加个头就绕过

### 4. 闸本身的限频（补证：闸也不是强防线）

**发码限频（gate.py:129-158）：**
- `_RATE_PER_MIN = 1`：同账号每分钟最多发一条验证码
- 目的注释明说：**防误点刷屏**，非防暴力破解

**验码失败计数（gate.py:161-177）：**
- `verify_code` 有 `fail_count` 累加（173-174 行）
- 但 2026-07-28 已去掉「错 5 次锁定」逻辑（164 行注释）：
  > 应要求去掉「错 5 次锁定」：不再锁，错误仅计 fail_count
- `fail_count` 现在只是统计数据，不做任何自动限制

**结论：** 闸自身在 2026-07-28 降级后，发码仅有 1条/分限频（非阻断型），验码无锁定——若攻击者已通过密码验证（内网/桌面/admin 免闸直接跳过），闸完全不起作用；若 `gate_enabled=0`，所有来源免闸。

## 最终判决

**PASS** —— 四部分全部成立，且有代码直证。

| 子说法 | 判决 | 关键证据 |
|--------|------|----------|
| 登录端点无任何限频/失败计数/锁定 | 成立 | auth_router.py:73-109；User 模型无相关列 |
| 密码错/用户不存在统一 401 | 成立 | auth_router.py:77-78 |
| 仅靠 gate 防暴力破解 | 成立 | auth_router.py:86-108；gate 自身也降级了 |
| admin/内网/桌面免闸 | 成立 | auth_router.py:92；gate.py:54-64,67-84 |

## 分歧/遗留

- 无。四项均直读代码确认，未涉及推断。
