# n10 · 验收汇总 · 登录分析文档

**判决：PASS**

**日期**：2026-08-09

---

## 第一问：文档够不够格

### 核 3 条关键结论

**#1 免闸顺序（§3.1）**

- 出处：auth_router.py:86-108
- 实际查看：第92行 `if not u.has_role("admin")` → 第94-97行 `exempt = is_intranet(...) or desktop_exempt(...)` → 第98行 `if enabled and not exempt`
- 观察：代码中 intranet 与 desktop 是同级 OR，不是两个独立优先级。但功能上 admin > (intranet OR desktop) > gate_enabled 的顺序正确
- **PASS**（`git diff HEAD` 确认没被改过）

**#2 fail_count 注释过时（§9.2）**

- 出处：models.py:1204
- 实际查看：`git diff HEAD -- backend/app/models.py` 输出：
  ```
  -    fail_count: Mapped[int] = mapped_column(default=0)  # 连续错码次数（>=5 锁定）
  +    fail_count: Mapped[int] = mapped_column(default=0)  # 错码次数（仅计数，不锁定；2026-07-28 起）
  ```
- 观察：HEAD 已提交版仍写 `>=5 锁定`（过时），工作树有一笔未提交修复。文档所述**对已提交代码成立**
- **PASS**

**#3 AppSetting docstring 过时（§9.2）**

- 出处：models.py:1145-1148
- 实际查看：`"""通用 kv 配置表（key 主键）。目前仅 Agent 助手用来存 LLM 配置"""` → gate 四键也存此表（gate.py:96-127），docstring 确实过时
- **PASS**

### 空话检查

全文 grep "存在一些|建议进一步|可以优化|待优化" → **零命中**。文档用代码行号/接口名说话，无管理空话。

### 交叉印证检查

§13 有逐项 6 片对照表（免闸顺序/JWT有效期/IP取址链/verify-gate/X-PMS-Client/无refresh token/种子账号/H5耦合等共 13 项），不是罗列——每项标出各片口径并判一致性。结论有据。

---

## 第二问：对照总目标还漏什么

### 未提及的主题

| 缺失点 | 说明 |
|--------|------|
| 密码重置机制 | 全仓 grep `password.*reset`/`forgot.*password` 零命中——系统无密码重置功能，文档未提及此缺失 |
| 同账号并发登录 | gate.py:139 "同一账号同时只有一个有效码" 仅限发码阶段，JWT 无会话表/并发控制，未分析 |

这两个属于边界场景，不影响核心分析完整性。

### 待核实结论

全部主要结论都有文件:行号出处，或标注"上游已核实"（n30/n31/n32 等核验节点）。无未经任何人核实的核心断言。

### 各片矛盾抹平检查

§13 表中标记 `nginx 限频只挂 /login` 一项"待核实"——此非抹平，是明示未交叉验证。未见其他被抹平的矛盾。

### 一处技术不精确

§9.3 首句"issue_code add 码行后不显式 commit，依赖调用方的 audit commit 副作用将行真正写入"：正常路径下 push_message（notify.py:76）在 issue_code 内部即 commit，并非依赖外部 write_audit。但在 edge case（manager 无 active 用户）下确实依赖 write_audit。n30 已详析此口。文档附带流程图中标注了 "若无 manager active → early return, 不 commit"，此不精确不影响风险结论。

---

## 结论

- 3 条关键结论回源码核实，全部成立
- 无管理空话、有真正的交叉印证（§13 对照表）
- 缺失点（密码重置、并发登录）属边界，不影响"分析登录逻辑"目标的核心覆盖率
- 仅 §9.3 一处措辞可更严谨，属轻微不精确

**Verdict: PASS**
