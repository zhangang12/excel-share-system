# n40 · 核实：`_auto_stock_in` 幂等设计不抗并发

> 核实员 | 证伪视角 | 2026-08-09
>
> 要核的说法（来自 n36 融合产物）：
> 「收货自动入库 `_auto_stock_in` 的幂等设计不抗并发：check-then-act 检查 + `WhTxn.purchase_item_id` 仅 index 无唯一约束（models.py:413），两个请求几乎同时收货同一明细→双流水、库存翻倍」

---

## 判决：PASS（说法成立）

---

## 1. 说法逐条核实

### 1.1 `WhTxn.purchase_item_id` 仅 index，无唯一约束

**证据：`models.py:404-426`（WhTxn 全类定义）**

- 第 413 行：`purchase_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("purchase_items.id"), index=True)` —— 有 `index=True`，没有 `unique=True`
- WhTxn 类（404-426 行）**没有定义 `__table_args__`**，即没有任何 `UniqueConstraint`
- 全项目 grep `purchase_item_id.*unique` 无结果，排除任何原始 SQL 迁移建唯一索引的可能

**结论：确认为普通索引，非唯一约束。**

### 1.2 `_auto_stock_in` 使用 check-then-act 幂等检测

**证据：`purchase_mgmt_router.py:1404-1441`（`_auto_stock_in` 完整实现）**

- 第 1409-1412 行：
  ```python
  ex = await db.execute(select(models.WhTxn).where(
      models.WhTxn.purchase_item_id == item.id, models.WhTxn.is_reversal == False))
  if ex.scalars().first():
      return
  ```
- 第 1434-1438 行：若未查到已有流水，则 `db.add(models.WhTxn(...))` 新增一条
- 第 1405-1406 行 docstring 写明："幂等：同一采购明细只过账一次"

**结论：确认为经典 check-then-act 模式，唯一幂等手段即 SELECT 查已有流水。**

---

## 2. 证伪尝试（逐一排除可能的并发保护）

### 2.1 是否存在数据库级唯一约束？

**检查项**：
- ORM 模型 `WhTxn` 无 `__table_args__`（`models.py:404-426` 已通读）
- 全项目 grep `purchase_item_id.*UNIQUE|CREATE.*UNIQUE.*INDEX.*purchase_item_id` 无结果
- 全项目 `UniqueConstraint` 17 处（`models.py` 各处），无一处涉 `wh_txns.purchase_item_id`

**结论：不存在。**

### 2.2 是否存在 `SELECT ... FOR UPDATE` 行锁？

**检查项**：全项目 grep `with_for_update|select.*from_update` → 无结果

**结论：全局无一处在 ORM 查询中使用 `with_for_update()`。**

### 2.3 是否存在应用层锁（asyncio.Lock / 分布式锁）？

**检查项**：
- `purchase_mgmt_router.py` 全文（3335 行）grep `lock|Lock|acquire` → 无相关匹配
- 唯一 `asyncio.Lock` 在 `ws_router.py:19`（WebSocket 房间管理，无关）

**结论：不存在。**

### 2.4 `receive_item` 端点是否有前置检查阻挡并发？

**证据：`purchase_mgmt_router.py:2013-2046`（`receive_item` 完整实现）**

- 第 2022-2025 行：`select(PurchaseItem).where(PurchaseItem.id == iid)` —— 无 `with_for_update`
- 第 2026-2029 行：仅检查 `body.arrival_date` 是否为空（要求必填），**不检查 item 是否已有 arrival_date**
- 第 2043 行：`await _finish_receive(db, item, body.arrival_date, current)` → 最终调 `_auto_stock_in`

**结论：端点的 SELECT 不加行锁，不检查重复收货，SET arrival_date 是盲写覆盖。**

### 2.5 `receive_batch` 端点是否同样脆弱？

**证据：`purchase_mgmt_router.py:2049-2107`**

- 第 2063 行：`select(PurchaseItem).where(PurchaseItem.id.in_(body.item_ids))` —— 批量 SELECT，无 `with_for_update`
- 第 2104 行：逐行 `await _finish_receive(db, it, ...)` —— 每个 item 都独立调 `_auto_stock_in`

**结论：同样脆弱，且因为是循环逐行调用，同一个事务内对同一 item 也可能重复调用（如果前端误传重复 id）。**

### 2.6 数据库会话/事务是否有特殊隔离级别？

**证据：`database.py:28-30`（`get_db` 实现）**

```python
async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
```

- 无 `isolation_level` 设置
- 无 `execution_options` 覆盖
- 每个请求独立 session，独立事务

**结论：使用数据库默认隔离级别。PostgreSQL（生产）= READ COMMITTED，恰好是最容易触发 check-then-act 竞态条件的隔离级别。**

### 2.7 前端是否有防重复提交？

**检查项**：未做前端验证。后端不依赖前端防线，即便前端有防抖/按钮禁用，两个不同用户同时操作或 curl 直接调接口仍可触发。

**结论：不改变后端漏洞的判定。**

---

## 3. 竞态条件的具体时序

假设 T1、T2 是两个并发请求（例如仓库管理员 A 和 B 同时点了同一个采购明细的收货确认按钮）：

| 步骤 | T1 | T2 |
|------|----|----|
| 1 | BEGIN | |
| 2 | SELECT PurchaseItem WHERE id=X → 拿到 item | BEGIN |
| 3 | | SELECT PurchaseItem WHERE id=X → 拿到 item |
| 4 | SET item.arrival_date = "2026-01-01" | |
| 5 | CALL _auto_stock_in(db, item) | SET item.arrival_date = "2026-01-01" |
| 6 | → SELECT WhTxn WHERE purchase_item_id=X → **空** | CALL _auto_stock_in(db, item) |
| 7 | → INSERT WhTxn(purchase_item_id=X) | → SELECT WhTxn WHERE purchase_item_id=X → **空**（T1 未提交） |
| 8 | COMMIT → **一条 WhTxn 落地** | → INSERT WhTxn(purchase_item_id=X) |
| 9 | | COMMIT → **另一条 WhTxn 落地** |

**结果：`purchase_item_id=X` 对应两条 `direction='in'` 的库存流水，库存计算翻倍。**

> 注：PostgreSQL READ COMMITTED 下，T2 的 SELECT（步骤 7）看不到 T1 未提交的 INSERT（步骤 7），这是导致竞态的关键。

---

## 4. 被排除的路子

| 被排除的假设 | 排除依据 |
|-------------|---------|
| 可能有 DB 层 unique constraint 没反映在 ORM | 全项目 grep `purchase_item_id.*UNIQUE` 无结果；WhTxn 无 `__table_args__` |
| 可能有 SELECT FOR UPDATE 行锁 | 全项目 grep `with_for_update` 无结果 |
| 可能有 asyncio.Lock 保护 | 全 purchase_mgmt_router.py 无 lock 相关代码 |
| `receive_item` 可能先检查 arrival_date 已填就拒绝 | `purchase_mgmt_router.py:2026-2029` 无此逻辑 |
| 事务隔离级别可能不同 | `get_db`（database.py:28-30）无任何覆盖 |

---

## 5. 证据清单

| 说法中的要素 | 核实结果 | 证据 |
|-------------|---------|------|
| check-then-act 幂等检查 | ✅ 存在 | `purchase_mgmt_router.py:1409-1412` |
| purchase_item_id 仅有 index | ✅ 确认 | `models.py:413`: `index=True` 无 `unique=True` |
| 无 DB 唯一约束 | ✅ 确认 | WhTxn 类（`models.py:404-426`）无 `__table_args__` |
| 无行级锁 | ✅ 确认 | 全项目 `with_for_update` 0 匹配 |
| 无应用锁 | ✅ 确认 | `purchase_mgmt_router.py` 全文无 lock |
| 默认隔离级别 | ✅ 确认 | `database.py:28-30` 无覆盖 |
| 可被两个并发请求触发 | ✅ 确认 | 时序分析见第 3 节 |

---

## 6. 分歧/遗留

无。

