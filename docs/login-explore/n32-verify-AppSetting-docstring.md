# n32 核实：AppSetting docstring 过时

## 被核说法

> models.py:1146 AppSetting 类 docstring 仍写『仅 Agent 助手用来存 LLM 配置』，但外网闸门配置也存此表，注释过时误导排查。

## 核实方式：证伪视角

默认立场 = 说法不成立，自己走证据链，亲自读代码。

## 自己走的证据链

### 1. models.py AppSetting docstring 原文

`backend/app/models.py:1146-1148`
```python
class AppSetting(Base):
    """通用 kv 配置表（key 主键）。目前仅 Agent 助手用来存 LLM 配置
    （agent_llm.base_url / agent_llm.api_key / agent_llm.model / agent_llm_models），
    表由 Base.metadata.create_all 自动创建，无需迁移脚本。"""
```

关键表述「**仅** Agent 助手用来存 LLM 配置」—— **仅**字为事实错误。

### 2. gate.py 读 AppSetting

`backend/app/gate.py:96-107`
```python
async def get_gate_config(db: AsyncSession) -> dict:
    """生效配置 = app_settings；gate_enabled 默认开，两个名单默认空（每次请求实时读库）。"""
    r = await db.execute(select(models.AppSetting).where(models.AppSetting.key.in_(
        [_CFG_ENABLED, _CFG_CIDRS, _CFG_DEVICE_GATE, _CFG_DEVICE_IDS])))
    ...
```

四个闸门配置键：`gate_enabled`、`intranet_cidrs`、`device_gate_enabled`、`device_ids`（定义在 `gate.py:23-26`）。

### 3. gate.py 写 AppSetting

`backend/app/gate.py:110-126`
```python
async def set_gate_config(db, *, enabled, cidrs, device_gate=False, device_ids=None):
    """写 app_settings（upsert），保存即全局生效。"""
    ...
    row = await db.get(models.AppSetting, key)
    ...
    db.add(models.AppSetting(key=key, value=value))
    await db.commit()
```

直接对 `AppSetting` 表做 upsert，写入四个闸门 key。

### 4. gate.py 模块 docstring 也是明确证据

`backend/app/gate.py:5-6`
> 配置存 app_settings：gate_enabled 默认 "1" 开、device_gate_enabled 默认 "0" 关、intranet_cidrs / device_ids 为 JSON 数组默认 []。

### 5. 不止两家：还有 agent memory 和 daily briefing

- `backend/app/agent/memory.py:62-79`：读写 `app_settings` 存术语别名/已阅状态
- `backend/app/agent/daily.py:44-61`：读写 `app_settings.agent_briefing_users`
- `backend/app/routers/agent_router.py:1213-1214`：读 `agent_llm.*` 四个 LLM 配置 key

AppSetting 至少被 **三个子系统** 消费（Agent LLM / Agent 记忆与简报 / Gate 闸门），docstring 写「仅 Agent 助手」明显过时。

## 判决：PASS

说法成立。AppSetting 的 docstring 声称「仅 Agent 助手用来存 LLM 配置」，但实际至少还有外网闸门四个 key（gate_enabled/intranet_cidrs/device_gate_enabled/device_ids）以及 agent/memory 和 agent/daily 的 key 也落在同一张表。「仅」字为事实错误，会误导排查人员以为这张表只有 Agent 用到。

## 证据汇总

| # | 证据 | 说明 |
|---|------|------|
| 1 | `models.py:1146-1148` | docstring 写"仅 Agent 助手用来存 LLM 配置" |
| 2 | `gate.py:23-26` | 四个闸门 key 常量定义 |
| 3 | `gate.py:96-107` | get_gate_config 从 AppSetting 读取闸门配置 |
| 4 | `gate.py:110-126` | set_gate_config 向 AppSetting 写入闸门配置 |
| 5 | `gate.py:5-6` | 模块 docstring 写"配置存 app_settings" |
| 6 | `agent/memory.py:62-79` | memory 模块也读写 AppSetting |
| 7 | `agent/daily.py:44-61` | daily 模块也读写 AppSetting |

## 分歧/遗留

无。
