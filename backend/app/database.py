"""数据库连接 - SQLAlchemy 2.0 异步"""
from sqlalchemy.ext.asyncio import (
    create_async_engine, async_sessionmaker, AsyncSession,
)
from sqlalchemy.orm import DeclarativeBase

from .config import settings


engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """所有 ORM 模型继承此 Base"""
    pass


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
