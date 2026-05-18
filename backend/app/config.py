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


# 确保 data 目录存在（SQLite 文件会写到这里）
Path("data").mkdir(exist_ok=True)

settings = Settings()
