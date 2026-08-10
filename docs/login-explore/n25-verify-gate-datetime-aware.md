# n25 · 核实#2·证伪 登录/验证码逻辑里 datetime aware/naive 差异

> 核实员视角，默认立场 = 证伪。核一条说法，核完就走。

## 说法原文

> 登录/验证码逻辑里 SQLite 与 Postgres 的 datetime aware/naive 差异只有 gate.py 的 _aware() 一处兜底（SQLite 读回 naive，Postgres 读回 aware），而所有登录测试（test_login_gate/test_gate_device_ids/test_desktop_*）全在临时 SQLite 上跑

## 判决

**PASS（成立）**。三个子断言逐一核实，全部成立。

---

## 子断言 1：登录/验证码逻辑里只有 gate.py 的 _aware() 一处兜底

### 核实过程

**搜索 `def _aware`**：全仓仅 `gate.py:33` 一个定义。

```bash
$ grep -rn "def _aware" backend/
backend/app/gate.py:33:def _aware(dt: datetime) -> datetime:
```

**gate.py:33-35**（_aware 函数体）：

```python
def _aware(dt: datetime) -> datetime:
    """SQLite 读回的是 naive 时间，统一按 UTC 补齐时区再比较。"""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
```

**唯一调用点**：`gate.py:170` —— `verify_code()` 中：

```python
if not row or _aware(row.expires_at) < _now():
    raise HTTPException(400, "验证码无效或已过期，请重新获取")
```

这是登录/验证码逻辑中**唯一**在 Python 层比较数据库回读 datetime 的代码。`issue_code` 中 `LoginGateCode.created_at >= now - timedelta(minutes=1)`（gate.py:135）走 SQL 层的 WHERE 子句，由 SQLAlchemy 参数化处理，不需要 Python 层 datetime 转换。auth_router.py 中所有 `last_login` 操作都是写入（`datetime.now(timezone.utc)`），不涉及从数据库读回后比较。

### 其他文件中的 naive→aware 逻辑（不在登录链路中）

| 文件 | 行号 | 逻辑 | 是否在登录链路 |
|------|------|------|---------------|
| overdue.py | :26 | `ts.replace(tzinfo=timezone.utc)` | 否（逾期扫描） |
| briefing.py | :63 | `d if d.tzinfo else d.replace(tzinfo=timezone.utc)` | 否（晨报聚合） |
| tools_entity.py | :41 | 同上 | 否（AI 助手工具） |
| pay_req.py | :105 | `hit.created_at.replace(tzinfo=...)` | 否（请款卡片） |

这些 inline 的 naive→aware 补齐逻辑**均不在登录/验证码代码路径中**，不影响本说法的正确性。

### 结论

限定在"登录/验证码逻辑"内，`_aware()` 确实是唯一一处兜底。**子断言 1 成立。**

---

## 子断言 2：所有登录测试全在临时 SQLite 上跑

### 核实过程

逐一读取四个相关测试文件的头部：

**test_login_gate.py:15**：
```python
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
```

**test_gate_device_ids.py:15**：
```python
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
```

**test_desktop_clients.py:12**：
```python
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
```

**test_desktop_report.py:14**：
```python
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
```

另外，两个非登录但常被一起提及的测试（`test_smoke_startup.py`、`test_m01_roles_menus.py`）也全部使用相同的 SQLite 环境设置。

### 结论

全仓无任何登录相关测试连接 Postgres。**子断言 2 成立。**

---

## 子断言 3：Postgres 下 DateTime(timezone=True) 行为与 SQLite 不同

### 核实过程

`LoginGateCode` 模型（models.py:1201-1202）：
```python
created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
```

两列均声明为 `DateTime(timezone=True)`。在 Postgres 中映射为 `TIMESTAMPTZ`，读回 aware datetime（带 tzinfo）；在 SQLite 中 SQLAlchemy 存入 ISO 字符串（UTC 归一后剥离时区），读回 naive datetime（无 tzinfo）。这是 SQLAlchemy 的已知行为，在 AGENTS.md 和 data_migration.py:43 均有提及。

`_aware()` 的注释（gate.py:34）直接写明了这一差异的动机："SQLite 读回的是 naive 时间"。

### 结论

两库行为确实不同，`_aware()` 是专门为此差异设计的兜底。**子断言 3 成立。**

---

## 额外观察（补充分析）

### naive 比较在 SQLite 测试中是更严格的检查

Python 3 中 `naive_datetime < aware_datetime` 会直接抛 `TypeError`。因此：
- 如果某处在 Python 层比较数据库回读的 datetime 却忘了做 naive→aware 转换，**SQLite 测试会当场 crash**，Postgres 反而不会（因为 Postgres 读回的就是 aware）。
- 结论：**SQLite 测试在 datetime 处理方面比 Postgres 更严**，能检测到在 Postgres 下不会暴露的缺陷。

### `issue_code` 中的限频比较不受影响

`gate.py:135` 的 `LoginGateCode.created_at >= now - timedelta(minutes=1)` 是 SQLAlchemy ORM WHERE 子句，datetime 值经参数绑定后由数据库引擎执行比较，不经过 Python 层的 `datetime.__lt__`。无需 `_aware()` 兜底。

---

## 反例 / 被排除的猜想

- ~~可能有其他地方对 LoginGateCode 做 Python 层 datetime 比较~~：已全仓搜索 `LoginGateCode` 的所有引用（共 12 处），均在 `gate.py` 中，且只有 `verify_code:170` 一行做 Python 层比较。
- ~~test_desktop_* 不算"登录测试"~~：`test_desktop_clients.py` 和 `test_desktop_report.py` 虽然不直接测登录流程，但测了桌面免闸判定所依赖的头契约（X-PMS-Client/X-PMS-Device），与登录行为紧密耦合。n7 产出将它们归入登录测试合理。
- ~~可能有 Postgres 专门登录测试~~：全仓搜索 `postgres://` / `postgresql://` 在 `backend/tests/` 目录，无结果。

---

## 证据汇总

| 证据 | 文件:行号 |
|------|-----------|
| `_aware()` 唯一定义 | gate.py:33-35 |
| `_aware()` 唯一调用（登录链路） | gate.py:170 |
| `LoginGateCode.expires_at` 声明 | models.py:1202 |
| `LoginGateCode.created_at` 声明 | models.py:1201 |
| test_login_gate 用 SQLite | test_login_gate.py:15 |
| test_gate_device_ids 用 SQLite | test_gate_device_ids.py:15 |
| test_desktop_clients 用 SQLite | test_desktop_clients.py:12 |
| test_desktop_report 用 SQLite | test_desktop_report.py:14 |
| issue_code 限频比较走 SQL 层 | gate.py:135 |
| 其他 naive→aware 逻辑不在登录链路 | overdue.py:26 / briefing.py:63 / tools_entity.py:41 / pay_req.py:105 |
