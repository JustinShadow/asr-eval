"""CI service token 的签发、scope 限制、撤销与密钥保护。"""

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.adapters.etsstt import TranscribeOutcome
from app.core.security import hash_password
from app.domain.models import (
    AppUser,
    AudioAsset,
    Dataset,
    DatasetSubset,
    DatasetVersion,
    Deployment,
    Device,
    Item,
    RefText,
)

pytestmark = pytest.mark.integration


def _ensure_admin(client: TestClient) -> None:
    """其他集成用例会重建共享测试库，因此本用例独立恢复管理员夹具。"""
    factory = client.app.state.evaluation_session_factory
    with factory() as db:
        user = db.get(AppUser, "admin")
        if user is None:
            user = AppUser(username="admin")
            db.add(user)
        user.display_name = "管理员"
        user.role = "admin"
        user.role_source = "local"
        user.password_hash = hash_password("admin12345")
        user.active = True
        db.commit()


def test_service_token_is_scoped_and_revocable(client: TestClient) -> None:
    _ensure_admin(client)
    admin_login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert admin_login.status_code == 200, admin_login.text
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    created = client.post(
        "/api/v1/admin/service-tokens",
        headers=admin_headers,
        json={
            "name": "CI smoke",
            "scopes": ["run:read", "evaluation:read"],
            "expires_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        },
    )
    assert created.status_code == 201
    raw_token = created.json()["token"]
    assert raw_token.startswith("ase_")

    listed = client.get("/api/v1/admin/service-tokens", headers=admin_headers)
    assert listed.status_code == 200
    assert "token" not in listed.json()["items"][0]
    assert "secret_hash" not in listed.json()["items"][0]

    service_headers = {"Authorization": f"Bearer {raw_token}"}
    assert client.get("/api/v1/runs", headers=service_headers).status_code == 200
    assert client.get("/api/v1/devices", headers=service_headers).status_code == 403
    assert client.get("/api/v1/admin/users", headers=service_headers).status_code == 403

    forbidden = client.post(
        "/api/v1/admin/service-tokens",
        headers=admin_headers,
        json={
            "name": "invalid CI token",
            "scopes": ["device:ops"],
            "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    )
    assert forbidden.status_code == 422

    revoked = client.delete(
        f"/api/v1/admin/service-tokens/{created.json()['id']}",
        headers=admin_headers,
    )
    assert revoked.status_code == 204
    assert client.get("/api/v1/runs", headers=service_headers).status_code == 401


def test_service_token_creates_run_and_reads_evaluation_conclusion(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """受限 service token 可创建 Run、读取自动评分结论，供 CI 判定阈值。"""
    _ensure_admin(client)
    admin_login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert admin_login.status_code == 200
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

    factory = client.app.state.evaluation_session_factory
    suffix = uuid.uuid4().hex[:10]
    reference = "CI 评测结论样本"
    with factory() as db:
        dataset = Dataset(name=f"CI 数据集-{suffix}", source_type="synthetic")
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
        asset = AudioAsset(sha256=suffix.ljust(64, "0"), uri=f"/ci/{suffix}.wav")
        device = Device(
            name=f"CI 设备-{suffix}",
            platform="205",
            kind="etsstt",
            host="127.0.0.1",
            port=9,
            status="online",
        )
        db.add_all([subset, asset, device])
        db.flush()
        item = Item(
            dataset_version_id=version.id,
            subset_id=subset.id,
            item_key=f"ci-{suffix}",
            content_uid=suffix.ljust(24, "0"),
            audio_asset_id=asset.id,
            split="test",
            duration_ms=4000,
        )
        db.add(item)
        db.flush()
        db.add_all(
            [
                RefText(
                    item_id=item.id,
                    kind="verbatim",
                    text=reference,
                    sha256=suffix,
                ),
                Deployment(
                    device_id=device.id,
                    uid=suffix[:16],
                    model_key="ci-fake-model",
                    capabilities={},
                    limits={},
                    probe_meta={},
                ),
            ]
        )
        db.commit()
        version_id = str(version.id)
        device_id = str(device.id)

    async def fake_transcribe(*_args: object, **_kwargs: object) -> TranscribeOutcome:
        return TranscribeOutcome(ok=True, text=reference, wall_ms=5)

    monkeypatch.setattr("app.domain.runs.transcribe_file", fake_transcribe)
    token_response = client.post(
        "/api/v1/admin/service-tokens",
        headers=admin_headers,
        json={
            "name": f"CI full flow {suffix}",
            "scopes": ["run:create", "run:read", "evaluation:read"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert token_response.status_code == 201
    service_headers = {"Authorization": f"Bearer {token_response.json()['token']}"}

    created = client.post(
        "/api/v1/runs",
        headers={**service_headers, "Idempotency-Key": f"ci-run-{suffix}"},
        json={
            "name": f"CI Run-{suffix}",
            "dataset_version_id": version_id,
            "device_id": device_id,
            "auto_score": True,
        },
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["run"]["id"]

    deadline = time.monotonic() + 20
    conclusion = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}", headers=service_headers)
        assert response.status_code == 200
        conclusion = response.json()
        evaluation = conclusion.get("evaluation")
        if (
            conclusion["run"]["status"] == "completed"
            and evaluation is not None
            and evaluation["status"] == "completed"
        ):
            break
        time.sleep(0.05)

    assert conclusion is not None
    assert conclusion["run"]["status"] == "completed"
    assert conclusion["evaluation"]["status"] == "completed"
    assert any(
        metric["metric"] == "cer" and metric["value"] == 0 for metric in conclusion["metrics"]
    )
