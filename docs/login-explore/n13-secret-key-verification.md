# n13 · 核实#2·可复现——开发默认 SECRET_KEY 泄漏可伪造 token

> 核实员：只读探查，不改代码。只答一个问题：**这件事在什么条件下真的会发生**。

## 1. 说法逐条核验

### 1.1 硬编码默认值存在 ✓

`backend/app/config.py:28`：
```python
secret_key: str = "demo-secret-key-change-in-prod"
```

这是 Pydantic `BaseSettings` 的字段默认值。当且仅当**同时满足**以下条件时该值生效：
- 没有 `SECRET_KEY` 环境变量
- pydantic-settings 的 `env_file=".env"`（config.py:13）文件不存在或不含 `SECRET_KEY=...`

实测确认（`backend/` 目录无 `.env` 文件、无 `SECRET_KEY` 环境变量）：
```
settings.secret_key: 'demo-secret-key-change-in-prod'
Key matches default: True
```

### 1.2 HS256 对称签名 ✓

`backend/app/auth.py:36`：
```python
return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)
```

`backend/app/config.py:30`：`algorithm: str = "HS256"`

实测：用默认密钥签发 `sub=1` 的 admin token，`decode_token()` 成功解码为 `{'sub': '1', 'exp': 1786299032}`。

### 1.3 密钥长度低于 RFC 推荐 ⚠️

`demo-secret-key-change-in-prod` = **30 字节**。RFC 7518 §3.2 对 HS256 的最小推荐 = **32 字节**。
PyJWT 在 encode/decode 时均发出 `InsecureKeyLengthWarning`。这不阻止使用，但表示密钥强度不足。

### 1.4 离线伪造可行且威力大 ⚠️

已知密钥 = 可离线签发任意 `sub`（用户 ID）+ 任意 `exp`（过期时间）的 JWT，`decode_token` 照单全收。

唯一防线在 `deps.py:27`：每个请求查 `users` 表的 `is_active` 字段。但**对 active 用户（含 admin/manager）无任何额外限制**。

## 2. 核心问题：默认密钥到底能不能流到生产？

### 2.1 三条部署路径，逐一复盘

| 部署路径 | 默认值是否到达生产 | 阻断机制 |
|---|---|---|
| **A. deploy.sh（标准路径）** | **不会** | ① 首次运行自动 `openssl rand -hex 32` 生成随机密钥（deploy.sh:38-43）；② 即使手动编辑 `.env.prod` 未改 SECRET_KEY，`grep -qE 'SECRET_KEY=必须'` 会拦截并退出（deploy.sh:56-59） |
| **B. 手动 `docker compose -f docker-compose.prod.yml`** | **不会**，但缺省值不同 | docker-compose.prod.yml:32 写 `SECRET_KEY: ${SECRET_KEY}`（无默认值），docker compose v2 缺少变量时**报错拒绝启动**（Variable not set）。即使用空字符串，Pydantic 优先读环境变量，SECRET_KEY="" 为空字符串而非 "demo-secret-..." |
| **C. 直接 `uvicorn app.main:app`** | **会** | 无 SECRET_KEY 环境变量 + 无 `.env` 文件 → Pydantic 回落默认值。但这是开发命令，生产不用此方式 |

### 2.2 dev docker-compose 有自己独立的默认密钥

`docker-compose.yml:26`：
```yaml
SECRET_KEY: ${SECRET_KEY:-dev-secret-key-please-change}
```

开发 docker compose 的默认密钥是 `dev-secret-key-please-change`，**不是** `demo-secret-key-change-in-prod`。所以用 `docker compose up` 启动时，config.py 的字段默认值被 docker compose 的变量默认值覆盖了。

### 2.3 结论：生产到达条件几乎不存在

`deploy.sh` 是唯一的生产部署文档路径（README/AGENTS.md 均指向它），它做了两重防护：

1. **自动生成**（deploy.sh:39-41）：`openssl rand -hex 32` → 64 字符十六进制随机字符串，覆盖 `.env.prod.example` 的占位值
2. **占位符拦截**（deploy.sh:56-59）：即使自动生成失败（没有 openssl），grep 也会拦截占位值 `SECRET_KEY=必须` 并拒绝启动

唯一绕过路径：有人完全不看文档，在服务器上手动跑 `uvicorn` 且不设环境变量。但这已不是项目标准部署方式。

## 3. 影响面评估

| 维度 | 评估 |
|---|---|
| **如果发生，能伤到什么** | 攻击者可用 `demo-secret-key-change-in-prod` 伪造任意 active 用户的 JWT，获取该用户全部权限（含 admin）。**无法吊销**（无服务端会话/黑名单/refresh token）。改密码不使旧 token 失效。 |
| **严重度** | **高**（如果发生）—— 等同于任意用户全权限访问 |
| **发生概率** | **极低**（如果按 deploy.sh 部署）—— 双重阻断：自动生成 + 占位符检查 |
| **默认密钥的其他问题** | 30 字节低于 RFC 7518 最低推荐（32 字节），即使非生产使用也偏弱 |

## 4. 排除了什么

- `docker-compose.yml`（dev）的默认密钥是 `dev-secret-key-please-change`，不是说法中 config.py 的默认值——docker compose 的 env var 会覆写 Pydantic 默认值
- Dockerfile.prod 内未设 SECRET_KEY，把控制权交给 docker compose
- deps.py 的 `is_active` 检查是最低限度的补偿（禁用账号即使 token 有效也会 401），但不足以防御 active 账号的 token 伪造

## 证据清单

| # | 证据 | 文件:行号 | 说明 |
|---|---|---|---|
| 1 | 默认密钥硬编码 | backend/app/config.py:28 | `secret_key: str = "demo-secret-key-change-in-prod"` |
| 2 | JWT 签名使用 settings.secret_key | backend/app/auth.py:36 | `jwt.encode(payload, settings.secret_key, ...)` |
| 3 | 算法为 HS256 | backend/app/config.py:30 | `algorithm: str = "HS256"` |
| 4 | docker compose 注入 SECRET_KEY | docker-compose.prod.yml:32 | `SECRET_KEY: ${SECRET_KEY}` 无默认值 |
| 5 | deploy.sh 自动生成随机密钥 | deploy.sh:38-43 | `openssl rand -hex 32` → `SECRET_KEY=...` |
| 6 | deploy.sh 拦截占位符 | deploy.sh:56-59 | grep `SECRET_KEY=必须` → 拒绝启动 |
| 7 | dev docker compose 有独立默认值 | docker-compose.yml:26 | `SECRET_KEY: ${SECRET_KEY:-dev-secret-key-please-change}` |
| 8 | .env.prod.example 占位提示 | .env.prod.example:10 | `SECRET_KEY=必须替换为 64 位随机...` |
| 9 | 密钥长度 30 字节 < RFC 推荐 32 | 实测 | PyJWT InsecureKeyLengthWarning |
| 10 | 伪造 token 实测通过 | 命令输出 | `create_access_token("1")` + `decode_token` 成功 |
