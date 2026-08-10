# n14 · 核实#3·证伪：种子账号 manager 密码硬编码且不可覆盖

**判决**: PASS — 说法成立

## 核实的三部分

### 1. manager 密码硬编码在 seed.py

**证据**: `backend/app/seed.py:93-97`
```python
m = models.User(
    username="manager",
    full_name="管理员",
    password_hash=hash_password("manager123"),
    ...
)
```
字符串 `"manager123"` 是直接写在代码里的字面量，没有读取任何变量或配置。

### 2. 无环境变量可覆盖

**证据**:
- `backend/app/config.py:32-34` — Settings 类只有 `default_admin_username` 和 `default_admin_password`，不存在 `default_manager_*` 字段。
- seed.py 全文（104 行）搜索：manager 创建段（:88-103）未引用 `settings` 的任何属性（对比 admin 创建段 :63-82 明确使用了 `settings.default_admin_username` 和 `settings.default_admin_password`）。
- `grep DEFAULT.*MANAGER / MANAGER.*PASS / manager.*env` 在全项目零匹配。

### 3. 生产仅 admin 可通过环境变量覆盖

**证据**:
- `docker-compose.prod.yml:34-35` 透传了 `DEFAULT_ADMIN_USERNAME` 和 `DEFAULT_ADMIN_PASSWORD` 两个环境变量。
- admin 的创建（`seed.py:70,72`）引用 `settings.default_admin_username/password`，通过 pydantic-settings 自动从同名环境变量读取。
- manager 密码无对应环境变量，docker-compose 也不透传。

### 证伪尝试（未找到漏洞）
- 排查了 seed.py 之外是否有其他路径创建 manager 账号 → 无（seed() 是唯一种子入口，main.py startup 调用）。
- 排查了 config.py 是否有遗漏的 manager 配置字段 → 无（78 行全扫描，无 manager 关键词）。
- 排查了 .env / docker-compose 是否有 manager 环境变量 → 无。

## 结论

说法三者皆成立：manager 密码硬编码 `"manager123"`（`seed.py:96`），无环境变量可覆盖，仅 admin 可通过 `DEFAULT_ADMIN_USERNAME/PASSWORD` 覆盖。
