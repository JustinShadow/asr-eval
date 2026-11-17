"""配置资源 If-Match 并发控制的 API 验收。"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.domain.metric_profiles import profile_hash
from app.domain.models import AuditLog, Benchmark, Dataset, DatasetVersion, MetricProfile, Run

pytestmark = pytest.mark.integration


def _admin_headers(client: TestClient) -> dict[str, str]:
    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _assert_force_audit(client: TestClient, *, action: str, resource: str, reason: str) -> None:
    """强制覆盖必须与配置更新在同一事务中记录原因。"""
    factory = client.app.state.evaluation_session_factory
    with factory() as db:
        log = db.scalar(
            select(AuditLog)
            .where(AuditLog.action == action, AuditLog.resource == resource)
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
    assert log is not None
    assert log.detail["force"] is True
    assert log.detail["reason"] == reason


def test_device_etag_requires_match_reports_conflict_and_serializes_concurrent_updates(
    client: TestClient,
) -> None:
    """设备 PATCH 强制 If-Match，旧版本冲突，并发写入只允许一个成功。"""
    headers = _admin_headers(client)
    created = client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": f"etag设备-{uuid.uuid4().hex[:8]}",
            "platform": "205",
            "kind": "etsstt",
            "host": "127.0.0.1",
            "port": 9000,
            "tags": {},
        },
    )
    assert created.status_code == 201
    device_id = created.json()["id"]
    etag = created.headers["ETag"]

    missing = client.patch(
        f"/api/v1/devices/{device_id}", headers=headers, json={"name": "缺少版本"}
    )
    assert missing.status_code == 428
    assert missing.json()["code"] == "E_PRECONDITION_REQUIRED"

    barrier = threading.Barrier(2)

    def update(name: str):
        barrier.wait(timeout=5)
        return client.patch(
            f"/api/v1/devices/{device_id}",
            headers={**headers, "If-Match": etag},
            json={"name": name},
        )

    names = [f"并发修改甲-{uuid.uuid4().hex[:6]}", f"并发修改乙-{uuid.uuid4().hex[:6]}"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, names))

    assert sorted(result.status_code for result in results) == [200, 412]
    conflict = next(result for result in results if result.status_code == 412)
    detail = conflict.json()["detail"]
    assert detail["current"]["name"] in names
    assert detail["current_etag"] != etag
    assert detail["changed_fields"] == ["name"]
    assert detail["updated_by"] == "admin"
    assert detail["updated_at"]

    current = client.get(f"/api/v1/devices/{device_id}", headers=headers)
    assert current.status_code == 200
    assert current.headers["ETag"] == detail["current_etag"]
    unchanged = client.get(
        f"/api/v1/devices/{device_id}",
        headers={**headers, "If-None-Match": current.headers["ETag"]},
    )
    assert unchanged.status_code == 304
    assert unchanged.headers["ETag"] == current.headers["ETag"]

    missing_reason = client.patch(
        f"/api/v1/devices/{device_id}?force=true",
        headers={**headers, "If-Match": etag},
        json={"name": f"强制设备-{uuid.uuid4().hex[:6]}"},
    )
    assert missing_reason.status_code == 422

    reason = "确认保留管理员的设备名称修改"
    forced = client.patch(
        f"/api/v1/devices/{device_id}?force=true&reason={reason}",
        headers={**headers, "If-Match": etag},
        json={"name": f"强制设备-{uuid.uuid4().hex[:6]}"},
    )
    assert forced.status_code == 200
    _assert_force_audit(
        client,
        action="device.config.updated",
        resource=f"device:{device_id}",
        reason=reason,
    )


def test_metric_profile_etag_update_and_stale_write(client: TestClient) -> None:
    """MetricProfile 提供版本化详情，修改后旧 ETag 返回完整当前对象。"""
    headers = _admin_headers(client)
    created = client.post(
        "/api/v1/metric-profiles",
        headers=headers,
        json={"name": f"etag口径-{uuid.uuid4().hex[:8]}", "params": {}},
    )
    assert created.status_code == 201
    profile_id = created.json()["id"]
    etag = created.headers["ETag"]

    changed = client.patch(
        f"/api/v1/metric-profiles/{profile_id}",
        headers={**headers, "If-Match": etag},
        json={"name": f"已修改口径-{uuid.uuid4().hex[:8]}"},
    )
    assert changed.status_code == 200
    assert changed.json()["version"] == 2
    assert changed.headers["ETag"] != etag

    stale = client.patch(
        f"/api/v1/metric-profiles/{profile_id}",
        headers={**headers, "If-Match": etag},
        json={"name": "过期写入"},
    )
    assert stale.status_code == 412
    assert stale.json()["detail"]["current"]["name"] == changed.json()["name"]
    assert stale.json()["detail"]["changed_fields"] == ["name"]

    reason = "确认覆盖过期口径编辑"
    forced = client.patch(
        f"/api/v1/metric-profiles/{profile_id}?force=true&reason={reason}",
        headers={**headers, "If-Match": etag},
        json={"name": f"强制口径-{uuid.uuid4().hex[:8]}"},
    )
    assert forced.status_code == 200
    _assert_force_audit(
        client,
        action="metric_profile.config.updated",
        resource=f"metric_profile:{profile_id}",
        reason=reason,
    )

    cached = client.get(
        f"/api/v1/metric-profiles/{profile_id}",
        headers={**headers, "If-None-Match": forced.headers["ETag"]},
    )
    assert cached.status_code == 304


def test_benchmark_etag_updates_only_draft_configuration(client: TestClient) -> None:
    """Benchmark 草稿 PATCH 更新 hash 与版本，旧 ETag 回传当前配置。"""
    headers = _admin_headers(client)
    factory = client.app.state.evaluation_session_factory
    suffix = uuid.uuid4().hex[:8]
    with factory() as db:
        dataset = Dataset(name=f"ETag数据集-{suffix}", source_type="aishell1")
        db.add(dataset)
        db.flush()
        version = DatasetVersion(
            dataset_id=dataset.id,
            version="v1",
            status="ready",
            item_count=0,
            capabilities={},
        )
        profile_params = {}
        profile = MetricProfile(
            name=f"ETag口径-{suffix}",
            params=profile_params,
            profile_hash=profile_hash(profile_params),
        )
        db.add_all([version, profile])
        db.flush()
        benchmark = Benchmark(
            name=f"ETag基准-{suffix}",
            dataset_version_id=version.id,
            metric_profile_id=profile.id,
            subsets=["all"],
            sampling={"mode": "all"},
            params={},
            bucket_rules={},
            version_hash=f"etag-{suffix}",
        )
        db.add(benchmark)
        db.commit()
        benchmark_id = str(benchmark.id)

    current = client.get(f"/api/v1/benchmarks/{benchmark_id}", headers=headers)
    assert current.status_code == 200
    etag = current.headers["ETag"]

    missing = client.patch(
        f"/api/v1/benchmarks/{benchmark_id}",
        headers=headers,
        json={"note": "缺少 If-Match"},
    )
    assert missing.status_code == 428

    changed = client.patch(
        f"/api/v1/benchmarks/{benchmark_id}",
        headers={**headers, "If-Match": etag},
        json={"sampling": {"mode": "random", "n": 10, "seed": 7}},
    )
    assert changed.status_code == 200
    assert changed.json()["version"] == 2
    assert changed.json()["version_hash"] != current.json()["version_hash"]
    assert changed.headers["ETag"] != etag

    stale = client.patch(
        f"/api/v1/benchmarks/{benchmark_id}",
        headers={**headers, "If-Match": etag},
        json={"sampling": {"mode": "all"}},
    )
    assert stale.status_code == 412
    assert stale.json()["detail"]["current"]["sampling"] == changed.json()["sampling"]
    assert stale.json()["detail"]["changed_fields"] == ["sampling"]

    reason = "确认覆盖过期基准编辑"
    forced = client.patch(
        f"/api/v1/benchmarks/{benchmark_id}?force=true&reason={reason}",
        headers={**headers, "If-Match": etag},
        json={"note": reason},
    )
    assert forced.status_code == 200
    _assert_force_audit(
        client,
        action="benchmark.config.updated",
        resource=f"benchmark:{benchmark_id}",
        reason=reason,
    )

    cached = client.get(
        f"/api/v1/benchmarks/{benchmark_id}",
        headers={**headers, "If-None-Match": changed.headers["ETag"]},
    )
    assert cached.status_code == 200
    assert cached.json()["editors"] == []


def test_referenced_profile_requires_a_new_immutable_version(client: TestClient) -> None:
    """被 Benchmark 引用的口径只能创建新版本，不能改写历史定义。"""
    headers = _admin_headers(client)
    suffix = uuid.uuid4().hex[:8]
    created_profile = client.post(
        "/api/v1/metric-profiles",
        headers=headers,
        json={"name": f"联动口径-{suffix}", "params": {}},
    )
    assert created_profile.status_code == 201
    profile_id = created_profile.json()["id"]

    factory = client.app.state.evaluation_session_factory
    with factory() as db:
        dataset = Dataset(name=f"联动数据集-{suffix}", source_type="aishell1")
        db.add(dataset)
        db.flush()
        version = DatasetVersion(
            dataset_id=dataset.id,
            version="v1",
            status="ready",
            item_count=0,
            capabilities={},
        )
        db.add(version)
        db.flush()
        benchmark = Benchmark(
            name=f"联动基准-{suffix}",
            dataset_version_id=version.id,
            metric_profile_id=uuid.UUID(profile_id),
            subsets=["all"],
            sampling={"mode": "all"},
            params={},
            bucket_rules={},
            version_hash=f"petag-{suffix}",
        )
        db.add(benchmark)
        db.commit()
        benchmark_id = str(benchmark.id)

    before_benchmark = client.get(f"/api/v1/benchmarks/{benchmark_id}", headers=headers)
    old_profile_etag = created_profile.headers["ETag"]
    rejected = client.patch(
        f"/api/v1/metric-profiles/{profile_id}",
        headers={**headers, "If-Match": old_profile_etag},
        json={"params": {"text": {"strip_space": False}}},
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "E_VERSION_IN_USE"

    versioned = client.post(
        f"/api/v1/metric-profiles/{profile_id}/versions",
        headers=headers,
        json={"params": {"text": {"strip_space": False}}},
    )
    assert versioned.status_code == 201
    assert versioned.json()["version"] == 2
    assert versioned.json()["params"] == {"text": {"strip_space": False}}
    assert versioned.json()["id"] != profile_id

    updated_benchmark = client.get(f"/api/v1/benchmarks/{benchmark_id}", headers=headers)
    assert updated_benchmark.json()["version"] == 1
    assert updated_benchmark.json()["version_hash"] == before_benchmark.json()["version_hash"]

    with factory() as db:
        db.add(Run(benchmark_id=uuid.UUID(benchmark_id), name=f"已使用Run-{suffix}"))
        db.commit()

    frozen_benchmark = client.patch(
        f"/api/v1/benchmarks/{benchmark_id}",
        headers={**headers, "If-Match": updated_benchmark.headers["ETag"]},
        json={"note": "已使用后不能更新"},
    )
    assert frozen_benchmark.status_code == 409
    assert frozen_benchmark.json()["code"] == "E_VERSION_IN_USE"

    frozen_profile = client.patch(
        f"/api/v1/metric-profiles/{profile_id}",
        headers={**headers, "If-Match": old_profile_etag},
        json={"name": f"不可修改口径-{suffix}"},
    )
    assert frozen_profile.status_code == 409
    assert frozen_profile.json()["code"] == "E_VERSION_IN_USE"
