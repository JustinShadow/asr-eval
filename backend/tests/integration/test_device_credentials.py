"""设备凭据的 API 脱敏、持久化与 cloud Key 探测。"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.domain.devices import wait_for_probe_tasks
from app.domain.models import DeviceCredential

pytestmark = pytest.mark.integration


def _login(client: TestClient) -> str:
    """登录测试管理员并返回访问令牌。"""
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert response.status_code == 200
    return str(response.json()["access_token"])


def test_gateway_can_save_ssh_credential_for_identity_checks(client: TestClient) -> None:
    """网关允许保存 SSH 凭据，供身份采集与健康检查使用。"""
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": f"gateway-{uuid.uuid4().hex[:8]}",
            "platform": "502",
            "kind": "gateway",
            "host": "192.0.2.10",
            "port": 30025,
            "ssh_user": "tester",
            "ssh_port": 22,
        },
    )
    assert created.status_code == 201
    device_id = created.json()["id"]
    credential = {
        "kind": "ssh_password",
        "secret": "gateway-secret-never-return-this",
    }

    saved = client.put(
        f"/api/v1/devices/{device_id}/credential",
        headers={**headers, "If-Match": created.headers["ETag"]},
        json=credential,
    )

    assert saved.status_code == 200
    assert saved.json()["has_password"] is True
    assert credential["secret"] not in saved.text
    summary = client.get(f"/api/v1/devices/{device_id}/credential", headers=headers)
    assert summary.json()["kind"] == "ssh_password"
    assert credential["secret"] not in summary.text


def test_cloud_key_is_encrypted_redacted_and_used_by_full_probe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cloud Key 只写入密文；full 探测只调用模型列表校验。"""
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": f"cloud-{uuid.uuid4().hex[:8]}",
            "platform": "205",
            "kind": "cloud",
            "host": "api.example.test",
            "port": 443,
        },
    )
    assert created.status_code == 201
    device_id = created.json()["id"]

    missing = client.post(f"/api/v1/devices/{device_id}/probe?mode=full", headers=headers)
    assert missing.status_code == 409
    assert missing.json()["code"] == "E_CREDENTIAL_REQUIRED"

    secret = "sk-cloud-secret-never-return-this"
    invalid_passphrase = client.put(
        f"/api/v1/devices/{device_id}/credential",
        headers={**headers, "If-Match": created.headers["ETag"]},
        json={"kind": "cloud_api_key", "secret": secret, "passphrase": "unused"},
    )
    assert invalid_passphrase.status_code == 422

    saved = client.put(
        f"/api/v1/devices/{device_id}/credential",
        headers={**headers, "If-Match": created.headers["ETag"]},
        json={"kind": "cloud_api_key", "secret": secret},
    )
    assert saved.status_code == 200
    assert saved.json() == {
        "configured": True,
        "kind": "cloud_api_key",
        "has_password": False,
        "has_ssh_key": False,
        "has_api_key": True,
    }
    assert secret not in saved.text

    summary = client.get(f"/api/v1/devices/{device_id}/credential", headers=headers)
    assert summary.status_code == 200
    assert summary.json()["has_api_key"] is True
    assert secret not in summary.text
    detail = client.get(f"/api/v1/devices/{device_id}", headers=headers)
    assert secret not in detail.text
    assert "secret_ref" not in detail.json()

    with client.app.state.evaluation_session_factory() as db:
        row = db.scalar(
            select(DeviceCredential).where(DeviceCredential.device_id == uuid.UUID(device_id))
        )
        assert row is not None
        assert secret.encode() not in row.ciphertext

    received: list[str] = []

    async def validate_cloud_key(host: str, port: int, api_key: str) -> SimpleNamespace:
        assert host == "api.example.test"
        assert port == 443
        received.append(api_key)
        return SimpleNamespace(ok=True, model_count=3, error=None)

    monkeypatch.setattr("app.domain.devices.probe_cloud_models", validate_cloud_key)
    accepted = client.post(
        f"/api/v1/devices/{device_id}/probe?mode=full",
        headers={**headers, "Idempotency-Key": f"cloud-full-{device_id}"},
    )
    assert accepted.status_code == 202
    wait_for_probe_tasks()
    finished = client.get(f"/api/v1/jobs/{accepted.json()['id']}", headers=headers)
    assert finished.json()["status"] == "succeeded"
    assert finished.json()["result"]["probe"]["result"]["model_count"] == 3
    assert received == [secret]

    current = client.get(f"/api/v1/devices/{device_id}", headers=headers)
    removed = client.delete(
        f"/api/v1/devices/{device_id}/credential",
        headers={**headers, "If-Match": current.headers["ETag"]},
    )
    assert removed.status_code == 204
    assert client.get(f"/api/v1/devices/{device_id}/credential", headers=headers).json() == {
        "configured": False,
        "kind": None,
        "has_password": False,
        "has_ssh_key": False,
        "has_api_key": False,
    }
