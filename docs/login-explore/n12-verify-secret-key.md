# n12 · 核实报告：开发默认 SECRET_KEY

> 核实视角：证伪优先 | 判定：PASS（四个事实点全部成立，安全风险有效）

---

## 被核说法（原文）

> 开发默认 SECRET_KEY 为硬编码明文 'demo-secret-key-change-in-prod'(config.py:28)，所有环境共用同一 HS256 对称密钥签名 JWT；若部署时未用 .env.prod 覆盖，该密钥泄露=可离线伪造任意用户 token

证据：`backend/app/config.py:28`、`backend/app/auth.py:26-36`(jwt.encode 用 settings.secret_key)、`docker-compose.prod.yml:32`(SECRET_KEY 由 env 注入)、`deploy.sh:38-43`(首次自动生成随机值)

---

## 逐条核实

### 1. 默认值为 `"demo-secret-key-change-in-prod"`（config.py:28）

**结果：成立。**

```python
# backend/app/config.py:28
secret_key: str = "demo-secret-key-change-in-prod"
```

这是 pydantic-settings 字段默认值。当环境变量 `SECRET_KEY` 未设置、`.env` 文件也不存在时，`settings.secret_key` 取值即为此字符串。

补充证据：
- `.env.example:9`：`SECRET_KEY=dev-secret-key-please-change-in-prod`（本地开发模板值，**不同于** 代码默认值）
- `.env.prod.example:10`：`SECRET_KEY=必须替换为 64 位随机十六进制字符串`（生产模板为占位符，非有效值）

即：代码默认 `demo-secret-key-change-in-prod` ↔ 本地开发模板 `dev-secret-key-please-change-in-prod` ↔ 生产模板占位符。三者互不相同。

---

### 2. JWT 用 `settings.secret_key` 做 HS256 对称签名（auth.py:26-36）

**结果：成立。**

```python
# backend/app/auth.py:36
return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)
```

```python
# backend/app/auth.py:41 (验签)
return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
```

```python
# backend/app/config.py:30
algorithm: str = "HS256"
```

签发（`create_access_token`）与验证（`decode_token`）使用**同一个密钥** — 对称签名。密钥泄露 = 任何人可在同一 HS256 算法下签发任意 `{"sub": user_id, "exp": ...}` 的合法 JWT，以任意用户身份通过 `get_current_user`（`deps.py:11-27`）验证。

---

### 3. docker-compose.prod.yml:32 — SECRET_KEY 由 env 注入

**结果：成立。**

```yaml
# docker-compose.prod.yml:32
SECRET_KEY: ${SECRET_KEY}
```

**重要补充（说法未提及的额外防护）**：生产编排文件中 `${SECRET_KEY}` **无默认值**（没有 `:-fallback` 语法）。如果 `.env.prod` 未定义 `SECRET_KEY`，docker compose v2 会直接报错退出：
```
WARN[0000] The "SECRET_KEY" variable is not set. Defaulting to a blank string.
```
→ 容器环境变量 `SECRET_KEY=`（空串）→ pydantic-settings 会取空串而非代码默认值（环境变量优先级 > 默认值）→ JWT 签名密钥为 `""`。

结论：生产 docker compose 路径**不可能**回退到代码默认值 `demo-secret-key-change-in-prod`。最坏情况是空串签名（仍不安全，但不同于原说法中的 demo key）。

---

### 4. deploy.sh:38-43 — 首次部署自动生成随机值

**结果：成立。**

```bash
# deploy.sh:38-43
cp .env.prod.example "$ENV_FILE"
# 自动生成强随机 SECRET_KEY
if command -v openssl >/dev/null 2>&1; then
    SECRET=$(openssl rand -hex 32)
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$SECRET|" "$ENV_FILE"
    echo "      已自动生成随机 SECRET_KEY"
fi
```

额外防护 — 占位符校验（deploy.sh:56-57）：
```bash
if grep -qE 'POSTGRES_PASSWORD=请改|SECRET_KEY=必须' "$ENV_FILE"; then
    echo "[错误] $ENV_FILE 中仍有未修改的占位值..."
    exit 1
fi
```

即 deploy.sh 提供双重保障：
1. 首次不存时自动生成 `openssl rand -hex 32`（64 字符随机十六进制）
2. 存在但为占位符时**拒绝部署**（exit 1）

---

## 证伪尝试（打回的材料）

| 尝试证伪点 | 结果 | 证据 |
|---|---|---|
| "所有环境共用同一 HS256 对称密钥" | **不精确**：dev docker compose 有独立默认值 `dev-secret-key-please-change`，不同于代码默认 | `docker-compose.yml:26` |
| "若部署时未用 .env.prod 覆盖" — 生产会不会回退到 demo key | **不会**：docker-compose.prod.yml `${SECRET_KEY}` 无默认值，变量未定义时 docker compose 报错，容器收到的可能是空串而非代码默认 | `docker-compose.prod.yml:32` |
| 是否有运行时 guard 检测 demo key | **不存在**：全仓 grep `demo-secret-key` 仅 config.py:28 一处命中，无任何启动/运行时校验 | grep 结果 |

---

## 分歧与精确度修正

1. **"所有环境共用同一"** 不准确：dev docker compose 默认值（`dev-secret-key-please-change`）和代码默认值（`demo-secret-key-change-in-prod`）是**两个不同的弱密钥**。严格说该表述可改为"各环境的默认密钥均为硬编码明文值"。

2. **说法未提及 docker-compose.prod.yml 的无默认值设计**：这恰好是额外防护——即便 deploy.sh 被绕过，docker compose 的 `${SECRET_KEY}`（无 fallback）也会阻止以代码默认值启动。但该防护并未消除风险—用户可手动设弱密码绕过。

3. **说法未提及 deploy.sh 的占位符校验（line 56-57）**：这是首道防线的第二重（自动生成 + 拒绝占位符），使 deploy.sh 路径更不可能遗漏。

---

## 最终判决（PASS）

四个举证锚点全部验真：
- `config.py:28` → 代码默认值确为 `"demo-secret-key-change-in-prod"`
- `auth.py:36` → JWT 签发/验证使用同一 `secret_key`，HS256 对称算法
- `docker-compose.prod.yml:32` → SECRET_KEY 从 env 注入
- `deploy.sh:38-43` → 首次自动生成随机值写入

安全风险表述有效：密钥为对称 HS256，泄露 = 可离线签发任意用户 token。上述防护（自动生成 + 占位符拒绝 + docker compose 无默认值）是**有效的防御纵深**但均为**部署流程级**防护，代码层面无运行时警告/硬拒绝 demo key 的 gate。

---

## 结论总结

核心说法四个事实点全部成立。安全风险有效（HS256 对称密钥 + 硬编码默认值泄露可伪造 token）。存在部署流程级防御（deploy.sh 双重保障 + docker compose 无默认值），但不改变"代码默认是 demo key"这一事实本身。
