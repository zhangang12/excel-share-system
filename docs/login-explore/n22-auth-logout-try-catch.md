# n22 · 核实#2·证伪 auth.logout try/catch ignore 声称

## 判决：FAIL（说法不成立）

## 要核的说法

> auth.logout 里 authApi.logout() 用 try/catch ignore（:102-103），登出请求失败被静默吞掉；若后端 logout 是服务端失效 token 的设计，网络失败时服务端 token 仍有效且审计日志丢失

## 核实过程

### 1. 确认前端代码（说法给的证据）

`frontend/src/stores/auth.ts:102-111`：

```typescript
function logout() {
    try { authApi.logout() } catch { /* ignore */ }
    token.value = ''
    user.value = null
    menus.value = null
    localStorage.removeItem('pms_token')
    localStorage.removeItem('pms_user')
    localStorage.removeItem('pms_menus')
    localStorage.removeItem('pms_can_view_detail')
}
```

- `try/catch ignore` 确实存在（:102-103）
- 但无论 authApi.logout() 成功或失败，后续客户端清除 token/user/menus 的代码**都会执行**（不在 try 块内）

### 2. 核实后端 logout 实现（说法的核心前提）

`backend/app/routers/auth_router.py:167-170`：

```python
@router.post("/logout", response_model=schemas.Msg)
async def logout(_: models.User = Depends(get_current_user)):
    # JWT 无状态，靠客户端清 token 即可
    return schemas.Msg(message="已登出")
```

**后端 logout 是纯空操作**：
- 不做任何服务端 token 失效（JWT 无状态，token 本来就不过期不撤销）
- 不写任何审计日志
- 只是验证了当前用户身份后返回 `{"message": "已登出"}`

### 3. 说法预设的前提不成立

说法的核心逻辑链是：
1. 前端 try/catch ignore 吞掉网络错误
2. 如果后端 logout 是服务端失效 token 的设计 → 网络失败时 token 仍有效 + 审计日志丢失

但这个链在第 2 步断开：**后端 logout 根本不是服务端失效 token 的设计**，它什么都不做。因此：
- 不存在"服务端 token 仍有效"的问题（token 本来就是永远有效的 JWT，不管调不调 logout）
- 不存在"审计日志丢失"的问题（logout 端点从来不写审计日志）

### 4. 前端设计在 JWT 无状态架构下的合理性

在 JWT 无状态认证中，logout 的唯一实际效果是客户端清除存储的 token。前端 logout() 函数：
- catch 之前尝试调用后端 logout（通知后端，目前后端是空操作）
- catch 之后**无论如何都清除客户端 token**（localStorage.removeItem）
- 所以即使网络完全断掉，用户点登出后 token 也从客户端消失了

## 证据清单

| 证据 | 文件:行号 |
|------|-----------|
| 前端 logout 的 try/catch ignore | `frontend/src/stores/auth.ts:102-103` |
| 前端 logout 的客户端清除（catch 外） | `frontend/src/stores/auth.ts:104-111` |
| 后端 logout 纯空操作 + JWT 无状态注释 | `backend/app/routers/auth_router.py:167-170` |
| 前端 authApi.logout 调用 | `frontend/src/api/auth.ts:36` |

## 分歧/遗留

无。
