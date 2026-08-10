# n31 · 核实#3 · models.py LoginGateCode.fail_count 注释过时

**判决：PASS（说法成立）**

## 核实过程

### 证据 1：models.py:1204 注释仍写 >=5 锁定

```
backend/app/models.py:1204
fail_count: Mapped[int] = mapped_column(default=0)  # 连续错码次数（>=5 锁定）
```

注释明确写着 `>=5 锁定`。

### 证据 2：gate.py:161-164 明确声明已去掉锁定

```
backend/app/gate.py:161-164
"""验码：按 pre_token+user_id 找未用行；不存在/已用/过期 → 400；哈希不符 → fail_count+1 后 400；
成功 → used=True。异常一律 HTTPException，由调用方写审计。
（🆕 2026-07-28 应要求去掉「错 5 次锁定」：不再锁，错误仅计 fail_count）"""
```

文档字符串明确声明 2026-07-28 已去掉锁定。上方模块级常量注释也相互印证（`:21`）：
```python
_RATE_PER_MIN = 1  # 同账号发码限频：每分钟（防误点刷屏；🆕 2026-07-28 应要求去掉每日上限与错码锁定）
```

### 证据 3：verify_code 只写 fail_count，从不读它做决策

```
backend/app/gate.py:172-175
if hashlib.sha256(code.strip().encode()).hexdigest() != row.code_hash:
    row.fail_count += 1
    await db.commit()
    raise HTTPException(400, "验证码错误")
```

整个 `verify_code` 函数（:161-177）中 `fail_count` 的唯一操作为 **写入（+1）**：
- 无任何 `if row.fail_count >= 5` 或类似锁定判断
- 验码成功（:176-177）只设 `used = True`，不重置 `fail_count`

### 证据 4：调用方 auth_router.py 也不消费 fail_count

```
backend/app/routers/auth_router.py:112-130
```

`login_verify_gate` 调用 `gate.verify_code()` 后，不检查任何 `fail_count` 相关状态，直接发 token。

### 证据 5：测试明确验证不锁定行为

```
backend/tests/test_login_gate.py:146-162
# ===== 4. 错码不锁定（🆕 2026-07-28 应要求去掉错5次锁定）
# 连错 6 次仍 400 非 429，正确码仍可用
```

测试连输 6 次错误码，断言每次返回 400（非 429 锁定），且 `fail_count == 6` 后正确码仍可登录。

### 证据 6：全项目 grep 确认无 fail_count 消费分支

`grep fail_count` 命中 23 处，分布在：
- `models.py:1204`：字段定义 + 过时注释
- `gate.py:162-164,173`：docstring 声明不锁定 + 唯一写入点
- `test_login_gate.py:132-167`：测试读写（断言用）
- 4 篇文档：均记载此注释过时问题

**除测试断言外，无任何生产代码读取 `fail_count` 做决策。**

## 结论

说法四点全部成立：

| 子断言 | 结论 | 证据 |
|--------|------|------|
| 注释写 >=5 锁定 | ✅ 成立 | models.py:1204 原文 |
| 2026-07-28 已去掉锁定 | ✅ 成立 | gate.py:164 注释 |
| 验码成功不清零 | ✅ 成立 | gate.py:176-177 只设 used=True |
| 无分支消费该值 | ✅ 成立 | 全项目 grep，仅测试读 |

## 上游已知

n4 gate-analysis.md:149 已记录同一问题（"fail_count 只增不清零...注释过时点一"），本核实独立复现 n4 结论，未发现新的分歧或遗漏。
