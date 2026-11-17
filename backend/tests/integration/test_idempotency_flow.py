"""Run 创建接口幂等重放与并发去重集成测试。"""

import hashlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domain.idempotency import execute_idempotently
from app.domain.models import (
    AudioAsset,
    Dataset,
    DatasetSubset,
    DatasetVersion,
    Deployment,
    Device,
    IdempotencyKey,
    Item,
    RefText,
    Run,
)

pytestmark = pytest.mark.integration


def test_run_create_replays_and_rejects_different_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """相同 key 与内容返回原 Run，不同内容返回 409。"""
    client.cookies.clear()
    factory = client.app.state.evaluation_session_factory
    with factory() as db:
        suffix = uuid.uuid4().hex[:10]
        dataset = Dataset(name=f"幂等数据集-{suffix}", source_type="aishell1")
        db.add(dataset)
        db.flush()
        version = DatasetVersion(
            dataset_id=dataset.id,
            version="v1",
            status="ready",
            item_count=1,
            capabilities={},
        )
        db.add(version)
        db.flush()
        subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
        db.add(subset)
        db.flush()
        audio = AudioAsset(
            sha256=hashlib.sha256(suffix.encode()).hexdigest(), uri=f"/data/{suffix}.wav"
        )
        db.add(audio)
        db.flush()
        item = Item(
            dataset_version_id=version.id,
            subset_id=subset.id,
            item_key=f"idempotency-{suffix}",
            content_uid=hashlib.sha256(f"content-{suffix}".encode()).hexdigest()[:24],
            audio_asset_id=audio.id,
            split="test",
            duration_ms=4000,
        )
        db.add(item)
        db.flush()
        db.add(
            RefText(
                item_id=item.id,
                kind="verbatim",
                text="幂等请求样本",
                sha256=hashlib.sha256("幂等请求样本".encode()).hexdigest()[:16],
            )
        )
        device = Device(
            name=f"幂等设备-{suffix}",
            platform="205",
            kind="etsstt",
            host="127.0.0.1",
            port=9,
            status="online",
        )
        db.add_all([version, device])
        db.flush()
        deployment = Deployment(
            device_id=device.id,
            uid=suffix,
            model_key="idempotency-test",
            platform="205",
            accel={},
            capabilities={},
        )
        db.add(deployment)
        db.commit()
        version_id = str(version.id)
        device_id = str(device.id)

    async def fake_execute_run(db: Session, run_id: uuid.UUID) -> Run:
        run = db.get(Run, run_id)
        assert run is not None
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        db.commit()
        return run

    monkeypatch.setattr("app.domain.run_worker.execute_run", fake_execute_run)

    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    headers = {
        "Authorization": f"Bearer {login.json()['access_token']}",
        "Idempotency-Key": f"run-{uuid.uuid4()}",
    }
    payload = {
        "name": f"幂等 Run-{uuid.uuid4().hex[:8]}",
        "dataset_version_id": version_id,
        "device_id": device_id,
    }

    first = client.post("/api/v1/runs", headers=headers, json=payload)
    replay = client.post("/api/v1/runs", headers=headers, json=payload)

    assert first.status_code == replay.status_code == 201
    assert replay.json()["run"]["id"] == first.json()["run"]["id"]
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.headers["Location"] == first.headers["Location"]

    conflict = client.post(
        "/api/v1/runs",
        headers=headers,
        json={**payload, "name": f"不同请求-{uuid.uuid4().hex[:8]}"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "E_IDEMPOTENCY_CONFLICT"

    with factory() as db:
        count = db.scalar(
            select(func.count())
            .select_from(Run)
            .where(Run.id == uuid.UUID(first.json()["run"]["id"]))
        )
        assert count == 1


def test_concurrent_same_key_runs_operation_once(client: TestClient) -> None:
    """PostgreSQL advisory lock 串行化并发请求并重放首次响应。"""
    factory = client.app.state.evaluation_session_factory
    barrier = threading.Barrier(2)
    operation_lock = threading.Lock()
    operation_count = 0
    key = f"parallel-{uuid.uuid4()}"

    def invoke() -> bool:
        nonlocal operation_count
        with factory() as db:
            barrier.wait(timeout=5)

            def create() -> dict[str, str]:
                nonlocal operation_count
                with operation_lock:
                    operation_count += 1
                time.sleep(0.1)
                return {"id": str(uuid.uuid4())}

            result = execute_idempotently(
                db,
                username="parallel-test-user",
                key=key,
                request_body={"name": "同一请求"},
                operation=create,
                location=lambda body: f"/api/v1/runs/{body['id']}",
            )
            return result.replayed

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(invoke) for _ in range(2)]
        replayed = [future.result(timeout=15) for future in futures]

    assert sorted(replayed) == [False, True]
    assert operation_count == 1


def test_expired_idempotency_key_can_be_reused(client: TestClient) -> None:
    """缓存超过 24 小时后同一 key 可以代表新的请求。"""
    factory = client.app.state.evaluation_session_factory
    key = f"expired-{uuid.uuid4()}"
    with factory() as db:
        first = execute_idempotently(
            db,
            username="expiry-test-user",
            key=key,
            request_body={"name": "旧请求"},
            operation=lambda: {"id": "old"},
            location=lambda body: f"/api/v1/runs/{body['id']}",
        )
        assert first.replayed is False
        row = db.get(IdempotencyKey, {"username": "expiry-test-user", "key": key})
        assert row is not None
        row.created_at = datetime.now(UTC) - timedelta(hours=25)
        db.commit()

        second = execute_idempotently(
            db,
            username="expiry-test-user",
            key=key,
            request_body={"name": "新请求"},
            operation=lambda: {"id": "new"},
            location=lambda body: f"/api/v1/runs/{body['id']}",
        )

    assert second.replayed is False
    assert second.body == {"id": "new"}


def test_different_request_body_conflicts(client: TestClient) -> None:
    """幂等缓存里的 request hash 不匹配时返回 409。"""
    factory = client.app.state.evaluation_session_factory
    key = f"conflict-{uuid.uuid4()}"
    with factory() as db:
        execute_idempotently(
            db,
            username="conflict-test-user",
            key=key,
            request_body={"name": "首个请求"},
            operation=lambda: {"id": "first"},
            location=lambda body: f"/api/v1/runs/{body['id']}",
        )
        with pytest.raises(ApiError) as error:
            execute_idempotently(
                db,
                username="conflict-test-user",
                key=key,
                request_body={"name": "另一个请求"},
                operation=lambda: {"id": "second"},
                location=lambda body: f"/api/v1/runs/{body['id']}",
            )

    assert error.value.status_code == 409
    assert error.value.code == "E_IDEMPOTENCY_CONFLICT"
