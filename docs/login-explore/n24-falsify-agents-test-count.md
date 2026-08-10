# n24 · 核实#1 · 证伪 AGENTS.md [PARTIAL]

> 任务：核实 AGENTS.md "已知坑" 段落声称"13 个测试在基线 HEAD 上就挂"但清单实际列出 14 个名字的说法。
> 默认立场：证伪；即先认为这条是错的。

## 1. 一句话结论

AGENTS.md 第 84 行**声称 13 但列出 14**，计数自相矛盾。数字 "13" 是错的，"14" 更接近真实情况（但也不一定准确——本片未跑测试，无法确认到底"挂"了多少个）。判决：**PARTIAL**（事实基础成立，数字不可信）。

## 2. 证据链

### 2.1 AGENTS.md 原文（第 84 行）

```
**13 个测试在基线 HEAD 上就挂**（历史欠账，与新改动无关）：`m01`(剩4个#91详单闸门)`/m02/m04/m07/m08/m12/m13/m14/m15`、e2e 两个、`outsourcing_template`、`user_feedback`、`void_sales_order`。
```

### 2.2 列出的测试名称逐个拆解

| 序号 | 名称 | 解析说明 |
|------|------|----------|
| 1 | `m01` | m01 是一个测试文件名，括号 "剩4个#91详单闸门" 是补充说明内部挂了多少个测试方法，不是独立测试名 |
| 2 | `m02` | |
| 3 | `m04` | |
| 4 | `m07` | |
| 5 | `m08` | |
| 6 | `m12` | |
| 7 | `m13` | |
| 8 | `m14` | |
| 9 | `m15` | |
| 10 | e2e（第一个） | 对应 `test_e2e_full_lifecycle.py` |
| 11 | e2e（第二个） | 对应 `test_e2e_business_flows.py` |
| 12 | `outsourcing_template` | 对应 `test_outsourcing_template.py` |
| 13 | `user_feedback` | 对应 `test_user_feedback.py` |
| 14 | `void_sales_order` | 对应 `test_void_sales_order.py` |

**合计 14 个名称，不是 13 个。**

### 2.3 文件存在性验证

glob 查询 `backend/tests/` 下对应的测试文件，14 个名称全部存在：

| 名称 | 实际文件路径 |
|------|-------------|
| m01 | `backend/tests/test_m01_roles_menus.py` |
| m02 | `backend/tests/test_m02_sales.py` |
| m04 | `backend/tests/test_m04_orders.py` |
| m07 | `backend/tests/test_m07_warehouse.py` |
| m08 | `backend/tests/test_m08_logistics_e2e.py` |
| m12 | `backend/tests/test_m12_detail.py` |
| m13 | `backend/tests/test_m13_feedback.py` |
| m14 | `backend/tests/test_m14_reports.py` |
| m15 | `backend/tests/test_m15_overdue.py` |
| e2e(1) | `backend/tests/test_e2e_full_lifecycle.py` |
| e2e(2) | `backend/tests/test_e2e_business_flows.py` |
| outsourcing_template | `backend/tests/test_outsourcing_template.py` |
| user_feedback | `backend/tests/test_user_feedback.py` |
| void_sales_order | `backend/tests/test_void_sales_order.py` |

### 2.4 交叉验证：n7 产出文档也注意到同一矛盾

`docs/login-explore/login-tests.md:76`：

> **数量矛盾（待核实）**：AGENTS.md 标题称"**13 个**测试在基线 HEAD 上就挂"，但清单实际罗列 **14 个名字**（9 个 m 系 + 2 个 e2e + outsourcing_template + user_feedback + void_sales_order）。差额可能是某名字已在后续修复但 AGENTS.md 未更新。

两个独立核实节点得出同样的观测，不是计数幻象。

### 2.5 可能的原因推测（排除）

排除以下猜想：
- **"m01(剩4个)" 被当成了 4 个测试**：如果有人把 m01 括号里的 "剩4个" 理解为 m01 占了 4 个名额，那么 9 个 m 系文件名里有 4 个来自 m01 → m01/02/04/07/08/12/13/14/15 变成 4+1+1+1+1+1+1+1+1 = 12，加上 e2e*2+out+uf+void = 12+2+1+1+1=17，不可能凑出 13。此路径排除。
- **"e2e 两个" 被算成了 1 个**：如果只把 e2e 当一个名字，那么 9+1+1+1+1 = 13。**这可能是最合理的解释**——"e2e 两个" 被某人当成一个条目计数，实际是两个独立的测试文件。
- **mr_probe_menu 或 smoke_startup 曾计入**：假设原始清单 16 个（14 + mr_probe_menu + smoke_startup），修好 mr_probe_menu 和排除 smoke_startup 后变 14 个，但手误写成了 13。**不大可能**——手误如果来自减法，16-2=14 才对，不会得 13。

**最合理的解释**：某次编辑时，把 "e2e 两个" 当 1 个名字数了（9 + 1 + 1 + 1 + 1 = 13），但实际是 2 个独立的 e2e 测试文件（9 + 2 + 1 + 1 + 1 = 14）。

## 3. 判决

**PARTIAL**——AGENTS.md "基线 HEAD 上挂测试" 的事实基础成立（14 个测试文件确实存在于 tests 目录，且被称为历史挂测），但声称的数字 "13" 与自身列出的 14 个名字自相矛盾，13 这个数字并不可信。正确数字应为 14（或需跑测试才能最终确认）。

## 4. 反例与排除

- 排除 "我数错了 / m01 括号 '剩4个' 该拆成 4 个"：详见 §2.5。
- 排除 "某文件已删除 / 改名导致数字对不上"：14 个文件全部当前存在。
- 未跑测试，不确认 14 个是否全挂 / 是否有应从清单移出的（例如 smoke_startup 已被指出 "旧清单偏旧"，不排除清单中也有其他已修复未更新的）。
