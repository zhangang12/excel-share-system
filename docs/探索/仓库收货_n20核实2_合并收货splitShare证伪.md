# n20 · 核实#2 合并收货 splitShare 死代码 —— 证伪结论

> 核实员: n20 | 说法来源: 前端收货页面
> 默认立场: 证伪优先

## 待核说法

> 合并收货的 splitShare() 函数与 batchRecvForm.total_amount 是死代码残留：batchRecvMode 恒为 'lines'，total 模式 UI 已删，splitShare 全文件无调用点，请求体也不含 total_amount，可清理无行为影响

## 判决: PASS ✅

说法完全成立。n6 已执行清理（未提交），当前代码中四个符号全部移除且无行为影响。

## 核实过程

### 1. 当前文件状态 — 四个符号已全部移除

`git diff frontend/src/views/WarehouseView.vue`（未暂存改动，2026-08-09）：

| 符号 | 清理前代码 | 清理后状态 |
|------|-----------|-----------|
| `batchRecvMode` ref | `const batchRecvMode = ref<'total' \| 'lines'>('lines')` | 已删除 |
| `batchRecvForm.total_amount` | `total_amount: null as number \| null` 在 reactive 中 | 已从 reactive 定义移除 |
| `batchTotalQty` computed | `const batchTotalQty = computed(() => batchRecvLines.value.reduce(...))` | 已删除 |
| `splitShare()` | 7 行函数体（按数量分摊总价） | 已删除 |

辅助改动: 两处 `batchRecvMode.value = 'lines'` 赋值、三处 `total_amount: null` 重置、注释文案「只填合并总价 或 逐行单价」→「逐行填单价/收货金额」、说明文案「只填总价」→「逐行填单价/收货金额」同步更新。

### 2. splitShare 全文件无调用点 — 确认

```
grep splitShare --include="*.vue" → No files found
```

清理前代码中 `splitShare` 仅定义、无任何调用（模板中无 `{{ splitShare }}`、script 中无 `splitShare(` 调用），grep 全前端确认。

### 3. batchRecvMode 只赋值无读取 — 确认

定义 `ref<'total' | 'lines'>('lines')` 初始值即 'lines'。仅有两处赋值在 `openBatchReceive()` 与 `openBatchReceiveGroup()` 中设 `batchRecvMode.value = 'lines'`（始终设同一个值）。全文件无 `batchRecvMode` 的读取（无 `v-if/v-show` 用该值、无 computed 依赖它、无 watch 监听它）。grep 确认清理后全无。

### 4. 请求体不含 total_amount — 确认

`submitBatchReceive()` (当前文件 639-660 行，清理前 644-665 行) 构造 body：

```ts
const body: any = {
  item_ids: batchRecvLines.value.map(l => l.item_id),
  delivery_note_no: batchRecvForm.delivery_note_no || null,
  arrival_date: batchRecvForm.arrival_date,
  stock_location: batchRecvForm.stock_location || null,
  project_code: batchRecvForm.project_code || null,
}
body.lines = batchRecvLines.value.map(l => ({
  item_id: l.item_id, unit_price: l.unit_price, received_amount: l.received_amount
}))
```

body 中从未包含 `total_amount`。后端 `POST /purchase-mgmt/items/receive-batch` (`purchase_mgmt_router.py:2049`) 只接收 `lines` 逐行价，不接收 `total_amount`。前后端契约一致且从未包含 total_amount。

### 5. total 模式 UI 已删 — 确认

模板中（弹窗 `el-dialog`，当前 1963-2010 行区间）为逐行 `el-input-number` 编辑单价/收货金额（`batchRecvLines` 的 v-for），无任何「按总价分摊」的模式切换按钮或总价输入框。整个弹窗 UI 只支持 lines 模式。

## 证据链

| 检查项 | 方法 | 结果 |
|--------|------|------|
| splitShare 调用点 | grep splitShare *.vue | 0 处 |
| batchRecvMode 读取点 | git diff 看清理前的代码 + grep batchRecvMode | 仅 2 处赋值 .value='lines'，0 处读取 |
| batchTotalQty 引用 | grep batchTotalQty *.vue | 0 处（仅被 splitShare 引用） |
| total_amount 发请求 | 读 submitBatchReceive body 构造 | body 从未含 total_amount |
| total 模式 UI | 读弹窗模板 | 只有 lines 逐行编辑 |

## 分歧/遗留

无。清理干净、类型检查通过（n6 确认 `vue-tsc -b` pass）。
