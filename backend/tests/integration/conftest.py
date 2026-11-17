"""集成测试共享夹具：PostgreSQL + TestClient + admin。"""

import os
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.db import get_db
from app.core.security import hash_password
from app.domain.models import AppUser, Base
from app.domain.user_events import UserEventListener
from app.main import create_app

TEST_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://asr:asr@127.0.0.1:5432/asr_eval_test",
)
TEST_DATABASE_NAME = urlsplit(TEST_URL).path.lstrip("/")
if not any(marker in TEST_DATABASE_NAME.split("_") for marker in ("test", "e2e")):
    raise RuntimeError(
        "集成测试只允许连接名称包含独立 test/e2e 标记的 PostgreSQL 数据库；"
        "请设置 TEST_DATABASE_URL，开发数据库已被拒绝。"
    )


@pytest.fixture(autouse=True)
def isolate_process_rate_limits() -> Iterator[None]:
    """每条集成用例独立验证限流，避免共享 TestClient 污染后续用例。"""
    from app.core.rate_limit import rate_limiter, sse_connection_limiter

    rate_limiter.clear()
    sse_connection_limiter.clear()
    yield
    rate_limiter.clear()
    sse_connection_limiter.clear()


@pytest.fixture(scope="session")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """建表、造 admin，并覆盖 get_db；数据库不可用时整组跳过。"""
    engine = create_engine(TEST_URL, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:
        pytest.skip(f"PostgreSQL 不可用: {exc}")

    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS import_task CASCADE"))
        connection.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine, expire_on_commit=False)

    with testing_session() as db:
        db.add(
            AppUser(
                username="admin",
                display_name="管理员",
                role="admin",
                role_source="local",
                password_hash=hash_password("admin12345"),
                active=True,
            )
        )
        db.commit()

    previous_key_path = os.environ.get("CREDENTIALS_KEY_FILE")
    os.environ["CREDENTIALS_KEY_FILE"] = str(
        tmp_path_factory.mktemp("asr-eval-test-secrets") / "credentials_key"
    )
    get_settings.cache_clear()
    app = create_app()
    app.state.evaluation_session_factory = testing_session
    app.state.evaluation_engine = engine
    app.state.user_event_listener = UserEventListener(engine)

    def override_db() -> Iterator[Session]:
        session = testing_session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        engine.dispose()
        if previous_key_path is None:
            os.environ.pop("CREDENTIALS_KEY_FILE", None)
        else:
            os.environ["CREDENTIALS_KEY_FILE"] = previous_key_path
        get_settings.cache_clear()
