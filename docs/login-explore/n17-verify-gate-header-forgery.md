# n17 · 核实#1·证伪 外网验证码闸可被伪造

> 角色：核实员·证伪视角  
> 任务：核实"外网验证码闸可被伪造 X-PMS-Client 头绕过"这条说法是否成立  
> 日期：2026-08-09

## 判决

**PASS — 说法成立。** X-PMS-Client 头确实可被任何 HTTP 客户端伪造以绕过外网验证码闸，在 `device_gate` 默认关闭的前提下。

## 核实过程

### 1. 免闸判定链（auth_router.py:86-109）

登录流程中，密码验证通过后的闸门判定：

```python
# auth_router.py:89-97
ip = _client_ip(request)
is_desktop = request.headers.get("x-pms-client", "").startswith("desktop/")
device_id = (request.headers.get("x-pms-device") or "").strip()
if not u.has_role("admin"):
    cfg = await gate.get_gate_config(db)
    exempt = (gate.is_intranet(ip, cfg["cidrs"])
              or gate.desktop_exempt(is_desktop, device_id,
                                     device_gate=cfg["device_gate"],
                                     device_ids=cfg["device_ids"]))
    if cfg["enabled"] and not exempt:
        pre_token = await gate.issue_code(db, u)  # 走验证码
    ...
return await _issue_token(...)  # 直接发 token
```

关键判断（第90行）：`is_desktop` 仅检查请求头是否以 `"desktop/"` 开头，**无任何密码学验证**。

### 2. desktop_exempt 函数（gate.py:67-85）

```python
def desktop_exempt(is_desktop: bool, device_id: str, *,
                   device_gate: bool, device_ids: list[str]) -> bool:
    if not is_desktop:
        return False       # 第80行：不是桌面头 → 不免闸
    if not device_gate:
        return True        # 第83行：设备闸关闭 → 免闸（默认路径）
    return bool(device_id) and device_id in set(device_ids)
                           # 第84行：设备闸开启 → 检查设备名单
```

代码注释（gate.py:74-75）**自认可伪造**：
> 只认 X-PMS-Client 头是**可伪造**的（curl 加个头就绕过整道闸门）；加上设备名单后，伪造者还得先知道某台在册机器的 UUID。

### 3. device_gate 默认值确认（gate.py:96-107）

```python
# gate.py:104-106
"device_gate": stored.get(_CFG_DEVICE_GATE, "0").strip() == "1",
                                               # ^^^ 默认 "0"，即关
```

配置键 `_CFG_DEVICE_GATE = "device_gate_enabled"`（gate.py:25），存量环境不填此键时行为不变——**默认关闭**。

### 4. 测试代码的双重确认

- **test_login_gate.py:174-178**（测试5）明确测试并确认此行为：
  ```
  外网 + X-PMS-Client: desktop/... 头 → 直接发 token（客户端免闸）
  ```
  测试头：`"X-PMS-Client": "desktop/1.0.0"` + 外网 `X-Real-IP`

- **test_gate_device_ids.py:109-113**（测试4）证实开启 `device_gate` 后会拦截：
  ```
  伪造 X-PMS-Client 头绕闸，现在拦得住
  ```
  `device_gate=True` 时，只有头而没有在册 `device_id` 才被拦截。

### 5. 攻击路径还原

```
攻击者在外网（非回环/私网 IP）
  → POST /api/auth/login {"username":"已知用户","password":"已知密码"}
  → 携带请求头 X-PMS-Client: desktop/1.0.0
  → _client_ip 取 X-Real-IP（外网地址）
  → is_intranet(外网IP, []) = False（回环/私网都不命中，内网名单默认空）
  → is_desktop = header.startswith("desktop/") = True
  → desktop_exempt(True, "", device_gate=False, []) = True
  → exempt = True
  → 直接发 token，绕过了 6 位验证码流程
```

## 攻击前提条件

1. **必须知道有效账号密码**（闸门在密码验证之后，不是替代密码）
2. `device_gate` 处于默认关闭状态
3. 能从真正的外网 IP 发起请求（非回环/私网）

## 反例 / 排除项（证明我没漏看）

| 假设的防线 | 为何不成立 |
|---|---|
| nginx 层面拦截该头 | nginx 是反向代理，不校验业务头语义 |
| X-PMS-Client 被签名/加密 | auth_router.py:90 仅做 `startswith("desktop/")` 字符串比较，无 HMAC/JWT/证书验证 |
| `_client_ip` 会被客户端端伪造的 IP 骗过 | 优先取 `X-Real-IP`（nginx `$remote_addr` 覆写，不可伪造）— 但这里恰好确认了攻击者是外网 IP，`is_intranet` 返回 False，反而帮攻击者"证明了自己在外网" |
| 有其他中间件校验设备 | 中间件 `main.py:133-141` 只做统计 upsert（写 `desktop_clients` 表），不拦截请求 |
| device_gate 已默认开启 | gate.py:105 `stored.get(_CFG_DEVICE_GATE, "0")` 默认 `"0"`，即关闭 |

## 真正防线

`device_gate` 设备名单（gate.py:84）：管理员在「外网访问」页手工录入设备 UUID，只有名单内的设备才免闸。**默认关闭**，需要管理员主动打开 + 录入名单。

## 分歧 / 遗留

无。这道防线是已知设计取舍，代码注释、测试、项目知识库三方一致承认。是否视为安全漏洞取决于运营决策：若 `device_gate` 一直不开且存在弱口令风险，则提升严重度。
