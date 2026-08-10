# n15 · 核实 #1 · 证伪：收货清单 GET /receiving 不套 _buyer_restricted

## 要核的说法

> 收货清单 GET /receiving 完全不套 _buyer_restricted，warehouse 角色能看所有采购员下的全部明细（含待收货/已收货），而采购员自己的 GET /items 被收窄到 buyer_id==本人——同一张 PurchaseItem 表两套行级谓词，仓库可见范围比采购员本人还大

## 判决：PASS（说法完全成立）

## 核证过程

### 1. 检查 `/receiving` 端点（purchase_mgmt_router.py:1881-1957）

- **入口处权限校验**（line 1890）：`current: models.User = Depends(require_roles(*_RECEIVE_ROLES))` —— 仅限 warehouse / warehouse_lead 角色访问，admin/manager 由 require_roles 自动放行。
- **SQL 构造**（lines 1894-1921）：全函数从头到尾 **没有任何 `_buyer_restricted(current)` 调用**，也没有 `buyer_id == current.id` 的 WHERE 条件。
- WHERE 子句清单（line 1898-1920）：
  - `arrival_date IS NULL` / `IS NOT NULL`（根据 received 参数）
  - `supplier_id == supplier_id`（供应商过滤）
  - `po_no ILIKE`（采购单号模糊）
  - keyword 三向分支（po_no / project_code / source_sheet_id 经子查询）
  - `item_name ILIKE`（物料名称模糊）
  - `delivery_date START WITH`（下单月份）
  - `LIMIT limit`
- **结论**：对 warehouse / warehouse_lead 角色，该查询返回 PurchaseItem 表全量数据（受上述过滤条件约束），**不做任何按 buyer_id 的行级收窄**。

### 2. 检查 `/receiving/meta` 端点（purchase_mgmt_router.py:1960-1989）

- 角色校验同 `/receiving`：`require_roles(*_RECEIVE_ROLES)`（line 1962）。
- 两个内部函数 `_count(pending)`（line 1969-1973）和 `_suppliers(pending)`（line 1975-1981）均直接对 PurchaseItem 表做 COUNT / DISTINCT 查询，**同样无 buyer_id 过滤**。
- **结论**：`/receiving/meta` 同口径，也对 warehouse 角色返回全量。

### 3. 对比 `/items` 端点（purchase_mgmt_router.py:370-400）

- **入口处权限校验**（line 372）：`require_roles(*_PURCHASE_ROLES)` —— buyer 家族 + finance。
- **行级收窄**（lines 380-381）：
  ```python
  if _buyer_restricted(current):
      stmt = stmt.where(models.PurchaseItem.buyer_id == current.id)
  ```
  受限采购员只能看到 `buyer_id == 本人` 的明细。
- **结论**：同一张 PurchaseItem 表，`/items` 对 buyer 角色收窄到本人，`/receiving` 对 warehouse 角色无任何收窄。

### 4. 检查 `_buyer_restricted` 定义（purchase_mgmt_router.py:46-53）

```python
def _buyer_restricted(current: models.User) -> bool:
    return current.has_role("buyer", "buyer_standard", "buyer_outsource") \
           and not current.has_role("buyer_lead", "admin", "manager")
```

- warehouse / warehouse_lead 角色不在 buyer 家族 → `_buyer_restricted(warehouse_user)` 返回 `False`。
- 但对 `/receiving` 来说这无关紧要：该函数**根本没有调用** `_buyer_restricted`。

### 5. 检查 `_RECEIVE_ROLES` 定义（purchase_mgmt_router.py:28）

```python
_RECEIVE_ROLES = ("warehouse", "warehouse_lead")
```

- 仅仓库角色。admin/manager 由 require_roles 自动放行。

## 反例尝试（已排除）

### 试证伪路径 1：是否有其他隐式 buyer 过滤？

扫描 `/receiving` 函数全文（lines 1881-1957），WHERE 子句只有：arrival_date、supplier_id、po_no、keyword（project_code/po_no/source_sheet_id）、item_name、delivery_date、LIMIT。**无任何 buyer_id 或 buyer 相关过滤**。

### 试证伪路径 2：是否 `_item_out()` 中有过滤？

`_item_out(i)` 是纯序列化函数（PurchaseItem → PurchaseItemOut），不做行级过滤。返回的 `List[PurchaseItemOut]` 含单价/总价/供应商全字段。

### 试证伪路径 3：是否 `require_roles` 内部对 warehouse 有额外限制？

`require_roles` 实现于 `backend/app/deps.py:52-62`，语义为多角色并集 + admin/manager 恒放行。warehouse 角色通过 `require_roles(*_RECEIVE_ROLES)` 校验后，不做任何额外行级限制。

### 试证伪路径 4：是否 `/receiving` 走 `_RECEIVE_ROLES` 但 warehouse 本身被其他机制限制？

- `_buyer_restricted` 未调用（已确认）。
- 无其他行级谓词被调用。
- **结论**：此路径同样证伪失败。

## 分歧/遗留

无。说法与代码完全吻合。

## 证据汇总

| 事实 | 证据 |
|---|---|
| `/receiving` 未调用 `_buyer_restricted` | purchase_mgmt_router.py:1881-1957（全文扫描，0 次 `_buyer_restricted` 出现） |
| `/receiving` 仅 `_RECEIVE_ROLES` 门控，无 buyer_id 过滤 | purchase_mgmt_router.py:1890, 1894-1920 |
| `/receiving/meta` 同口径无 buyer 过滤 | purchase_mgmt_router.py:1962, 1969-1981 |
| `/items` 有 `_buyer_restricted` → buyer_id 收窄 | purchase_mgmt_router.py:380-381 |
| `_buyer_restricted` 判定语义 | purchase_mgmt_router.py:46-53 |
| `_RECEIVE_ROLES` = warehouse 独享 | purchase_mgmt_router.py:28 |
| n7 文档同样已记录此事实 | docs/探索/仓库收货_05权限.md:32, 110 |
