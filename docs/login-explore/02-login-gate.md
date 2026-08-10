# 外网登录闸门（两步验证码）探索报告

> 探索日期：2026-08-09　范围：后端 `gate.py` + `auth_router.py` + 相关配置/通知/模型
> 相关文件：`backend/app/gate.py`(177 行)、`backend/app/routers/auth_router.py`、`backend/app/notify.py`、`backend/app/models.py:1193`、`backend/app/routers/admin_router.py:424`、`nginx/conf.d/default.conf:1`

## 1. 一句话总览

浏览器 + 外网 IP + 非 admin 登录时，`/api/auth/login` 不发 token，先经 `push_message` 把 6 位随机码发给 **manager 角色**（站内+企微），管理层核实后告知本人，再用 `/api/auth/login/verify-gate` 验码换 token。免闸四类：admin 角色 / 桌面客户端（`X-PMS-Client` 头）/ 内网 IP / 开关关闭。

## 2. 免闸顺序与判定（auth_router.py:86-109）

`login` 验密码、验 active、更新 last_login 后：

```python
ip = _client_ip(request)
is_desktop = request.headers.get("x-pms-client", "").startswith("desktop/")
device_id = (request.headers.get("x-pms-device") or "").strip()
if not u.has_role("admin"):                       # ← admin 恒免闸，完全跳过判定
    cfg = await gate.get_gate_config(db)
    exempt = (gate.is_intranet(ip, cfg["cidrs"])
              or gate.desktop_exempt(is_desktop, device_id,
                                     device_gate=cfg["device_gate"],
                                     device_ids=cfg["device_ids"]))
    if cfg["enabled"] and not exempt:
        pre_token = await gate.issue_code(db, u)   # 命中闸门 → 发码，返回 GateRequiredOut
        ...
return await _issue_token(db, u, ...)             # 免闸 → 直接发 token
```

要点：
- **admin 判定在一切之前**：`if not u.has_role("admin")` 才进入闸门分支，admin 连配置都不读（auth_router.py:92）。`has_role` 是角色并集判断（models.py:99-101），`role_codes` 含 finance_lead⊇finance 隐含。
- `is_desktop` 判 `x-pms-client` 头前缀 `desktop/`（auth_router.py:90）；桌面端免闸细节见 §4.4。
- **配置实时读库**：`get_gate_config` 每次登录请求都查 `app_settings`，改配置立即全局生效，无需重启（gate.py:96-107）。

## 3. 客户端真实 IP 判定

### 3.1 `_client_ip`（auth_router.py:48-56）

```python
rip = (request.headers.get("x-real-ip") or "").strip()
if rip: return rip
parts = [p.strip() for p in request.headers.get("x-forwarded-for","").split(",") if p.strip()]
return parts[-1] if parts else (request.client.host if request.client else "")
```

- **优先 `X-Real-IP`**：nginx `proxy_set_header X-Real-IP $remote_addr` 用 TCP 连接真实地址**覆写**，客户端伪造的 X-Real-IP 会被覆盖，无法伪造（nginx/conf.d/default.conf:37）。
- **次取 `X-Forwarded-For` 末段**：nginx `$proxy_add_x_forwarded_for` 把真实地址**追加**到链尾；取**末段**是因为首段可被客户端伪造成 `X-Forwarded-For: 假IP`（auth_router.py:49-51 注释明说）。
- 兜底 `request.client.host`（uvicorn 直连场景）。

### 3.2 `is_intranet`（gate.py:54-64）

```python
if addr.is_loopback or addr.is_private: return True   # 恒内网
return _ip_in(ip, cidrs)                               # 名单覆盖办公网公网出口等
```

- **回环 + 私网恒判内网**：127/8、10/8、172.16/12、192.168/16、::1、fc00::/7（`ipaddress` 的 `is_loopback`/`is_private` 判定）。这些段不可公网路由，天然不可能是外网来源，故本地开发/内网部署不受闸门影响。
- **`intranet_cidrs` 名单**：用于办公网走公网出口等场景；单 IP 按 /32、CIDR 按网段匹配，非法条目静默跳过（`_ip_in`，gate.py:38-51）。纯名单匹配，**不含私网自动放行逻辑**（私网放行由 `is_intranet` 里 `is_private` 负责）。
- 非法 IP 字符串 → `ipaddress.ip_address` 抛 ValueError → 返回 False（按外网处理，走闸门）。

## 4. 免闸四路径

| 路径 | 条件 | 代码位置 |
|---|---|---|
| admin 角色 | `has_role("admin")` | auth_router.py:92 |
| 桌面客户端 | `X-PMS-Client: desktop/...` 头，且 `device_gate` 关（默认）或 `device_id` 在名单 | auth_router.py:90, gate.py:67-84 |
| 内网 IP | 回环/私网地址 或命中 `intranet_cidrs` | gate.py:54-64 |
| 开关关闭 | `gate_enabled=0` | auth_router.py:98 |

### 4.1 桌面客户端免闸与设备闸（gate.py:67-84）

```python
if not is_desktop: return False
if not device_gate: return True            # 默认：装了客户端就免闸
return bool(device_id) and device_id in set(device_ids)
```

- 只认 `X-PMS-Client` 头**可伪造**（curl 加个头就绕过整道闸门）；`device_gate` 打开后还要 `X-PMS-Device` 落在名单里，伪造者还得知道某台在册机器 UUID。
- **开关开而名单空 = 所有桌面端都要验证码**——刻意字面语义（gate.py:77-79）；admin 恒免闸兜底，不会锁死。
- 桌面端统计头由 `main.py` 中间件节流 upsert `desktop_clients` 表（main.py:139-157），与闸门判定无关。

## 5. `issue_code` 发码流程（gate.py:129-158）

1. **限频**：同账号最近 1 分钟内已发过 → `429 "验证码发送过于频繁，请 1 分钟后再试"`。`_RATE_PER_MIN=1`（gate.py:21）。
   - 🆕 2026-07-28 应要求**去掉每日上限与错 5 次锁定**，仅保留 1 条/分钟（gate.py:21 注释）。
2. **作废旧码**：同一账号所有 `used=False` 的行置 `used=True`——同一账号同时只有 1 个有效码，重发即作废（gate.py:139-143）。
3. **生成**：`code = f"{secrets.randbelow(1000000):06d}"`（6 位数字，含前导零）；`pre_token = secrets.token_urlsafe(24)`（登录会话临时凭证）。
4. **存储**：`LoginGateCode` 行，**库中只存 `code_hash = sha256(code)`**，不存明文（gate.py:149, models.py:1199）。`expires_at = now + 10 分钟`（`CODE_TTL_MIN=10`）。
5. **通知**：`push_message(db, to_role="manager", kind="warn", text=...)`，文本含 `用户全名(username) + 明文验证码 + 10分钟有效`（gate.py:153-157）。
   - 注释明确：**push_message 自带 commit，码行随之同事务落库**（gate.py:157）。
6. **返回** `pre_token` 给客户端（auth_router.py:106-108 组装 `GateRequiredOut`），并写审计 `login_gate_issue`。

异常路径：限频 429 时 auth_router 写审计 `login_gate_fail` 后再抛（auth_router.py:101-104）。

### 5.1 manager 角色扇出与企微外发（notify.py:29-84）

- `to_role="manager"`：`notify.push_message` 按角色取 role_id（`Role.code in ['manager']`），扇出给锚点 role_id 命中**或** user_roles 关联命中的所有 active 用户，每人一行 `Message`（notify.py:44-59）。站内消息与码行同一事务 commit。
- 企微外发：用户绑了 `wxid` 且 `wecom_corp_id/secret/agent_id` 配置齐 → `_send_wecom` 用 access_token 调 `message/send`（notify.py:79-84, 105-120）；**失败仅 `log.warning` 绝不阻塞**（"站内消息已落"）。
- 角色池无人 → `log.info` 丢弃并返回 0（notify.py:60-62）。——若系统无 manager 角色用户，码无人接收，外网用户会卡死在第二步（seed 默认建 `manager/admin123` 账号，故正常运行不受影响）。

## 6. `verify-gate` 验码换 token（auth_router.py:112-130 + gate.py:161-177）

- 输入：`username + pre_token + code + remember`（`GateVerifyIn`，schemas.py:721-726）。
- **只查 username + is_active，不验密码**——第一步已验过密码，`pre_token` 就是会话凭证（auth_router.py:116-119）。
- `verify_code` 校验：
  1. 按 `user_id + pre_token + used=False` 找行；不存在 / 已用 / 过期 → `400 "验证码无效或已过期，请重新获取"`（gate.py:165-171）。
  2. `sha256(code.strip())` 与 `code_hash` 不符 → **`fail_count += 1` 并 commit，然后 400 "验证码错误"**；不再锁定（gate.py:172-175）。
  3. 命中 → `used=True` + commit（gate.py:176-177）。
- 验码失败时 auth_router 写审计 `login_gate_fail` 后抛（auth_router.py:123-126）。
- 成功后更新 last_login → `_issue_token` 发 token + 写审计 `login`（auth_router.py:127-130）。

## 7. 数据模型

### 7.1 `LoginGateCode`（models.py:1193-1204，表 `login_gate_codes`）

| 列 | 类型 | 说明 |
|---|---|---|
| id | PK | |
| user_id | FK users.id, index | 登录人 |
| code_hash | String(64) | `sha256(6 位数字码)`，**不存明文** |
| pre_token | String(64), index | 登录第一步下发的会话临时凭证 |
| created_at | DateTime, server_default now | |
| expires_at | DateTime | 10 分钟有效 |
| used | Boolean default False | 已用/已作废（重发即作废旧码） |
| fail_count | Int default 0 | 连续错码次数（**注释写 ">=5 锁定" 已过时**，2026-07-28 起只计数不锁定） |

表由 `Base.metadata.create_all` 启动自动建，无迁移脚本（models.py:1195）。

### 7.2 配置存储（`app_settings` 表，models.py:1145-1153）

`AppSetting` = key-value 两列（key 为主键），gate 用 4 个 key（gate.py:23-26）：

| key | 默认 | 含义 |
|---|---|---|
| `gate_enabled` | `"1"` | 总开关，`!= "0"` 即开 |
| `intranet_cidrs` | `[]` | 内网名单，JSON 数组 |
| `device_gate_enabled` | `"0"` | 设备闸开关，默认关 |
| `device_ids` | `[]` | 在册设备 UUID 名单，JSON 数组 |

- `get_gate_config` 实时读库（gate.py:96-107）；`set_gate_config` upsert 写回（gate.py:110-126）。
- 脏数据安全：JSON 解析失败一律当空名单，"绝不因脏数据把人挡在外面"（gate.py:87-93）。

### 7.3 时间处理

- 业务时间统一 UTC：`_now() = datetime.now(timezone.utc)`（gate.py:29-30）。
- SQLite 读回 naive 时间 → `_aware` 补 `tzinfo=UTC` 再比较过期（gate.py:33-35）。

## 8. 管理端点与审计（admin_router.py:424-453）

- `GET /api/admin/gate-config` → `GateConfigOut`，`require_admin_or_manager`（admin **或** manager 可看/改）。
- `PUT /api/admin/gate-config` 接受 `GateConfigIn`：`enabled / cidrs / device_gate / device_ids`；服务端做 cidrs 去空 strip、device_ids 去重保序后写库，并写审计 `set_gate_config`（detail 记开关值与名单长度，**不记名单内容**）。
- 菜单入口：管理组「外网访问」`gate-config`（menus.py:51）。
- 前端页面在「管理→外网访问」，保存时对打开设备闸有二次确认（gate.py:78 注释）。

## 9. 外部防线：nginx 登录限频（nginx/conf.d/default.conf:1-3, 42-50）

- `limit_req_zone $binary_remote_addr zone=login_limit:10m rate=10r/m`——**每 IP 登录接口 10 次/分钟**，burst=5 nodelay，超限 429（`limit_req_status 429`）。
- 只作用于 `location = /api/auth/login`（**不含** `/api/auth/login/verify-gate`，验码接口无限频，由 §5 的应用层限频兜底）。
- 与闸门的关系：闸门防「越权登录」，nginx 限频防「密码爆破」，互补。

## 10. 审计事件清单（闸门相关）

| action | 触发点 |
|---|---|
| `login_gate_issue` | 命中闸门、发码成功（auth_router.py:105） |
| `login_gate_fail` | 发码限频 429 / 验码失败（auth_router.py:102, 124） |
| `login` | 免闸直接登录 / 验码成功后登录（`_issue_token` 内，auth_router.py:69） |
| `set_gate_config` | 管理端改配置（admin_router.py:450） |

## 11. 反例与排除的猜想

- **"verify-gate 不校验闸门命中状态是漏洞"** → 不成立。`verify-gate` 只凭 `username+pre_token+code` 换 token，确实不重查免闸条件；但 `pre_token` 只在 login 命中闸门时下发一次、单账号单有效码、码仅经 push_message 发 manager——这是设计使然而非遗漏。
- **"X-Real-IP 可以被客户端伪造绕过内网判定"** → 不成立。nginx 用 `$remote_addr` 覆写，伪造头到不了后端。
- **"XFF 应取首段"** → 反例：nginx `$proxy_add_x_forwarded_for` 把真实地址追加到链尾，取首段会被 `X-Forwarded-For: 假IP` 骗过，故实现取末段（auth_router.py:49-51）。
- **"LoginGateCode.fail_count >=5 锁定"** → 已排除。注释如此但 2026-07-28 起代码只计数不锁定（gate.py:162 注释），models.py:1204 注释残留过时。

## 12. 相关测试

- `backend/tests/test_login_gate.py`：免闸/命中闸门/限频/验码/配置读写/权限（非 admin 不可读 gate-config）等。
- `backend/tests/test_gate_device_ids.py`：设备闸（device_gate + device_ids）行为，含"开关开名单空=全验证"。

## 13. 时间线

- 闸门初版上线（AGENTS.md 记「外网登录闸门已上线」）；2026-07-28 应用户要求去掉每日 10 条上限与错 5 次锁定，仅保留 1 条/分钟间隔（gate.py:21, 162 注释）。
- 设备闸（device_gate/device_ids）为后续增量，默认关，存量环境行为不变（gate.py:104）。
