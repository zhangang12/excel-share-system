"""应用配置 - 通过环境变量或 .env 调整"""
import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


# 默认数据库：若没设环境变量，用本地 SQLite（单机演示模式）
DEFAULT_DB = "sqlite+aiosqlite:///./data/app.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "同辉智能项目管理系统"
    debug: bool = True

    # 数据库
    database_url: str = DEFAULT_DB

    # Redis（演示版可选；后续 Yjs 协作才需要，暂不实际使用）
    redis_url: str = "redis://localhost:6379/0"

    # 安全
    secret_key: str = "demo-secret-key-change-in-prod"
    access_token_expire_minutes: int = 60 * 8
    algorithm: str = "HS256"

    # 默认管理员
    default_admin_username: str = "admin"
    default_admin_password: str = "admin123"

    # 前端静态目录（演示模式：由 backend 直接托管）
    static_dir: str = "../web"

    # 🆕 v3：附件存储目录（合同/图纸包/产物/发票等业务文件）
    files_dir: str = "data/files"
    # 🆕 v3：单文件上传大小上限（字节，默认 50MB）
    max_upload_size: int = 50 * 1024 * 1024

    # 🆕 v3：企业微信推送凭证（留空=纯站内消息模式，F3 口径不阻塞）
    wecom_corp_id: str = ""
    wecom_agent_id: str = ""
    wecom_secret: str = ""

    # 🆕 v3 M16：导出审批开关（默认关闭=所有导出行为与现状完全一致；
    # 上线灰度后由管理层确认再打开，符合"动老页面用可逆开关"红线）
    export_approval_enabled: bool = False

    # 🆕 销售下单审批开关(2026-06-18)：开启后「销售员」下单进入"待销售主管审批"，通过后才创建并推送
    # 各部门任务/物流发货待办；销售主管/管理层下单仍直接生效。默认关闭=与现状一致(下单即生效)，
    # 生产环境置 SALES_ORDER_APPROVAL=true 开启（可逆开关红线）。
    sales_order_approval: bool = False

    # 🆕 Agent 助手（只读问数 POC）：OpenAI 兼容接口（function calling）。
    # api_key 留空 = 纯规则降级模式（永远可用）；任意 LLM 异常也自动降级。
    agent_llm_base_url: str = "https://api.deepseek.com/v1"
    agent_llm_api_key: str = ""
    agent_llm_model: str = "deepseek-chat"
    # 可选模型白名单（逗号分隔；agent_llm_model 为默认模型，不在名单里会自动并入）
    agent_llm_models: str = "deepseek-chat,deepseek-reasoner"

    # 🆕 项目目录行级可见性：仅 设计/电工/装配 三类岗位只看"自己接的项目"(被派单 worker_id=自己)，
    # 其余角色(管理层/各部门负责人/采购/仓库/财务/物流等)维持看全部。
    # 默认开启；如需回退为"全部可见"，设环境变量 PROJECT_DIR_OWN_ONLY=false 重启即可（可逆开关红线）。
    project_dir_own_only: bool = True


# 确保 data 目录存在（SQLite 文件会写到这里）
Path("data").mkdir(exist_ok=True)

settings = Settings()

# 🆕 确保附件目录存在
Path(settings.files_dir).mkdir(parents=True, exist_ok=True)
