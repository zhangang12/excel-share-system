# n18 · 核实#2 · 证伪 version.js force_latest 说法

**判决**: PASS（说法成立）

---

## 被核说法原文

> version.json 的 force_latest:true 当前生效，客户端强制升级口径是「必须为通道最新版」而非手工 min_version 地板，与 AGENTS.md「API 只增不改、老客户端长期并存」策略存在节奏张力（每次发版所有旧客户端都被强制）

---

## 证据链

### 1. force_latest:true 当前生效 ✓

**本地文件** `desktop/version.json:4`：
```json
"force_latest": true
```

**线上文件** `http://8.141.123.141/desktop/version.json`（HTTP GET 实测返回）：
```json
{
  "min_version": "1.0.33",
  "notes": "修复反复无响应时卡在重载循环里...",
  "force_latest": true
}
```
线上线下一致，`force_latest:true` 已部署生效。

### 2. 强制升级口径 = 通道最新版而非 min_version 地板 ✓

核心逻辑在 `desktop/lib/health.js:45-50`：

```js
function requiredVersion(cfg, latest, knowsForceLatest) {
  let need = (cfg && cfg.min_version) || '';
  if (knowsForceLatest && cfg && cfg.force_latest && latest
      && (!need || compareVersions(latest, need) > 0)) need = latest;
  return need;
}
```

调用点在 `desktop/main.js:320`：

```js
const need = requiredVersion(j, j.force_latest ? await latestChannelVersion() : '', true);
```

当 `force_latest:true` 时，流程为：
1. 获取 `latestChannelVersion()` → 拉取 `http://8.141.123.141/desktop/latest.yml`
2. `requiredVersion` 取 `max(min_version, latest)` 作为要求版本
3. `knowsForceLatest` 硬编码为 `true`（`main.js:320` 第三个参数），即所有当前客户端都认识此字段

**实测 `latest.yml` 返回值**：`version: 1.0.40`

因此当前有效的强制版本为 `max(1.0.33, 1.0.40)` = **1.0.40**（通道最新版），而非 `min_version` 地板 `1.0.33`。

代码注释也明确说明了设计意图（`main.js:311-313`）：

```
//   min_version   —— 手工设的地板，老客户端只认这个
//   force_latest  —— 「必须是通道上的最新版」，省得每次发版都要记得改 min_version
```

### 3. 与 AGENTS.md「老客户端长期并存」策略存在节奏张力 ✓

AGENTS.md 的策略表述（行可见性相关知识库条目）：

> API 只增不改（老客户端长期并存），破坏性变更只能走 `--min-version` 强制升级流程

而 `force_latest:true` 的效果是：
- 每次发版 → `latest.yml` 版本号前移 → `requiredVersion` 更新为新版本
- **所有低于新版本的客户端在启动时都被强制进入更新页**（`main.js:762-768`）
- 并非只有破坏性变更才触发强制升级，而是**每次发版都强制**
- `force_latest` 的效果等价于**每次发版都写 `min_version` 为最新版号**，这正是注释里说的"省得每次发版都要记得改 min_version"

### 4. 无绕过路径 ✓

启动时强制检查（`main.js:742-768`）：
```
app.whenReady() → 打包模式 → checkForceUpdate() → 不满足即 forceMode=true → 窗口加载 force-update.html
```

登录前二次拦（`main.js:335-350`）：
```
enforceVersionBeforeLogin() → 再次 checkForceUpdate() → 不满足即阻止登录
```

网络不通时放行（`main.js:326`），但这属于可靠性设计而非权限绕过。

---

## 证伪尝试（被排除的猜想）

| 猜想 | 排除原因 |
|------|----------|
| `knowsForceLatest=false` 时老客户端不受影响 | `main.js:320` 硬编码传 `true`；且 1.0.29 起就认此字段 |
| `latest.yml` 拉取失败时走 min_version 地板 | 对，但这是网络容错，不是设计上的分级策略 |
| `force_latest:true` 不意味着"所有旧客户端被强制"，因为最新版的客户端不需要更新 | 语义上确实——通道最新版的客户端不会被强制；但"旧客户端"按通常理解指非最新版客户端，此时均被强制 |
| `force_latest` 可用 `--min-version` 绕过 | 否——`requiredVersion` 取两者**更高者** |

---

## 分歧 / 遗留

无。说法三项断言均被验证成立。

---

## 验证步骤摘要

1. `cat desktop/version.json` → 确认本地 `force_latest:true`
2. `GET http://8.141.123.141/desktop/version.json` → 确认线上 `force_latest:true`
3. `GET http://8.141.123.141/desktop/latest.yml` → 确认通道最新版 `1.0.40`
4. 阅读 `desktop/lib/health.js:45-50` → 确认 `requiredVersion` 逻辑 = `max(min_version, latest)`
5. 阅读 `desktop/main.js:315-329` → 确认 `checkForceUpdate` 调用链
6. 阅读 `desktop/main.js:742-768` → 确认启动时无绕过路径
7. 阅读 `desktop/main.js:335-350` → 确认登录前二次拦截
