"""数据库连接与会话。

开发阶段用同步 SQLAlchemy：3~5 个用户、任务量万级，同步足够；
FastAPI 的同步路由会在线程池执行，不阻塞事件循环。
"""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    """按配置创建引擎；连接是惰性的，数据库未启动也能导入应用。"""
    settings = get_settings()
    return create_engine(settings.database_url, pool_pre_ping=True, pool_size=5)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """返回会话工厂。"""
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话。"""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
