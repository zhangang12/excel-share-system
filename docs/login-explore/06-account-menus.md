# 06 · 账号 / 菜单 / 角色建模

> 登录链路中的"账号是什么样、凭什么是这个账号能看这些"这一层。认证签发（JWT）与登录闸门见 01/02 篇，本文只讲**数据模型与可见性判定**。
> 口径时间：2026-07-21 起一级菜单按账号配置，角色菜单矩阵废除。

## 1. 账号（User）数据模型

### 1.1 users 表（`backend/app/models.py:39-101`）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| username | String(64) unique | 登录名 |
| full_name / email | String(64/128) 可空 | |
| password_hash | String(255) | bcrypt 60 位串（见 §2） |
| password_must_change | bool 默认 False | **首登强制改密**：建号/seed 置 True，改密后置 False |
| role_id | FK roles.id | **锚点角色**：存量兼容/展示，恒等于该用户角色之一 |
| is_active | bool 默认 True | 停用账号登录直接被拒（deps.py:26-27） |
| wxid | String(64) 可空 | 企微 userid（手动绑定；空=未绑定，推送降级站内） |
| can_export | bool 默认 False | 导出权限（审批通过后永久放行；管理层天然有） |
| hidden_tabs | JSON 可空 | 按账号隐藏的二级菜单 tab key（如 `finance:pay_payment`） |
| grant_menus | JSON 可空 | **2026-07-21 起停用**：存量值已由迁移并入 menus，读取侧只保留列不删 |
| menus | JSON 可空 | **一级菜单 key 完整清单**（业务+管理组混合）；NULL=未配置 |
| deputy_uid | FK users.id 可空 | OA 审批代理人（指定到人步骤卡死时此人可批） |
| created_at / last_login | DateTime | |

### 1.2 多角色机制（平等，无主次）

- `user_roles` 关联表（`models.py:31-36`）：`UniqueConstraint(user_id, role_id)`，一个用户可配多个角色，**权限取并集**。
- `User.role_id` 仅是存量兼容锚点，**新逻辑一律读 `role_codes` / `role_ids` / `has_role()`**（`models.py:28-29` 注释）。
- `User.roles` 是 viewonly 只读关系（selectin 预加载）；写入统一走 user_roles 关联表（Core），避免 async 下同步 lazy-load 触发 MissingGreenlet（`models.py:71-76`）。

### 1.3 权限判定入口（全系统统一）

```python
# models.py:79-89
role_codes: set[str] = {r.code for r in roles} ∪ {锚点 role.code}
if "finance_lead" in codes: codes.add("finance")   # 财务主管 ⊇ 财务，一处实现全局生效
# models.py:99-101
def has_role(*codes) -> bool: return bool(self.role_codes & set(codes))
```

`finance_lead` ⊇ `finance` 是硬编码隐含（`models.py:85-88`）：财务主管自动拥有财务的一切能力（菜单/请款审批/付款/售后费用），前端 hasRole、require_roles("finance") 全部生效。

## 2. 密码哈希（`backend/app/auth.py:9-23`）

- **bcrypt**，非 PBKDF2/argon2：`hash_password` = `bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt())`，`verify_password` = `bcrypt.checkpw`，异常一律返回 False（不抛）。
- 存库串为 bcrypt 60 位密文；明文不落库。改密点：`auth_router.py:160`（本人改）、`admin_router.py:239`（管理端重置）。

## 3. Seed 账号（`backend/app/seed.py`）

启动幂等创建，仅当不存在时建：

| 账号 | 密码 | 密码来源 | 角色 | 说明 |
|---|---|---|---|---|
| admin | admin123 | `config.py:33-34` `default_admin_password`（可用环境变量覆盖） | admin | "系统全部权限"，密码也可覆盖 |
| manager | manager123 | seed.py:96 硬编码 | manager | 日常使用账号，"admin 仅用于系统底层"（seed.py:87 注释） |

- 两个 seed 账号都设 `password_must_change=True`（`seed.py:73,97`）——**首次登录必须改密**；生产环境若没改过密，admin123 就是真口令，不是演示玩具。
- **seed 只设 role_id，menus 为 NULL**（`seed.py:69-76,93-100`）——对 admin/manager 无影响，因为菜单全量 bypass 不读 menus 列（见 §4）。

### 3.1 角色表（ROLES 清单，`seed.py:14-42`）

31 个角色，seed 幂等 upsert：只同步 name/description，`can_push` 仅在空值时初始化（不覆盖权限页手改）。结构：

- 老 8 角色（红线只增不改）：admin / manager / designer / production_clerk / warehouse / buyer_standard / buyer_outsource / hr
- v3 增量（seed.py:24-41）：sales / sales_lead / design_lead / electrician / electric_lead / assembler / pm_lead / sheetmetal / sealing / buyer / buyer_lead / warehouse_lead / logistics / finance / finance_lead / as_worker / as_lead
- 曾存在的 fitter（钳工）已撤（反馈#304），图纸改推装配组。

### 3.2 roles 表结构（`models.py:18-25`）

`id / code(unique) / name / description / can_push`（消息推送人标记：主管类角色收逾期/预警推送，权限管理页可改）。

## 4. 菜单可见性 —— 唯一权威 `user_menu_keys()`

### 4.1 运行时口径（`menus.py:142-154`）

```python
def user_menu_keys(user) -> list[str]:
    codes = user.role_codes
    if codes & {"admin", "manager"}:
        return _ALL_KEYS + _ADMIN_KEYS                    # 全量，不读 User.menus
    configured = user.menus if user.menus is not None else DEFAULT_ACCOUNT_MENUS
    return canonical_menu_order(set(configured) | {"agent"})
```

- **admin/manager：业务 19 key + 管理组 9 key 全可见**（前端经 `GET /api/auth/menus` 渲染）。
- **其余账号：`User.menus` 即完整清单**；NULL 兜底 `DEFAULT_ACCOUNT_MENUS = ["catalog", "list", "messages", "oa"]`（`menus.py:58`）。
- **`agent`（AI 助手）全员无条件追加**——所有登录用户都看得到，查询权限在 agent_router 按菜单门控。
- 输出按 `canonical_menu_order` 排：业务 key 按 MENU_DEFS 顺序、管理组 key 按 ADMIN_MENU_DEFS 顺序排尾、无效 key 丢弃（`menus.py:125-129`）。

### 4.2 菜单定义

- `MENU_DEFS`（`menus.py:19-39`）：19 个业务菜单 —— catalog 项目目录 / list 项目详单 / sales / leads / design / electric / produce / purchase_mgmt / warehouse / logistics / finance / aftersales / hr / report / oa / agent / messages。
- `ADMIN_MENU_DEFS`（`menus.py:42-52`）：9 个管理组菜单 —— admin-users / admin-perms / admin-audit / dict-admin / approve / wxbind / user-feedback / desktop / gate-config，admin+manager 专属。

### 4.3 ROLE_DEFAULT_MENUS —— 仅建号/迁移默认值，运行时**不读**

- 内容 = 已废除的原角色菜单矩阵，原样保留（`menus.py:60-93`），admin/manager 不在表内（=全可见）。
- 唯一读取点：`default_menus_for_roles()`（`menus.py:132-139`），做 **role_code → ROLE_DEFAULT_MENUS 并集 ∪ {messages, oa}**，未知角色按 `["catalog", "list"]` 老默认。
- 仅在两处被调：**建号预填**（admin_router.py:179-181）、**存量迁移 backfill**。

### 4.4 建号 / 改号流程

- **建号**（`admin_router.py:160-187`）：`menus = default_menus_for_roles(所选角色并集)`，admin/manager 不预填；新用户自动加入所有存量活跃项目为 edit 成员；`password_must_change=True`。
- **改角色不联动菜单**——User.menus 是建号时的快照，之后由管理端「菜单权限」弹窗（`PUT /admin/users/{uid}/menus`）按账号调整。
- 前端 `MainLayout.vue` 全部菜单项（含管理组硬编码项）已 menus 驱动；前端 auth store 的 `isAdmin` = admin **或** manager。

### 4.5 二级菜单（tab）授权

- `TAB_REGISTRY`（`menus.py:99-115`）：purchase_mgmt / finance / hr / warehouse 四菜单的 tab 注册表，全局唯一 key = `f"{menu_key}:{tab_name}"`，存进 `User.hidden_tabs` 表示对该账号隐藏。
- `tab_registry()`（`menus.py:118-122`）给管理端权限页用。

## 5. 角色 → 业务归属（角色已不管菜单，只管业务）

角色不再管"能看哪些菜单"（2026-07-21 起按账号），仍管**业务归属**：部门工作台归属、下游推送、行级过滤、负责人判定。

### 5.1 部门工作台（`dept_config.py:15-76`）

三个执行部门的 worker_role / lead_role 映射（对应 seed 角色 code）：

| 部门 | worker_role | lead_role | 备注 |
|---|---|---|---|
| design 设计部 | designer | design_lead | sheet_check=True（完成前置四表已导入） |
| electric 电工部 | electrician | electric_lead | |
| produce 生产部 | assembler | pm_lead | 生产无产物，完成只是状态信号 |

部门 push 流按 `to_role` 路由（设计图纸→buyer+sealing+sheetmetal+assembler；电工清单→buyer；电路图→logistics），#324 起按 `to_domain`（BUYER_SHEET_MAP 域）路由。

### 5.2 采购员按清单分工（`dept_config.py:94-98`）

`BUYER_SHEET_MAP`：username → 清单域集合（lixinxin: standard/elec_po；wangqin: material/laser；fangbusen: outsource）。用途：采购下单按人分表可见性 + 设计图纸推送按域路由。

### 5.3 行级过滤 —— 五个复用谓词（唯一真源，禁止重写）

| 谓词 | 文件:行号 | 语义 |
|---|---|---|
| `_buyer_restricted(current)` | purchase_mgmt_router.py:46-53 | 有 buyer 家族且非 buyer_lead/admin/manager → 只看见自己的单；**兼任 finance/logistics 不解除隔离**（反直觉，勿自写）；纯 finance（不带 buyer）可看全部对账 |
| `_all_view(u)` | sales_router.py:53-54 | `_is_mgr`（admin/manager）或 sales_lead |
| `_is_mgr(u)` | sales_router.py:41-42 / orders_router.py:151-152 | `has_role("admin", "manager")` |
| `_is_lead(u, dept)` | orders_router.py:155-156 | `has_role(DEPTS[dept]["lead_role"])` |
| `restricted_dir_pids(db, user)` | deps.py:95-122 | 项目目录/一览行级可见性：角色集 ⊆ {designer, electrician, assembler, sheetmetal, sealing, sales} **且** `project_dir_own_only` 开 → restricted=True，my_pids = 被派单 worker_id / 下单 sales_uid / ProduceGroupTask 聚合；兼任 _lead/管理层则 restricted=False（看全部） |

**AI 助手**（agent_router.py:45-48）直接 `import` 上述谓词本体，不在工具层重写——历史教训：曾自写一套门控导致行级越权（销售员问"尾款"拿到全公司数据，无报错只安静多返回几行）。

### 5.4 项目级门禁（deps.py:125-164）

- `user_can_view_project(db, user, project)`：admin/manager **或任一 `*_lead`** 全项目可见（不依赖成员资格，修新建主管看不到老项目）；其余角色看 `ProjectMember` 成员资格（建项目自动加+启动回填）。
- `user_can_edit_project`：admin/manager 全可编辑；其余看 `ProjectMember.permission == "edit"`。
- `require_can_view_detail`（deps.py:65-72）：`user_can_view_detail()` = 菜单里有 `'list'`（menus.py:157-160）——销售/电工/装配/售后无详单权限（2026-06-12 收紧口径）。

### 5.5 角色级依赖工厂（deps.py）

- `require_admin` / `require_admin_or_manager`（deps.py:31-41）：admin **或** manager 同权。
- `require_roles(*codes)`（deps.py:52-62）：admin/manager 始终放行，多角色取并集。
- `require_not_viewer`（deps.py:45-49）：仅当唯一角色是 viewer 才拦（viewer 角色在 seed ROLES 中不存在，为预留）。

## 6. 反例 / 被排除的猜想

- **猜想"角色决定菜单"**：❌ 不成立。ROLE_MENUS 矩阵已废除，运行时唯一权威是 `user_menu_keys()` 读 `User.menus`；ROLE_DEFAULT_MENUS 只是建号/迁移的一次性模板。改角色不改菜单是**预期行为**（按账号配置）。
- **猜想"seed 的 admin123/manager123 会过期或被强制"**：❌ 不成立。seed 只幂等建一次，密码改过就改过；`password_must_change=True` 只强制首登改密一次（auth_router.py:161 改密后置 False），不是定期轮换。
- **猜想"User.grant_menus 仍生效"**：❌ 停用（models.py:56-57）。存量值并入 menus，读取侧只保留列不删。

## 7. 关键结论速查

1. **菜单三态**：admin/manager = 全量（不读列）；普通账号 = `User.menus` 快照；NULL = catalog/list/messages/oa 兜底；agent 全员追加。
2. **账号-角色是 N:N**，权限取并集；`role_id` 只是锚点，新代码一律用 `role_codes` / `has_role()`。
3. **`finance_lead` ⊇ `finance`** 硬编码在 `role_codes`，一处实现全局生效。
4. **角色不再管菜单**（2026-07-21 起），仍管业务归属（部门工作台/推送/行级过滤/负责人）。
5. **密码 bcrypt**；seed 两账号首登强制改密，生产必须改过默认口令。
6. **改角色不联动菜单**，管理端按账号在「菜单权限」弹窗调 `User.menus`。
