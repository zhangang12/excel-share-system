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


# 确保 data 目录存在（SQLite 文件会写到这里）
Path("data").mkdir(exist_ok=True)

settings = Settings()

# 🆕 确保附件目录存在
Path(settings.files_dir).mkdir(parents=True, exist_ok=True)
