# n29 · 证伪核实：X-Real-IP 免闸判定可被绕过

> 核实员 · 证伪视角 | 2026-08-09

## 被核说法

> 外网登录闸门的免闸判定依赖 X-Real-IP 头可信，但该头只在请求必经 nginx 时才被 $remote_addr 覆写不可伪造；若绕过 nginx 直连 uvicorn 端口，客户端可伪造 X-Real-IP: 127.0.0.1 触发 is_loopback 恒免闸，整道验证码闸失效，代码层无反制

## 判决：PASS（说法成立）

## 逐项证据核查

### 1. `_client_ip` 盲目信任 X-Real-IP（成立）

**文件**: `backend/app/routers/auth_router.py:48-56`

```python
def _client_ip(request: Request) -> str:
    rip = (request.headers.get("x-real-ip") or "").strip()
    if rip:
        return rip   # ← 取到即返回，零校验
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    return parts[-1] if parts else (request.client.host if request.client else "")
```

- 当 X-Real-IP 头存在时，**不校验该值是否与 `request.client.host`（TCP 连接层真实地址）一致**
- `request.client.host` 只在 X-Real-IP 和 XFF 都为空时才作为兜底值使用
- 无 trusted proxy 白名单、无 IP 来源可信度标记

### 2. `is_intranet` 将回环地址恒判内网（成立）

**文件**: `backend/app/gate.py:54-64`

```python
def is_intranet(ip: str, cidrs: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_private:   # ← 127.0.0.1 恒 True
        return True
    return _ip_in(ip, cidrs)
```

- `is_loopback` 覆盖 127.0.0.0/8 和 ::1，**无条件**返回 True
- `is_private` 额外覆盖 10.0.0.0/8、172.16.0.0/12、192.168.0.0/16、fc00::/7
- 注释（gate.py:55-57）称这些段"天然不可能是外网来源"——**前提是 IP 值本身可信**，而 `_client_ip` 不保证这一点

### 3. nginx 覆写 X-Real-IP 确实存在（成立）

**文件**: `nginx/conf.d/_shared-locations.inc:33,43,54,69,82`

每处 `proxy_pass http://backend:8000` 均包含：
```
proxy_set_header X-Real-IP $remote_addr;
```

`$remote_addr` 是 nginx 与客户端之间的真实 TCP 连接地址，外部不可伪造。但这只对**经过 nginx 的流量**有效——nginx 做的事是"覆写"，不是"验证"。

### 4. 代码层无反制（成立）

搜索范围：`backend/app/` 全量 Python 文件。

- **无 trusted proxy 配置**：uvicorn 启动命令（`Dockerfile.prod:16`）为裸 `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4`，不含 `--proxy-headers` 或 `--forwarded-allow-ips`
- **无中间件 IP 校验**：`backend/app/main.py` 中唯一的 HTTP 中间件（`desktop_client_stats`，:139-157）只处理 X-PMS-Client 头统计，不验证 IP 来源
- **无 header vs. connection 地址比对**：`_client_ip` 选择信任 X-Real-IP，不将其与 `request.client.host` 做一致性检查（绕过 nginx 直连时，`request.client.host` 会是攻击者的真实公网地址）
- **gate.py 自身意识到但未处理此问题**：`desktop_exempt` 函数（gate.py:74）注释明确写"只认 X-PMS-Client 头是**可伪造**的"，但对 X-Real-IP 无同等警示；模块 docstring（gate.py:1-6）列出的免闸方式中也未提及"需 nginx 前置"

### 5. 生产部署层面的缓解（与说法无冲突，但影响可行性）

**文件**: `docker-compose.prod.yml`

```yaml
backend:
  # 不开放外网端口，由 nginx 代理       ← :53 行注释
  # 无 ports 配置                        ← 整个 services.backend 段无 ports key
```

生产编排中，只有 nginx 容器的 80/443 端口对外暴露。要直连 uvicorn 端口 8000 需：
- 已进入 docker 网络（容器间访问）
- SSH 到宿主机后 localhost 访问
- 或部署配置被意外改动（如误加 `ports: - "8000:8000"`）

--**说法用的"若"字面（"若绕过 nginx"）是正确的**：只说可行性，未宣称端口已暴露。

## 反例排除

| 猜想 | 结论 | 证据 |
|---|---|---|
| "`request.client.host` 可用于反伪造" | **不成立**——代码里 `_client_ip` 在 X-Real-IP 存在时直接返回 header 值，不读 `request.client.host` | auth_router.py:52-54: `if rip: return rip` |
| "uvicorn 的 `--proxy-headers` 已配置" | **不成立**——Dockerfile.prod:16 未传此参数 | `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4` |
| "有中间件校验收到的 IP 与连接地址" | **不成立**——main.py 中无此类中间件，唯一中间件只处理桌面客户端统计头 | main.py:139-157 |
| "gate.py 已在其他分支（如 desktop_exempt）处理同类问题" | **不成立**——`desktop_exempt` 虽然承认 X-PMS-Client 可伪造，但未将同等防范扩展到 X-Real-IP | gate.py:74-76 注释 |
| "生产端口不暴露，此说法无实际威胁" | **不成立**——说法本身是"若绕过"，未断言端口开；且部署配置可被误改 | docker-compose.prod.yml 无 backend ports |

## 其他视角（留给另一核实员）

- 防火墙层是否放行 8000 端口（iptables/安全组）--未查，属服务器运维
- docker 网络是否允许宿主机之外的主机路由到 backend 容器 --未查
- `request.client.host` 在 Starlette/Uvicorn 中异步 I/O 是否可作为可信的第二来源用于交叉验证 --未纳入本次核实

## 总结

**说法成立。** `_client_ip` 对 X-Real-IP 头采取"有即信"策略，`is_intranet` 对回环地址无条件放行，两者组合构成代码层面的绕过攻击面。生产 docker compose 通过不暴露 backend 端口来在部署层关闭这个窗口--但这是运维层面的缓解措施，不是代码层面的反制。
