# n30 · 核实#2 · LoginGateCode 码行落库依赖 write_audit

**判决**：PASS —— 说法成立。

**日期**：2026-08-09

---

## 说法原文

> LoginGateCode 码行落库实际依赖调用方 auth_router.py:105 的 write_audit 的 commit 副作用：push_message 在 manager 角色无 active 用户时提前 return 0 且不 commit（notify.py:60-62），此时 issue_code 内 db.add 的码行悬而未提交；若未来移除该 write_audit 调用...

## 逐条证据核实

### 1. issue_code 不自己 commit

`gate.py:147-158`：

```python
# gate.py:147-152
db.add(models.LoginGateCode(
    user_id=user.id,
    code_hash=hashlib.sha256(code.encode()).hexdigest(),
    pre_token=pre_token,
    expires_at=now + timedelta(minutes=CODE_TTL_MIN),
))
await push_message(
    db, to_role="manager", kind="warn",
    text=f"【外网登录验证】{user.full_name}({user.username}) 正在外网登录系统，"
         f"验证码：{code}（10 分钟内有效）。请核实身份后告知本人。",
)  # push_message 自带 commit，上面的码行随之落库
return pre_token
```

- `db.add(...)` 在第 147 行，之后只有一个 `await push_message(...)` 调用
- code 注释（第 157 行）明确写 "push_message 自带 commit，上面的码行随之落库"
- issue_code 自己**没有**额外的 `await db.commit()`
- 把落库责任完全委托给 push_message 或调用方

### 2. push_message 在无 active 用户时提前 return 且不 commit

`notify.py:44-62,75-76`：

```python
# notify.py:44-59 — 查询 manager 角色的 is_active==True 用户
if to_role:
    ...
    res = await db.execute(
        select(models.User.id).where(
            models.User.is_active == True,
            or_(models.User.role_id.in_(rids), models.User.id.in_(sub)),
        )
    )
    user_ids.extend(r[0] for r in res.all())

# notify.py:60-62 — 无用户时直接 return，不 commit
if not user_ids:
    log.info("push_message: 角色 %s 无在线用户，消息丢弃: %s", to_role, text[:50])
    return 0

# notify.py:75-76 — 正常路径才 commit
db.add_all(rows)
await db.commit()
```

- **正常路径**（有 manager 的 active 用户）：user_ids 非空 → 第 75-76 行 `db.add_all` + `await db.commit()` → 码行落库
- **edge case**（无 manager 的 active 用户）：user_ids 为空 → 第 60-62 行 `return 0`，无 commit → 码行悬在 session 中

### 3. auth_router.py 调用链

`auth_router.py:92-108`：

```python
# auth_router.py:98-108
if cfg["enabled"] and not exempt:
    try:
        pre_token = await gate.issue_code(db, u)      # line 100
    except HTTPException as e:
        await write_audit(db, user=u, action="login_gate_fail",
                          detail=str(e.detail), ip=ip or None)   # line 102-103
        raise                                                  # line 104
    await write_audit(db, user=u, action="login_gate_issue", ip=ip or None)  # line 105
    return schemas.GateRequiredOut(...)
```

- issue_code 在 try 块中调用（第 100 行）
- issue_code 正常返回后，**必须走到**第 105 行的 `write_audit`
- 如果 issue_code 抛异常（如 429 限频），进 except 块执行 write_audit 后 raise —— 但限频场景下 add 码行根本没执行（第 136-137 行提前返回），所以不涉及码行落库问题

### 4. write_audit 确实会 commit

`utils.py:10-32`：

```python
# utils.py:31-32
db.add(rec)
await db.commit()
```

`write_audit` 确实执行 commit，会连带提交当前 session 中的所有未提交变更（包括 issue_code 中 add 的 LoginGateCode 码行）。

---

## 分场景分析

### 场景 A：manager 有 active 用户（正常路径）

| 步骤 | 位置 | 操作 |
|------|------|------|
| 1 | gate.py:147 | `db.add(LoginGateCode)` |
| 2 | gate.py:153 | `await push_message(...)` |
| 3 | notify.py:59 | user_ids 非空 |
| 4 | notify.py:75-76 | `db.add_all(rows)` + `await db.commit()` → **码行在此落库** |
| 5 | gate.py:158 | `return pre_token` |
| 6 | auth_router.py:105 | `await write_audit(...)` → 再 commit 一次（多余） |

**结论**：正常路径下码行在 push_message 内部就落库了，**不依赖** write_audit。

### 场景 B：manager 无 active 用户（edge case）

| 步骤 | 位置 | 操作 |
|------|------|------|
| 1 | gate.py:147 | `db.add(LoginGateCode)` |
| 2 | gate.py:153 | `await push_message(...)` |
| 3 | notify.py:60-62 | user_ids 为空 → `return 0` **无 commit** |
| 4 | gate.py:158 | `return pre_token` → **码行悬在 session 中** |
| 5 | auth_router.py:105 | `await write_audit(...)` → `db.commit()` → **码行在此通过 write_audit 连带提交** |

**结论**：edge case 下码行**确实依赖** write_audit 的 commit。

---

## 判决

**PASS** —— 说法描述的依赖关系在 edge case（manager 无 active 用户）下成立。

### 为何 PASS 而非 PARTIAL

说法原文写的是 "push_message 在 manager 角色无 active 用户时提前 return 0 且不 commit（notify.py:60-62），**此时** issue_code 内 db.add 的码行悬而未提交" —— 它明确限定了条件（"此时" = 无 active 用户的 edge case），并非宣称所有路径都依赖 write_audit。

### 额外发现：代码注释的假设有漏洞

`gate.py:157` 注释断言 "push_message 自带 commit，上面的码行随之落库"，但这个注释**只在正常路径下成立**。在 notify.py:60-62 的 edge case 下，push_message 不 commit，注释跟行为不一致。此注释隐含了"push_message 总是 commit"的错误假设。

### 风险场景

若有人在不知情的情况下移除 auth_router.py:105 的 write_audit（例如觉得"登录发码成功没必要记审计"），edge case 下（manager 无 active 用户）LoginGateCode 码行将丢失——既没有 commit，也没有 rollback，session 结束时这条记录就消失了。

### 验证方法

实际动手做了：打开 gate.py / notify.py / auth_router.py / utils.py 四个文件的对应行号，追踪了整个调用链中 commit 的所有可能路径。

---

## 文件清单

产出文档：`docs/login-explore/n30-LoginGateCode-commit-dependency.md`
