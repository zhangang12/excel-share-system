# 核实 #1·可复现 — 收货自动入库 _auto_stock_in 并发竞态

> 核实节点：n41 · 核实员（可复现视角）
> 判决时间：2026-08-09

## 被核说法

> 收货自动入库 `_auto_stock_in` 的幂等设计不抗并发：check-then-act 检查 + `WhTxn.purchase_item_id` 仅 index 无唯一约束（`models.py:413`），两个请求几乎同时收货同一明细 → 双流水、库存翻倍

**判决：PASS — 成立且可复现。**

## 核实过程（didWhat）

1. 阅读 `_auto_stock_in` 实现（`purchase_mgmt_router.py:1404-1440`）：确认为 check-then-act 模式——先 `SELECT WhTxn WHERE purchase_item_id==X AND is_reversal==False`，有则 return，无则 INSERT
2. 阅读 `WhTxn` 表定义（`models.py:413`）：`purchase_item_id` 仅有 `index=True`，无 `unique=True` 约束
3. 确认两个调用方：`receive_item`（单条）和 `receive_batch`（批量）均调用 `_finish_receive` → `_auto_stock_in`，且均无 upstream 重复提交防护
4. 编写并发复现脚本：自建内存 SQLite 库，创建一条未收货 PurchaseItem，两个协程同时调用 `_auto_stock_in`
5. 执行结果：**产生 2 条 WhTxn 流水**（`id=1, id=2`），库存入库记录翻倍

## 复现脚本输出（observed）

```
[A] commit OK
[B] commit OK

=== 结果 ===
WhTxn 入库流水数: 2
  id=1 qty=10.0 amount=1000.0
  id=2 qty=10.0 amount=1000.0

结论: 🔴 竞态复现(PASS)
```

## 触发条件

| 条件 | 说明 |
|------|------|
| 数据库 | PostgreSQL（生产，READ COMMITTED 隔离）或 SQLite（开发）均可触发 |
| 并发请求 | 两个 HTTP 请求同时到达，操作同一 `purchase_item_id` |
| 时序 | 两个请求的 SELECT WhTxn 均在对方 INSERT 提交之前完成 |
| 前端场景 | 最可能：收货按钮快速双击/连点（前端无 debounce/disable 防护——`WarehouseView.vue` 仅搜索框有防抖，收货按钮无） |

### 触发路径

**路径1（最常见）**：仓库收货弹窗，用户双击"确认收货"按钮
→ 两个 `PUT /items/{iid}/receive` 几乎同时到达
→ 各自 `SELECT PurchaseItem` → 各自修改 `arrival_date`
→ 各自 `SELECT WhTxn` → 都看到空结果（对方未提交）
→ 各自 `INSERT WhTxn` → **两条入库流水**

**路径2**：两仓管同时打开同一未收货明细，各自点收货
→ 同上（`list_receiving` 过滤 `arrival_date IS NULL`，但两人打开后一人收完另一人页面未刷新时仍可提交）

**路径3**：批量收货 `POST /items/receive-batch` 快速双击发送两批有重叠 item_ids 的请求

## 影响评估

| 影响面 | 严重度 | 说明 |
|--------|--------|------|
| 库存流水（WhTxn） | **高** | 同一笔收货产生多条入库记录，数量/金额翻倍 |
| 实时库存（_stock_map） | **高** | `_stock_map = init_stock + Σin − Σout`，in 侧翻倍 → 库存虚高 |
| 项目材料成本 | **中** | 下游按 WhTxn 汇总材料成本时虚高 |
| 采购收货金额统计 | **中** | 按 WhTxn 汇总的入库金额翻倍 |

**与原始说法比**：严重度一致（原始说法判"高"），竞态导致的库存翻倍确实属于高影响。

## 为什么现实中可能没被报告

1. **概率低**：需要精确的双击时序窗口（两个请求的 SELECT 都必须在对方 INSERT 之前）
2. **SQLite 生产不常用**：生产是 PostgreSQL，READ COMMITTED 下窗口存在但仍是小概率事件
3. **用户不双重点击**：多数用户习惯单点，不连点
4. **不易察觉**：用户收货后看到"收货成功"，不会去核对 WhTxn 表；库存翻倍是一次性的，后续出货扣减可能掩盖

## 修复方向（仅建议，不属本核实员职责范围）

1. **数据库层（首选）**：给 `WhTxn.purchase_item_id` 加 `unique=True`（需处理已有的 NULL 值——当前列允许 NULL，SQLite/PostgreSQL 对多 NULL 处理不同）
2. **应用层**：在 `receive_item` / `receive_batch` 入口检查 `arrival_date IS NOT NULL` 则拒绝（已收货不可再收）
3. **前端**：收货按钮点击后 disabled + loading，防双击

## 反例排查

以下路径经排查**不成立**：

- **"SQLite 串行化写锁会阻止竞态"**：不成立。aiosqlite 使用 `check_same_thread=False` 的多连接模式，两个独立 session 各自 DEFERRED 事务，SELECT 不拿写锁，两条 SELECT 均通过后各自 INSERT → 双流水。复现脚本实测证实。
- **"arrival_date 已填写会阻止第二次收货"**：不成立。`receive_item` 端点不检查已有 `arrival_date`，直接覆盖写入（`item.arrival_date = body.arrival_date`），然后仍调用 `_auto_stock_in`。虽然 `_auto_stock_in` 的 check-then-act 在无并发时能挡，但并发窗口内仍然穿过。
- **"前端防抖/按钮 disabled 已有防护"**：不成立。`WarehouseView.vue` 仅搜索框有防抖（第 187 行 `// 搜索走后端，输入时防抖`），收货提交按钮无加载态/disalbed 逻辑。

## 引用文件

- `backend/app/routers/purchase_mgmt_router.py:1404-1440` — `_auto_stock_in` 函数定义（check-then-act）
- `backend/app/routers/purchase_mgmt_router.py:1992-2010` — `_finish_receive` 调用方
- `backend/app/routers/purchase_mgmt_router.py:2013-2046` — `receive_item` 端点
- `backend/app/routers/purchase_mgmt_router.py:2049-2107` — `receive_batch` 端点
- `backend/app/models.py:413` — `WhTxn.purchase_item_id` 定义（`index=True` 无 `unique=True`）
- `backend/app/models.py:405-424` — `WhTxn` 完整表定义
- `backend/app/database.py:10-14` — engine 创建（未设置隔离级别，默认 READ COMMITTED）
