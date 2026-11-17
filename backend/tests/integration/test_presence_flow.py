"""编辑 presence 的登录、续期、到期和离开验收。"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.domain.models import Benchmark, Dataset, DatasetVersion, MetricProfile, Presence

pytestmark = pytest.mark.integration


def test_presence_heartbeat_expires_and_leaves_benchmark_editor_list(client: TestClient) -> None:
    """心跳按用户和资源幂等续期，详情只显示 TTL 内编辑者。"""
    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    factory = client.app.state.evaluation_session_factory
    suffix = uuid.uuid4().hex[:8]
    with factory() as db:
        dataset = Dataset(name=f"Presence数据集-{suffix}", source_type="aishell1")
        db.add(dataset)
        db.flush()
        version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready")
        db.add(version)
        db.flush()
        profile = MetricProfile(
            name=f"Presence口径-{suffix}", params={}, profile_hash=f"presence{suffix}"
        )
        db.add(profile)
        db.flush()
        benchmark = Benchmark(
            name=f"Presence基准-{suffix}",
            dataset_version_id=version.id,
            metric_profile_id=profile.id,
            subsets=["all"],
            sampling={},
            params={},
            bucket_rules={},
        )
        db.add(benchmark)
        db.commit()
        benchmark_id = str(benchmark.id)
        presence_key = ("benchmark", benchmark_id, "admin")

    unauthenticated = client.post(
        "/api/v1/presence", json={"type": "benchmark", "id": benchmark_id}
    )
    assert unauthenticated.status_code == 401

    body = {"type": "benchmark", "id": benchmark_id}
    first = client.post("/api/v1/presence", headers=headers, json=body)
    assert first.status_code == 204
    with factory() as db:
        first_row = db.get(Presence, presence_key)
        assert first_row is not None
        first_expiry = first_row.expires_at
        count = db.scalar(
            select(func.count()).select_from(Presence).where(Presence.resource_id == benchmark_id)
        )
        assert count == 1

    refreshed = client.post("/api/v1/presence", headers=headers, json=body)
    assert refreshed.status_code == 204
    with factory() as db:
        row = db.get(Presence, presence_key)
        assert row is not None
        assert row.expires_at > first_expiry
        assert row.expires_at >= datetime.now(UTC) + timedelta(seconds=89)

    detail = client.get(f"/api/v1/benchmarks/{benchmark_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.headers["Cache-Control"] == "no-store"
    editors = detail.json()["editors"]
    assert len(editors) == 1
    assert editors[0]["username"] == "admin"
    assert editors[0]["display_name"] == "管理员"
    assert editors[0]["last_seen_at"]

    with factory() as db:
        row = db.get(Presence, presence_key)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    expired = client.get(f"/api/v1/benchmarks/{benchmark_id}", headers=headers)
    assert expired.json()["editors"] == []

    leave = client.request("DELETE", "/api/v1/presence", headers=headers, json=body)
    assert leave.status_code == 204
    with factory() as db:
        assert db.get(Presence, presence_key) is None
