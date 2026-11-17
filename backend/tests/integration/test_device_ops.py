"""设备运维权限、指纹、空闲保护与审计流程。"""

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.domain.device_ops import wait_for_device_ops_tasks
from app.domain.models import Deployment, ProbeRun

pytestmark = pytest.mark.integration


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert response.status_code == 200
    return str(response.json()["access_token"])


def _device(client: TestClient, headers: dict[str, str]) -> tuple[str, str]:
    response = client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": f"ops-{uuid.uuid4().hex[:8]}",
            "platform": "205",
            "kind": "etsstt",
            "host": "127.0.0.1",
            "port": 8701,
            "ssh_user": "tester",
        },
    )
    assert response.status_code == 201
    device_id = str(response.json()["id"])
    uid = uuid.uuid4().hex[:16]
    with client.app.state.evaluation_session_factory() as db:
        db.add(
            Deployment(
                device_id=uuid.UUID(device_id),
                uid=uid,
                platform="205",
                bigversion="3.2.412",
                etsllm_version="1.4.49",
                model_key="qwen3-asr-1.7b",
                capabilities={"declared": {}, "observed": {}},
                limits={"concurrency": 1},
            )
        )
        db.commit()
    return device_id, uid


def _request(client: TestClient, headers: dict[str, str], device_id: str, uid: str):
    return client.post(
        f"/api/v1/devices/{device_id}/ops/restart",
        headers={**headers, "Idempotency-Key": f"ops-{device_id}-{uuid.uuid4().hex}"},
        json={
            "confirm": True,
            "expected_fingerprint_uid": uid,
            "reason": "验证设备服务恢复",
        },
    )


def test_ops_requires_confirmation_and_current_fingerprint(client: TestClient) -> None:
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    device_id, uid = _device(client, headers)

    missing_confirmation = client.post(
        f"/api/v1/devices/{device_id}/ops/restart",
        headers=headers,
        json={"confirm": False, "expected_fingerprint_uid": uid, "reason": "服务无响应"},
    )
    assert missing_confirmation.status_code == 422
    assert missing_confirmation.json()["code"] == "E_CONFIRM_REQUIRED"

    changed_fingerprint = client.post(
        f"/api/v1/devices/{device_id}/ops/restart",
        headers=headers,
        json={
            "confirm": True,
            "expected_fingerprint_uid": "0" * 16,
            "reason": "服务无响应",
        },
    )
    assert changed_fingerprint.status_code == 409
    assert changed_fingerprint.json()["code"] == "E_FINGERPRINT_CHANGED"


def test_ops_is_async_idempotent_and_audited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    device_id, uid = _device(client, headers)
    monkeypatch.setattr(
        "app.domain.device_ops._perform_operation",
        lambda _device, _action, _credential=None: {
            "command": "systemctl restart etsllm",
            "return_code": 0,
        },
    )
    monkeypatch.setattr(
        "app.domain.device_ops._wait_until_healthy", lambda _device, _credential=None: "online"
    )

    request_headers = {**headers, "Idempotency-Key": f"device-ops-{device_id}"}
    body = {
        "confirm": True,
        "expected_fingerprint_uid": uid,
        "reason": "验证设备服务恢复",
    }
    accepted = client.post(
        f"/api/v1/devices/{device_id}/ops/restart",
        headers=request_headers,
        json=body,
    )
    assert accepted.status_code == 202
    assert accepted.headers["Location"] == f"/api/v1/jobs/{accepted.json()['id']}"
    wait_for_device_ops_tasks()

    replay = client.post(
        f"/api/v1/devices/{device_id}/ops/restart",
        headers=request_headers,
        json=body,
    )
    assert replay.status_code == 202
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json()["id"] == accepted.json()["id"]

    job = client.get(f"/api/v1/jobs/{accepted.json()['id']}", headers=headers)
    assert job.json()["status"] == "succeeded"
    assert job.json()["result"]["health"] == "online"
    history = client.get(f"/api/v1/devices/{device_id}/ops/history", headers=headers)
    assert history.status_code == 200
    assert len(history.json()["items"]) == 1
    assert history.json()["items"][0]["status"] == "succeeded"
    assert history.json()["items"][0]["detail"]["reason"] == body["reason"]

    audit_logs = client.get("/api/v1/audit-logs", headers=headers)
    assert audit_logs.status_code == 200
    operation_audits = [
        row
        for row in audit_logs.json()["items"]
        if row["action"] in {"device.ops.restart", "device.ops.completed"}
    ]
    requested = next(row for row in operation_audits if row["action"] == "device.ops.restart")
    completed = next(row for row in operation_audits if row["action"] == "device.ops.completed")
    assert requested["actor"] == completed["actor"] == "admin"
    assert requested["ip"]
    assert completed["detail"]["command"] == "systemctl restart etsllm"
    assert completed["detail"]["return_code"] == 0
    assert completed["detail"]["health"] == "online"


def test_ops_lock_blocks_second_action_until_first_finishes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    device_id, uid = _device(client, headers)
    started = threading.Event()
    release = threading.Event()

    def hold_operation(_device, _action, _credential=None):
        started.set()
        assert release.wait(5)
        return {"command": "systemctl restart etsllm", "return_code": 0}

    monkeypatch.setattr("app.domain.device_ops._perform_operation", hold_operation)
    monkeypatch.setattr(
        "app.domain.device_ops._wait_until_healthy", lambda _device, _credential=None: "online"
    )

    first = _request(client, headers, device_id, uid)
    assert first.status_code == 202
    assert started.wait(5)
    second = _request(client, headers, device_id, uid)
    assert second.status_code == 409
    assert second.json()["code"] == "E_DEVICE_BUSY"
    release.set()
    wait_for_device_ops_tasks()


def test_health_returns_full_window_counts_and_recent_probe_rows(client: TestClient) -> None:
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    device_id, _uid = _device(client, headers)
    now = datetime.now(UTC)
    with client.app.state.evaluation_session_factory() as db:
        db.add_all(
            [
                ProbeRun(
                    device_id=uuid.UUID(device_id),
                    mode="lite",
                    status="ok",
                    duration_ms=120,
                    created_at=now - timedelta(hours=26),
                ),
                ProbeRun(
                    device_id=uuid.UUID(device_id),
                    mode="identity",
                    status="ok",
                    duration_ms=430,
                    created_at=now - timedelta(hours=2),
                ),
                ProbeRun(
                    device_id=uuid.UUID(device_id),
                    mode="identity",
                    status="failed",
                    duration_ms=890,
                    error="版本采集超时",
                    created_at=now - timedelta(hours=1),
                ),
            ]
        )
        db.commit()

    response = client.get(f"/api/v1/devices/{device_id}/health", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["probe_count"] == 2
    assert payload["success_count"] == 1
    assert payload["failure_count"] == 1
    assert [probe["status"] for probe in payload["probes"]] == ["ok", "failed"]
    assert payload["probes"][-1]["error"] == "版本采集超时"
    assert payload["last_probe"]["status"] == "failed"
