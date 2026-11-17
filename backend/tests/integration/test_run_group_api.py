"""跨数据集评测组（RunGroup）API 验收。"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password
from app.domain.models import (
    AppUser,
    AudioAsset,
    Benchmark,
    Dataset,
    DatasetSubset,
    DatasetVersion,
    Deployment,
    Device,
    Item,
    MetricProfile,
    RefText,
)

TEST_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://asr:asr@127.0.0.1:5432/asr_eval_test",
)
pytestmark = pytest.mark.integration


class _NoopWorker:
    """测试不启动真实执行队列，只验证创建与汇总契约。"""

    def enqueue(self, *_args: object, **_kwargs: object) -> None:
        return None


def _login(client: TestClient) -> dict[str, str]:
    username = f"run-group-{uuid.uuid4().hex[:12]}"
    password = "run-group-test-password"
    factory = client.app.state.evaluation_session_factory
    with factory() as session:
        session.add(
            AppUser(
                username=username,
                display_name="评测组测试用户",
                role="tester",
                role_source="local",
                password_hash=hash_password(password),
                active=True,
            )
        )
        session.commit()
    client.cookies.clear()
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _seed(db: Session) -> tuple[list[uuid.UUID], uuid.UUID]:
    """两个数据集版本各建一个 Benchmark，共用一台已探测设备。"""
    suffix = uuid.uuid4().hex[:8]
    profile = MetricProfile(name=f"组口径-{suffix}", params={}, profile_hash=suffix.ljust(16, "0"))
    device = Device(
        name=f"组设备-{suffix}",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=9,
        status="online",
    )
    db.add_all([profile, device])
    db.flush()
    db.add(
        Deployment(
            device_id=device.id,
            uid=suffix[:16],
            platform="205",
            accel={"type": "rknn"},
            model_key="etsstt",
            capabilities={},
        )
    )
    benchmark_ids: list[uuid.UUID] = []
    for index in range(2):
        dataset = Dataset(name=f"组数据集-{suffix}-{index}", source_type="synthetic")
        db.add(dataset)
        db.flush()
        version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready", item_count=1)
        db.add(version)
        db.flush()
        subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
        db.add(subset)
        db.flush()
        asset = AudioAsset(sha256=f"{suffix}{index}".ljust(64, "0"), uri=f"/data/{index}.wav")
        db.add(asset)
        db.flush()
        item = Item(
            dataset_version_id=version.id,
            subset_id=subset.id,
            item_key=f"组-{suffix}-{index}",
            content_uid=f"{suffix}{index}".ljust(24, "0"),
            audio_asset_id=asset.id,
            split="test",
            duration_ms=2000,
        )
        db.add(item)
        db.flush()
        db.add(RefText(item_id=item.id, kind="verbatim", text="组评测参考", sha256=suffix))
        benchmark = Benchmark(
            name=f"组基准-{suffix}-{index}",
            dataset_version_id=version.id,
            metric_profile_id=profile.id,
            subsets=["all"],
            sampling={},
        )
        db.add(benchmark)
        db.flush()
        benchmark_ids.append(benchmark.id)
    db.commit()
    return benchmark_ids, device.id


def test_create_and_read_run_group(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """一次请求为两个 Benchmark 各建一条 Run，并可读取组级汇总。"""
    import app.api.runs as runs_api

    monkeypatch.setattr(runs_api, "get_run_worker", lambda: _NoopWorker())
    headers = _login(client)
    engine = create_engine(TEST_URL, pool_pre_ping=True)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        benchmark_ids, device_id = _seed(session)
    finally:
        session.close()
        engine.dispose()

    response = client.post(
        "/api/v1/runs/groups",
        headers=headers,
        json={
            "name": "多数据集回归",
            "benchmark_ids": [str(value) for value in benchmark_ids],
            "targets": [{"device_id": str(device_id), "subset": "all"}],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["group"]["run_count"] == 2
    assert len(body["runs"]) == 2
    assert {entry["group_id"] for entry in body["runs"]} == {body["group"]["id"]}
    assert all(entry["dataset"]["benchmark"] for entry in body["runs"])

    listed = client.get("/api/v1/runs/groups", headers=headers)
    assert listed.status_code == 200
    assert any(item["id"] == body["group"]["id"] for item in listed.json()["items"])

    detail = client.get(f"/api/v1/runs/groups/{body['group']['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["group"]["id"] == body["group"]["id"]
    assert detail.json()["aggregate"]["cer"] is None
