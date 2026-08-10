# n11 · 核实 _auto_stock_in docstring 陈旧

## 判决：PASS（说法成立）

## 核实的说法

> _auto_stock_in 的 docstring 仍是旧语义（写着 is_stock=False 时自动出库），实际代码已删除自动出库，收货一律只入库；看注释会误以为收货会生成出库流水

## 做了什么

1. 打开 `backend/app/routers/purchase_mgmt_router.py`，读 `_auto_stock_in` 函数（行 1404-1440）
2. 核对 docstring 与函数体的差异
3. grep 全路由文件确认 `is_stock` 引用分布，交叉验证废弃状态

## 观察到的事实

### ① docstring 确实写的是旧语义

```python
# purchase_mgmt_router.py:1405-1406
"""采购收货 → 自动入库（带采购单价/金额）；非备货(is_stock=False)再自动一笔「采购领用」出库
(直发对应项目，净库存过账为0)；备货(is_stock=True)只入库、留库存。幂等：同一采购明细只过账一次。"""
```

docstring 描述了两条逻辑路径：
- `is_stock=False` → 入库 + 自动出库（采购领用），净库存为 0
- `is_stock=True` → 只入库

### ② 函数体已不包含任何自动出库逻辑

函数体（行 1407-1438）全程只做：
- 检查 qty > 0
- 检查 `purchase_item_id` 幂等（已有流水则 return）
- 查找/创建 WhMaterial
- 查项目 ID
- 创建一条 `direction="in"` 的 WhTxn（入库流水）

**没有**任何对 `item.is_stock` 的判断，**没有**创建 `direction="out"` 的流水。

### ③ 有明确注释承认已删除自动出库

```python
# purchase_mgmt_router.py:1439-1440
# 🆕 库位管理批次：**取消收货自动出库**（原 is_stock=False 自动生成「采购领用」出库已删）——
#   收货一律只入库到所选库位;出库统一走仓库领料(出入库登记/物料需求一键领用),挂项目计成本。
```

### ④ `is_stock` 字段已全局标记废弃

全路由文件 7 处 `is_stock` 引用中，3 处明确标记废弃：

| 行号 | 内容 |
|------|------|
| 811 | `# is_stock 已废弃(收货一律只入库);兼容旧前端仍传值,不再落库为 False` |
| 1213 | `# is_stock 已废弃:收货一律只入库(默认True)` |
| 1380 | `# is_stock 已废弃:收货一律只入库(默认True)` |

唯一 caller 是 `_finish_receive`（行 2006），仅传 `(db, item, current)`，不涉及 is_stock 判断。

## 结论

说法完全成立：docstring（行 1405-1406）描述了两个分支含自动出库，实际代码（行 1407-1438）已删除了 `is_stock=False` 的自动出库路径，仅有行 1439-1440 的注释说明了删除原因。任何只看 docstring 的人都会被误导以为收货会为非备货项生成出库流水。

## 分歧/遗留

无。证据链完整闭合——docstring 文本、函数体逻辑、删除注释、全路由 `is_stock` 废弃标记四者一致印证。
