# n21 核实报告：H5 与网页版/桌面端 localStorage 交叉污染

**判决**：PASS —— 说法成立，代码路径全部真实存在。

**核心理由**：代码路径 100% 匹配所述机制。但该共享是**设计意图**（非意外 bug），后果是双方最终都跳回各自登录页，行为正确。

---

## 证据逐条核实

### 1. 共享同一对 localStorage key

| 端 | 读 key | 写/清 key |
|---|---|---|
| H5 | `session.ts:17` `localStorage.getItem('pms_token')` | `session.ts:26-27` `setItem`; `:33-34` `removeItem` |
| H5 | `session.ts:14` `localStorage.getItem('pms_user')` | 同上 |
| H5 | `http.ts:18` 请求拦截器读 `pms_token` | `http.ts:28-29` 401 拦截器 `removeItem` |
| Web | `auth.ts:7-9` `localStorage.getItem('pms_token'/'pms_user')` | `auth.ts:74-75` `setItem`; `:107-109` `removeItem` |
| Web | `index.ts:12` 请求拦截器读 `pms_token` | `index.ts:131-132` 401 拦截器 `removeItem` |

结论：两端使用完全相同的 `pms_token` / `pms_user` key，无任何隔离机制。

---

### 2. 同源（必须同源才能共享 localStorage）

- Web 应用：`https://example.com/` → 加载 `index.html`
- H5 应用：`https://example.com/h5/` → 加载 `h5.html`
- `vite.config.h5.js:36` 注释明确写"API '/api'（同源）"
- H5 `apiBase.ts:14`：`API_BASE` 默认 `'/api'`（同源相对路径）

两者部署在同一 nginx 下的同一域名、同一端口，仅路径前缀不同。localStorage 按 origin 隔离（不按 path），故必然共享。

---

### 3. 401 时双方各自清 key

**Web 端 401 处理**（`frontend/src/api/index.ts:129-133`）：
```typescript
if (status === 401 && !isLoginRequest) {
  localStorage.removeItem('pms_token')
  localStorage.removeItem('pms_user')
  goLogin()  // 最终 reload/href → 整页重建
}
```
注意：这里**只清 localStorage，不调 `authStore.logout()`**（grep 全仓 `authStore.logout` / `useAuthStore.logout` 仅在 `auth.ts:102` 定义处出现，无外部调用）。

**H5 端 401 处理**（`frontend/src/h5/http.ts:27-30`）：
```typescript
if (err?.response?.status === 401) {
  localStorage.removeItem('pms_token')
  localStorage.removeItem('pms_user')
  if (!location.hash.startsWith('#/login')) location.hash = '#/login'
}
```

---

### 4. 另一方 isLoggedIn 仍 true 的证据

**H5 的 `token` ref**（`session.ts:17`）：
```typescript
export const token = ref<string>(localStorage.getItem('pms_token') || '')
```
- 模块加载时读一次 localStorage。
- 后续仅在 `setSession()` / `clearSession()` 中显式赋值才更新。
- **不存在** `window.addEventListener('storage', ...)` 监听（全文 grep 结果：`addEventListener.*storage` / `onstorage` 均无匹配）。

因此：Web 端清 localStorage 后，H5 的 `token` ref **不会自动更新**。

**H5 的 `isLoggedIn`**（`session.ts:19`）：
```typescript
export const isLoggedIn = computed(() => !!token.value)
```
因为 `token.value` 未更新，`isLoggedIn` 仍然为 `true`。

**Web Pinia store 同理**（`auth.ts:7-9`）：
```typescript
const token = ref<string>(localStorage.getItem('pms_token') || '')
const user = ref<User | null>(JSON.parse(localStorage.getItem('pms_user') || 'null'))
```
H5 清 localStorage 后，Web Pinia 的 `token` / `isLoggedIn` 不变。但 Web 的 401 分支最终通过 `goLogin()` 触发 `location.reload()` 或 `location.href`，**整页重建会刷新 store**，因此 Web 自身的 stale 状态不持久。

---

### 5. 下一次请求因无 token 而 401 的证据

**H5 请求拦截器**（`http.ts:17-21`）：
```typescript
http.interceptors.request.use((config) => {
  const token = localStorage.getItem('pms_token')  // 每次请求都从 localStorage 读
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})
```
该拦截器**每次请求实时读 localStorage**（非读 token ref），所以 localStorage 被清后，下一个请求不带 `Authorization` 头 → 服务端返回 401 → 触发 H5 自己的 401 处理 → redirect 到 `#/login`。

---

## 完整攻击链（按时间顺序）

1. 用户同时在 Web 和 H5 两个 SPA 中登录（同浏览器、同 origin）。
2. 令牌过期（8h）。
3. Web 端发请求 → 401 → `index.ts:129-133` 执行：
   - `localStorage.removeItem('pms_token')` / `pms_user`。
   - `goLogin()` → 整页重载/跳转。
4. H5 端完全不知情：
   - `session.ts:17` 的 `token` ref 维持旧值。
   - `session.ts:19` 的 `isLoggedIn` computed = `true`。
   - 无 storage 事件监听。
5. H5 端发下一次请求：
   - `http.ts:18` 从 localStorage 读 `pms_token` → `null`。
   - 请求不带 `Authorization` 头 → 401。
   - H5 自己的 401 handler 清 localStorage（已空）、切 hash 到 `#/login`。

---

## 性质判定

### 这是设计，不是 bug

`session.ts:6-7` 注释明确表达了设计意图：
> token 的 key 与桌面端保持一致（pms_token / pms_user）：同一浏览器先登过网页版再开 H5 时能直接复用，不用再登一次。

两端共享 `pms_token` 是为了**免重复登录**的正向场景设计的。交叉清 key 是其必然副效应。

### 后果在可接受范围内

无论哪端先触发 401/登出，最终结果都是**两端各自跳回登录页**——这恰好是令牌失效后的正确行为。不存在数据损坏或权限提升。

### 唯一的瑕疵

H5 的 401 处理不调 `clearSession()`（`http.ts:27-30`），导致 401 后 `token` ref / `isLoggedIn` 在内存中短暂残留。但由于页面已切到 `#/login`，且 login 路由是 `public`（`main.ts:22`），路由守卫不会误判，无实际危害。

---

## 试过但不成立的反驳路径

1. **"Web 的 401 处理会 reload 页面，所以 stale isLoggedIn 不持久"**——这对 Web 自己成立，但不影响 H5。H5 运行在另一个 SPA 实例/标签页中，不受 Web reload 影响。
2. **"两个 SPA 不太可能同时打开"**——不能基于使用频率驳代码路径存在性。
3. **"storage 事件可以同步"**——经 grep 证实前端无任何 `storage` 事件监听，此机制当前不存在。

---

## 证据锚点清单

| # | 证据 | 文件:行号 |
|---|------|-----------|
| 1 | H5 token ref 只从 localStorage 初始化一次 | `frontend/src/h5/session.ts:17` |
| 2 | H5 isLoggedIn = computed(token) | `frontend/src/h5/session.ts:19` |
| 3 | H5 clearSession 清 localStorage | `frontend/src/h5/session.ts:30-35` |
| 4 | H5 401 拦截器清 localStorage + 切 hash | `frontend/src/h5/http.ts:27-30` |
| 5 | H5 请求拦截器实时读 localStorage | `frontend/src/h5/http.ts:18` |
| 6 | Web Pinia token ref 只初始化一次 | `frontend/src/stores/auth.ts:7-9` |
| 7 | Web isLoggedIn = computed(token) | `frontend/src/stores/auth.ts:19` |
| 8 | Web logout 清 localStorage + 置空 ref | `frontend/src/stores/auth.ts:102-111` |
| 9 | Web 401 拦截器只清 localStorage（不调 logout） | `frontend/src/api/index.ts:129-133` |
| 10 | Web 401 后 goLogin 触发 reload | `frontend/src/api/index.ts:107-120` |
| 11 | 全仓无 storage 事件监听 | `frontend/src/` grep addEventListener.*storage → 0 结果 |
| 12 | 同源确认（H5 vite config） | `frontend/vite.config.h5.js:36` |
| 13 | 共享意图注释 | `frontend/src/h5/session.ts:6-7` |
| 14 | H5 登出按钮调用 clearSession | `frontend/src/h5/H5HomeView.vue:128` |
| 15 | H5ChatView 登出按钮调用 clearSession | `frontend/src/h5/H5ChatView.vue:268-269` |
