# 外网登录闸门（gate）分析

> 探索任务 n4 产物。聚焦 `backend/app/gate.py` 及其调用方，覆盖：免闸判定链、issue_code 发码、verify-gate 验码、app_settings 配置、IP 取址、manager 企微通道、安全边界、实测验证与疑点。
> 姊妹篇：`docs/login-explore/02-login-gate.md`（登录整体探索的 gate 章节，描述性）；本文偏模块级静态分析与边界验证，可交叉引用。

## 0. 一句话结论

外网登录两步闸门 = 浏览器 + 外网 IP + 非 admin → 第一步只回 `pre_token`，6 位码经站内+企微双通道发 manager 角色池，第二步凭 `pre_token + code` 换 token。免闸四优先级：admin 角色 → 桌面端（`X-PMS-Client` 头，device_gate 打开后还要求设备在名单）→ 内网 IP（私网段恒免）→ `gate_enabled=0`。核心文件 `gate.py` 全文 177 行。

## 1. 架构总览与数据流

```
POST /api/auth/login ──┐
                       ├─ 1. 验密 + is_active（auth_router.py:77-80）
                       ├─ 2. 更新 last_login（auth_router.py:82-84）
                       ├─ 3. 闸门判定（auth_router.py:86-108）:
                       │      admin 角色(has_role"admin") → 直接放行(:92)
                       │      get_gate_config 实时读库(:93)
                       │      exempt = is_intranet(ip,cidrs) 或 desktop_exempt(...)(:94-97)
                       │      if enabled and not exempt → issue_code(:98-100)
                       │          └─ 命中: GateRequiredOut{gate_required,pre_token}(:106-108)
                       │          └─ 429/异常: write_audit login_gate_fail(:101-104)
                       └─ 未命中 → _issue_token 直接发 token(:109)

POST /api/auth/login/verify-gate ── GateVerifyIn{username,pre_token,code,remember}
       ├─ 找用户 + is_active（auth_router.py:116-119，不验密码）
       ├─ gate.verify_code（auth_router.py:122；异常→audit login_gate_fail :124-126）
       └─ 通过 → last_login + _issue_token(:127-130)
```

数据表：`LoginGateCode`（models.py:1193-1205）+ `AppSetting`（models.py:1145）+ `Message`（models.py:509，站内通道）+ 企微外发（notify.py）。

## 2. 判定链：login 第一步（auth_router.py:73-109）

按代码执行顺序的免闸优先级：

1. **账号是否 admin**：`if not u.has_role("admin")`（auth_router.py:92）——admin **根本不进**闸门判定，是硬免闸。注意：`has_role("manager")` 不免闸（manager 恰是"审核验证码"的角色，设计如此）。
2. **桌面客户端**：`is_desktop = x-pms-client 头 startswith("desktop/")`（auth_router.py:90）；`desktop_exempt`（gate.py:67-84）：
   - `device_gate` 关（默认）→ 只要带 `X-PMS-Client` 头就免闸；
   - `device_gate` 开 → 还要求 `X-PMS-Device` 落在 `device_ids` 名单（gate.py:84）。
3. **内网 IP**：`is_intranet(ip, cidrs)`（gate.py:54-64）——回环/私网地址（`ipaddress.is_loopback/is_private`，即 127/8、10/8、172.16/12、192.168/16、::1、fc00::/7）**恒判内网**（注释：不可公网路由，天然不可能是外网来源）；名单 `cidrs` 用于覆盖办公网公网出口。名单匹配 `_ip_in`（gate.py:38-51）：单 IP 按 /32、CIDR 按网段、非法条目 `continue` 跳过。
4. **开关**：`cfg["enabled"] and not exempt` 才拦（auth_router.py:98）。`gate_enabled` **默认 "1" 开**（gate.py:102，`get_gate_config` docstring 也写明"gate_enabled 默认开"）——未配置的存量环境，外网浏览器登录就会触发验证码，内网/桌面不受影响。

命中闸门 → `issue_code` → 回 `GateRequiredOut{gate_required:True, pre_token, message:"已通知管理层，请联系管理层获取验证码"}`（auth_router.py:106-108）。`GateRequiredOut` 定义见 schemas.py:714-718。

**关键契约**：`_issue_token`（auth_router.py:65-70）对"免闸登录"与"verify-gate 验码登录"两路共用，`remember` 延长的只是令牌有效期到 30 天（`REMEMBER_MINUTES = 30*24*60`，auth_router.py:62），密码不落客户端。

## 3. IP 取址链（auth_router.py:48-56）

```
_client_ip(request):
  1. X-Real-IP 头优先（rip 非空即用）      ← nginx $remote_addr 覆写，外部不可伪造
  2. 次取 X-Forwarded-For 的【末段】        ← nginx $proxy_add_x_forwarded_for 把真实地址追加到链尾；
                                              取首段会被客户端伪装的 XFF 骗过
  3. 兜底 request.client.host（直连）
```

nginx 覆写证据：`nginx/conf.d/_shared-locations.inc:33-35` 等每个 location 块均含
`proxy_set_header X-Real-IP $remote_addr;` + `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`。

**部署前提（安全边界）**：X-Real-IP 可信的前提是"所有公网流量必经 nginx"。若有人绕过 nginx 直连 uvicorn 端口，`X-Real-IP: 127.0.0.1` 可伪造 → `is_loopback` 恒免闸。这是依赖拓扑的隐含前提，代码层无任何反绕过措施。

## 4. issue_code 发码流程（gate.py:129-158）

```
now = datetime.now(timezone.utc)
① 限频：count(LoginGateCode where user_id AND created_at >= now-1min)  （:133-135）
     ≥ 1 → HTTPException 429 "验证码发送过于频繁，请 1 分钟后再试"（:136-137）
② 作废旧未用码：UPDATE used=True where user_id AND used=False（:140-143，同账号同时只有一个有效码）
③ code  = f"{secrets.randbelow(1000000):06d}"     6 位数字（:145）
   pre_token = secrets.token_urlsafe(24)           192 bit 随机（:146）
   db.add(LoginGateCode{
     user_id, code_hash=sha256(code).hexdigest(),  ← 库中只存哈希（models.py:1199）
     pre_token, expires_at=now+10min               CODE_TTL_MIN=10（gate.py:20）
   })（:147-152）
④ push_message(to_role="manager", kind="warn", text=含明文 code)（:153-157）
     —— 注释明言"push_message 自带 commit，上面的码行随之落库"
⑤ return pre_token
```

常量：`CODE_TTL_MIN = 10`（gate.py:20）、`_RATE_PER_MIN = 1`（gate.py:21，注明"2026-07-28 应要求去掉每日上限与错码锁定"）。

**明文码去向**：push_message 文本同时写 `Message` 表（站内，落库）与企微应用消息（notify.py:71-84），两处都含 6 位明文码（gate.py:155-156）。`LoginGateCode` 表里只有 sha256。

## 5. verify-gate 验码流程（gate.py:161-177 + auth_router.py:112-130）

```
① 按 username 找用户 + is_active（auth_router.py:116-119）——【不验密码】
② verify_code：
   找行：user_id + pre_token + used=False（:165-169）
   行不存在 或 _aware(expires_at) < _now() → 400 "验证码无效或已过期"（:170-171）
   哈希不符 → fail_count+=1 + commit + 400 "验证码错误"（:172-175）
   成功   → used=True + commit（:176-177）
③ 通过 → 更新 last_login + _issue_token 发 token（auth_router.py:127-130）
```

要点：
- **验码不重查闸门**：只要码没过期（10 分钟）就发 token，不管此时 IP/开关状态。发码后 10 分钟内管理员关闸或加白名单，已发出的老码仍有效到过期。设计合理（发码→验码 IP 可漂移）。
- **无密码、无限频、无锁定**：攻击前提是拿到 `pre_token`（登录响应的 `GateRequiredOut` 里），之后可穷举 6 位码（1e6 组合）。码 10 分钟失效 + 同账号 1 条/分限频是主要防线。
- **双因素语义**：`pre_token`（登录第一步 HTTP 响应）与 `code`（manager 企微/站内通道）分离，攻击者需同时拿到两样。
- 错误码只 `fail_count += 1` 并 commit，**不再锁定**（2026-07-28 起）。`fail_count` 只增不清零（验码成功不重置，gate.py:176-177）。

## 6. 配置与存储

- 表：`AppSetting(key PK, value, updated_at)`（models.py:1145-1153，`DateTime(timezone=True)`）。
- key 常量：`gate_enabled` / `intranet_cidrs` / `device_gate_enabled` / `device_ids`（gate.py:23-26）。
- 读 `get_gate_config`（gate.py:96-107）：**每次请求实时读库**，无缓存。默认值：`enabled=("1"!="0")=True`、`cidrs=[]`、`device_gate=("0"=="1")=False`、`device_ids=[]`。脏数据兜底：`_json_list`（gate.py:87-93）解析失败或非数组一律当空名单，"绝不因脏数据把人挡在外面"。
- 写 `set_gate_config`（gate.py:110-126）：四 key 一起 upsert + commit，保存即全局生效。
- 配置权限：`GET /api/admin/gate-config`（admin_router.py:425-427）与 `PUT /api/admin/gate-config`（admin_router.py:434-437）均 `require_admin_or_manager`（deps.py:37-41，admin **或** manager）。
- schema：`GateConfigIn/GateConfigOut`（schemas.py:728-740）。
- 前端：管理 →「外网访问」`frontend/src/views/admin/GateConfigView.vue`；`device_gate` 打开时 `ElMessageBox` 二次确认（GateConfigView.vue:39-45）。设备台账页 `DesktopClientsPage.vue`（device_id 从 `desktop_clients` 表展示）。
- **seed 不预置**这 4 个 key（`backend/app/seed.py` 无 `gate_enabled` 命中）——全新环境 `get_gate_config` 直接走默认值（闸开、名单空、设备闸关）。

## 7. manager 扇出与企微双通道

`push_message`（notify.py:29-84）：
- 角色扇出（notify.py:44-59）：按 `Role.code=manager` 取 id → `is_active` 用户（**锚点 `role_id` 命中 或 `user_roles` 关联任一命中**，notify.py:45-58）→ 每用户一行 `Message`。manager 为副角色的用户也能收到。
- 站内通道：`db.add_all + commit`（notify.py:71-76），码随消息落 `Message` 表。
- 企微通道（notify.py:79-84 + 105-138）：`settings.wecom_corp_id/wecom_secret` 齐才尝试；取 token（进程级缓存 2h，notify.py:87-102）→ `message/send`（touser 拼接 wxid，≤1000 人）→ 失败仅 `log.warning("企微推送失败（站内消息已落）")`，**绝不阻塞主事务**（F3 口径，notify.py:7）。
- **边界**：若 manager 角色无 active 用户，`push_message` 提前 return 0 且不 commit（notify.py:60-62）→ 此时 `issue_code` 里 add 的码行**悬而未提交**。能落库靠的是调用方后续 `write_audit`（utils.py 内 commit）的连带副作用——login 端点 issue_code 后必有 `write_audit(action="login_gate_issue")`（auth_router.py:105）。这是隐式契约：**码行落库实际依赖 write_audit 的 commit，而非 push_message 的 commit**（push_message 无用户时确实不 commit）。

## 8. 安全边界分析（真实防线 vs 可绕过点）

| 层 | 真实防线 | 可绕过点 / 前提 |
|---|---|---|
| 桌面免闸 | `device_gate` 打开 + `device_ids` 设备名单（需知道在册设备 UUID） | **`X-PMS-Client` 头本身可伪造**——`curl -H "X-PMS-Client: desktop/x"` 即免闸；device_gate 默认关，代码注释自认（gate.py:74-76） |
| 内网判定 | 私网段恒免（物理上不可公网路由）；`intranet_cidrs` 名单 | 办公网若走公网出口且未配 CIDR 名单 → 外网来源被当内网（AGENTS.md 已知"内网名单待用户配置"） |
| admin 免闸 | 角色硬编码（auth_router.py:92） | admin 密码泄露则闸门对 admin 无效 |
| IP 取址 | nginx `$remote_addr` 覆写 X-Real-IP（外部不可伪造） | 绕过 nginx 直连后端端口时 X-Real-IP 可伪造（依赖部署拓扑，代码无反制） |
| 双因素 | pre_token（登录响应）+ code（manager 通道）两要素分离 | 需同时拿到两样 |
| 码强度 | 6 位码 + sha256 存储 + 10 分钟 TTL + 1 条/分限频 | 1e6 组合理论可暴力，但需 pre_token 且 10 分钟窗口；无锁定，只有 fail_count 记录 |
| 登录响应 | 命中闸门不发 token 只回 pre_token | pre_token 192bit，无泄漏面 |

## 9. 实测验证与疑点

### 9.1 SQLite 下限频/过期 SQL 比较（实测通过）
担心：`LoginGateCode.created_at` 在 SQLite 下存 naive（CURRENT_TIMESTAMP，`_aware` 注释 gate.py:33-35 也确认"SQLite 读回 naive"），而限频查询绑定 aware UTC 参数（`_now()`，gate.py:29-30），字符串比较可能有前缀陷阱。
实测（临时库 + aiosqlite，插入一行后查询 1 分钟窗口）：

```
rate-limit count(1min): 1     ← 命中
created_at repr: datetime.datetime(2026, 8, 9, 10, 7, 54)   # naive，无微秒
expires_at repr: datetime.datetime(2026, 8, 9, 10, 17, 54, tzinfo=datetime.timezone.utc)
```

结论：SQLite 下限频**正常命中**。`expires_at` 保留 aware，`created_at` 为 naive——限频/过期判断在 SQLite 下可用；生产 PostgreSQL `DateTime(timezone=True)` 是真 timestamptz（models.py:1202），比较更无问题。同秒边界理论上有字典序前缀瑕疵，实际影响可忽略（窗口 60s）。

### 9.2 疑点
- **`fail_count` 只增不清零**：验码成功不重置（gate.py:176-177），失败计数只用于观察/审计，无任何分支消费它（`verify_code` 内不再读 fail_count）。models.py:1204 注释还写着"连续错码次数（>=5 锁定）"，**与行为不符（已去掉锁定）**——注释过时点一。
- **`AppSetting` docstring 过时**：models.py:1146 写"仅 Agent 助手用来存 LLM 配置"，但 gate 配置也存此表（gate.py:98-99、110-126）。注释过时点二。
- **码明文滞留 Message 表**：6 位码写进 `Message` 文本（gate.py:155-156），到期后无清理逻辑（未查见 `login_gate` 消息清扫任务，**待核实**），长期积存在库。
- **verify-gate 无限频/锁定**：对 pre_token 的穷举无速率限制（仅发码侧有限频），依赖 TTL + 双因素缓解。
- **明文码在登录失败时**：`issue_code` 抛 429 前**无**码行产生（限频在 add 之前，gate.py:133→147）；正常发码后若 manager 通道故障，用户拿不到码但 pre_token 已下发，10 分钟内重登可再触发 issue_code（会再次限频拦截）。

## 10. 反例（排除的猜想）

- ~~"SQLite 下限频永远失效（naive vs aware 前缀比较导致 count 恒 0）"~~ → 实测 `count(1min)=1` 命中，不成立。详情见 9.1。
- ~~"码行落库依赖 push_message 的 commit，manager 无用户时会丢码行"~~ → 部分成立但被兜住：push_message 无用户确实不 commit（notify.py:60-62），但调用方 login 端点 issue_code 后必有 `write_audit`（auth_router.py:105）连带提交码行，实际不丢。
- ~~"manager 不免闸是漏洞"~~ → 不是漏洞：manager 是验证码的**审核接收方**（gate.py:1-2 docstring），免闸反而破坏模型。

## 11. 调用方清单

| 调用点 | 位置 | 说明 |
|---|---|---|
| POST `/api/auth/login` | auth_router.py:73-109 | 闸门判定 + 命中发码 |
| POST `/api/auth/login/verify-gate` | auth_router.py:112-130 | 验码发 token |
| GET `/api/admin/gate-config` | admin_router.py:425-427 | 读配置（admin/manager） |
| PUT `/api/admin/gate-config` | admin_router.py:434-437 | 写配置（admin/manager） |
| 统计中间件（读 X-PMS-* 头） | main.py:41-55, 133-153 | 60s 节流 upsert `desktop_clients`（**与闸门判定独立**，只做统计） |
| 前端「外网访问」配置页 | frontend/src/views/admin/GateConfigView.vue | device_gate 二次确认（:39-45） |
| 前端设备台账页 | frontend/src/views/admin/DesktopClientsPage.vue | 设备 ID 列表（名单录入参考） |

## 12. 测试覆盖

- `backend/tests/test_login_gate.py`（222 行，独立 asyncio 脚本非 pytest）：admin 免闸 / 外网命中验证码 / 错码 400 / 过期 400 / 限频 429 / 配置读写 / verify-gate 发 token。
- `backend/tests/test_gate_device_ids.py`（160 行）：device_gate 开关与设备名单行为。
- 运行方式：`cd backend && .venv/bin/python tests/<name>.py`（AGENTS.md；系统 python3 缺 aiosqlite 不能跑）。

## 13. 待核实 / 遗留

- `Message` 表里含验证码明文的站内消息有无清理任务（未查见，疑点见 9.2）。
- 生产 `app_settings` 实际值（`gate_enabled`/`intranet_cidrs`/`device_gate_enabled`/`device_ids`）未在本地可查，属服务器运行态，不在代码库。
- `docs/login-explore/02-login-gate.md` 与本文件若有出入，以代码为准（本文件所有行号均已核对）。
