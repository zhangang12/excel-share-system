# n13 核实：_sync_txn_amount 的 else 分支是否为死代码

**判决：FAIL（说法不成立）**

## 原说法

> _sync_txn_amount 的「部分入库」分支（t.qty != item.qty 时退回 qty×unit_price）在当前代码下是死代码，收货路径只有整条过账；若未来引入部分收货，此分支会成为金额口径分叉点
> 证据: backend/app/routers/purchase_mgmt_router.py:1456-1459

## 核实过程

### 1. 理解 _sync_txn_amount 的逻辑

`purchase_mgmt_router.py:1443-1459`：遍历该明细对应的所有非冲红 WhTxn 流水，对每条流水：
- 若 `received_amount` 不为 None 且 `t.qty == item.qty` → 金额 = received_amount
- 否则（`t.qty != item.qty` 或无 received_amount）→ 金额 = t.qty × unit_price

### 2. 调用点分析

`_sync_txn_amount` 被两处调用：

| 位置 | 上下文 |
|------|--------|
| `_finish_receive` 第 2009 行 | 紧跟在 `_auto_stock_in` 之后，刚生成的流水 t.qty 必然等于 item.qty |
| `update_item` 第 1823 行 | 当 qty/unit_price/received_amount 任一字段被修改时触发 |

### 3. update_item 允许收货后改 qty

`schedules.py:1357` — `PurchaseItemUpdate.qty: Optional[float]` 可写字段。

`purchase_mgmt_router.py:1807-1823` — `update_item` 仅 pop 掉 `arrival_date`（防采购员绕过收货流程），**未阻止修改已收货明细的 qty**。当 qty 变化时，第 1822-1823 行调用 `_sync_txn_amount`。

### 4. else 分支的可触达路径

触发条件：明细已收货（存在 WhTxn 流水）→ 用户通过 `update_item` 修改 qty → `_sync_txn_amount` 检索到原有流水。

此时：
- `t.qty` = _auto_stock_in 创建时的旧 qty（不变，因为 _auto_stock_in 幂等，不会重新生成流水）
- `item.qty` = 新修改的 qty（第 1816 行 setattr 已生效）
- `t.qty != item.qty` 成立 → 进入 else 分支（`purchase_mgmt_router.py:1458-1459`）

**此分支在当前代码下完全可触达，不是死代码。**

### 5. 关于"金额口径分叉点"的判断

原说法中关于"未引入部分收货，此分支会成为金额口径分叉点"的风险判断属于**未来设计风险评估**，不在本次"死代码"证伪范围内。当前核实仅针对"else 分支是否为死代码"这一可核事实。

## 证据清单

| 证据 | 位置 |
|------|------|
| `_sync_txn_amount` 的 else 分支定义 | `purchase_mgmt_router.py:1456-1459` |
| `_sync_txn_amount` 在 update_item 中的调用 | `purchase_mgmt_router.py:1822-1823` |
| `PurchaseItemUpdate.qty` 可写 | `schemas.py:1357` |
| `update_item` 未阻止已收货明细改 qty | `purchase_mgmt_router.py:1807-1816`（仅 pop arrival_date） |
| `update_item` setattr 后 item.qty 已变 | `purchase_mgmt_router.py:1815-1816` |
| `_auto_stock_in` 幂等，不会因 qty 变化重生成流水 | `purchase_mgmt_router.py:1409-1412` |

## 反例（排除的不成立路径）

- **排除"update_item 不允许改 qty"**：`PurchaseItemUpdate.qty` 是可写字段，`update_item` 中无守卫逻辑阻止已收货明细的 qty 修改。
- **排除"update_item 不会触发 _sync_txn_amount"**：第 1822 行明确检查 `"qty" in data`。
- **排除"流水会随 qty 变化重新生成"**：`_auto_stock_in` 第 1411-1412 行幂等检查，已有流水直接 return。
