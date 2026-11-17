"""设备列表和详情的租约占用摘要验收。"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.domain.models import (
    Benchmark,
    Dataset,
    DatasetVersion,
    Device,
    DeviceLease,
    MetricProfile,
    Run,
    RunTarget,
)

pytestmark = pytest.mark.integration


def test_device_list_and_detail_expose_active_lease_holder(client: TestClient) -> None:
    """列表与详情返回租约对应的 Run、操作者及释放时间，过期后返回 null。"""
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
        dataset = Dataset(name=f"租约数据集-{suffix}", source_type="aishell1")
        db.add(dataset)
        db.flush()
        version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready")
        db.add(version)
        db.flush()
        profile = MetricProfile(name=f"租约口径-{suffix}", params={}, profile_hash=f"lease{suffix}")
        db.add(profile)
        db.flush()
        benchmark = Benchmark(
            name=f"租约基准-{suffix}",
            dataset_version_id=version.id,
            metric_profile_id=profile.id,
            subsets=["all"],
            sampling={},
            params={},
            bucket_rules={},
        )
        device = Device(
            name=f"租约设备-{suffix}",
            platform="205",
            kind="etsstt",
            host="127.0.0.1",
            port=9000,
        )
        db.add_all([benchmark, device])
        db.flush()
        run = Run(benchmark_id=benchmark.id, name=f"占用任务-{suffix}", created_by="admin")
        db.add(run)
        db.flush()
        target = RunTarget(run_id=run.id, device_id=device.id)
        db.add(target)
        db.flush()
        now = datetime.now(UTC)
        lease = DeviceLease(
            device_id=device.id,
            run_target_id=target.id,
            holder=str(run.id),
            acquired_at=now,
            expires_at=now + timedelta(minutes=2),
        )
        db.add(lease)
        db.commit()
        device_id = str(device.id)
        run_id = str(run.id)
        acquired_at = lease.acquired_at.isoformat()
        expires_at = lease.expires_at.isoformat()

    listing = client.get("/api/v1/devices", headers=headers)
    assert listing.status_code == 200
    listed_device = next(item for item in listing.json()["items"] if item["id"] == device_id)
    assert listed_device["lease"] == {
        "holder_run_id": run_id,
        "holder_run_name": f"占用任务-{suffix}",
        "holder_user": "admin",
        "acquired_at": acquired_at,
        "expires_at": expires_at,
    }

    detail = client.get(f"/api/v1/devices/{device_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["lease"]["holder_run_id"] == run_id
    assert detail.json()["lease"]["holder_user"] == "admin"
    active_etag = detail.headers["ETag"]

    with factory() as db:
        stored_lease = db.get(DeviceLease, uuid.UUID(device_id))
        assert stored_lease is not None
        stored_lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    expired = client.get(
        f"/api/v1/devices/{device_id}",
        headers={**headers, "If-None-Match": active_etag},
    )
    assert expired.status_code == 200
    assert expired.json()["lease"] is None
    assert expired.headers["ETag"] != active_etag
