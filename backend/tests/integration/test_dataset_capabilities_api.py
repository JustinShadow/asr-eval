"""数据集版本能力查询接口集成测试（M1 §5.5/§5.6、M7 §5.1）。"""

import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.domain.models import Base, Dataset, DatasetSubset, DatasetVersion

TEST_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://asr:asr@127.0.0.1:5432/asr_eval_test",
)
pytestmark = pytest.mark.integration


@pytest.fixture()
def db() -> Iterator[Session]:
    """使用集成测试 PostgreSQL 准备版本与 subset 能力数据。"""
    engine = create_engine(TEST_URL, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_version_capabilities_include_weighted_and_subset_values(
    client: TestClient, db: Session
) -> None:
    """读取同时返回版本汇总与独立 subset 值，防止门控拿错聚合层级。"""
    suffix = uuid.uuid4().hex[:10]
    dataset = Dataset(name=f"能力接口-{suffix}", source_type="apy")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(
        dataset_id=dataset.id,
        version="v1",
        status="ready",
        capabilities={"text_ratio": 0.9, "item_count": 10},
    )
    db.add(version)
    db.flush()
    db.add_all(
        [
            DatasetSubset(
                dataset_version_id=version.id,
                name="mic",
                is_primary=False,
                capabilities={"text_ratio": 0.8},
            ),
            DatasetSubset(
                dataset_version_id=version.id,
                name="phone",
                is_primary=True,
                capabilities={"text_ratio": 1.0},
            ),
        ]
    )
    db.commit()

    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert login.status_code == 200
    response = client.get(
        f"/api/v1/dataset-versions/{version.id}/capabilities",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["dataset_version_id"] == str(version.id)
    assert payload["capabilities"] == {"text_ratio": 0.9, "item_count": 10}
    assert payload["subsets"] == [
        {"name": "mic", "is_primary": False, "capabilities": {"text_ratio": 0.8}},
        {"name": "phone", "is_primary": True, "capabilities": {"text_ratio": 1.0}},
    ]


def test_version_capabilities_returns_not_found(client: TestClient) -> None:
    """不存在的版本按统一 API 错误契约返回 404。"""
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    token = login.json()["access_token"]
    response = client.get(
        f"/api/v1/dataset-versions/{uuid.uuid4()}/capabilities",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "E_NOT_FOUND"
