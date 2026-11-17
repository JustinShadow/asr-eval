"""M7 设备、部署与说话人注册资源接口集成测试。"""

import io
import uuid
import wave

import pytest
from fastapi.testclient import TestClient

from app.adapters.etsstt import VoiceprintOutcome
from app.core.object_store import LocalObjectStore
from app.domain.models import Deployment, Job, SpeakerEnrollment, Voiceprint
from app.domain.remote_staging import StagedAudio
from app.domain.speaker_registry import wait_for_speaker_tasks

pytestmark = pytest.mark.integration


def _login(client: TestClient) -> dict[str, str]:
    """登录管理员并返回鉴权头。"""
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _device(client: TestClient, headers: dict[str, str]) -> str:
    """创建集成测试设备。"""
    response = client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": f"M7-{uuid.uuid4().hex[:8]}",
            "platform": "205",
            "kind": "etsstt",
            "host": "127.0.0.1",
            "port": 8701,
            "ssh_user": "tester",
        },
    )
    assert response.status_code == 201
    return str(response.json()["id"])


def _wav_bytes() -> bytes:
    """生成 100ms 的有效 PCM WAV 注册素材。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 1600)
    return buffer.getvalue()


def test_deployment_detail_and_device_delete_guard(client: TestClient) -> None:
    """部署详情可读；活动设备 Job 阻止删除，空闲且无历史时删除留审计。"""
    headers = _login(client)
    device_id = _device(client, headers)
    deployment_id = uuid.uuid4()
    with client.app.state.evaluation_session_factory() as db:
        db.add(
            Deployment(
                id=deployment_id,
                device_id=uuid.UUID(device_id),
                uid=uuid.uuid4().hex[:16],
                model_key="test-model",
                capabilities={"observed": {}},
                limits={},
            )
        )
        db.commit()

    detail = client.get(f"/api/v1/deployments/{deployment_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["device_id"] == device_id
    assert detail.json()["model_key"] == "test-model"

    with client.app.state.evaluation_session_factory() as db:
        db.add(
            Job(
                kind="probe",
                ref_type="device",
                ref_id=device_id,
                status="queued",
                created_by="admin",
            )
        )
        db.commit()
    blocked = client.delete(f"/api/v1/devices/{device_id}", headers=headers)
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "E_DEVICE_BUSY"

    with client.app.state.evaluation_session_factory() as db:
        job = db.query(Job).filter_by(ref_type="device", ref_id=device_id).one()
        job.status = "failed"
        db.commit()
    deleted = client.delete(f"/api/v1/devices/{device_id}", headers=headers)
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/devices/{device_id}", headers=headers).status_code == 404
    logs = client.get("/api/v1/audit-logs", headers=headers)
    assert any(
        row["action"] == "device.deleted" and row["target"] == f"device:{device_id}"
        for row in logs.json()["items"]
    )


def test_speaker_enrollment_extraction_is_idempotent_and_persisted(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """注册录音写对象存储；提取 Job 缓存 192 维向量并支持幂等重放。"""
    headers = _login(client)
    store = LocalObjectStore(tmp_path / "objects", "test-signing-key")
    monkeypatch.setattr("app.api.speakers.get_object_store", lambda: store)
    monkeypatch.setattr("app.domain.speaker_registry._object_store", lambda: store)

    speaker_key = f"speaker-create-{uuid.uuid4()}"
    created = client.post(
        "/api/v1/speakers",
        headers={**headers, "Idempotency-Key": speaker_key},
        json={"name": "测试说话人"},
    )
    assert created.status_code == 201
    speaker_id = created.json()["id"]
    replay = client.post(
        "/api/v1/speakers",
        headers={**headers, "Idempotency-Key": speaker_key},
        json={"name": "测试说话人"},
    )
    assert replay.status_code == 201
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json()["id"] == speaker_id

    audio = _wav_bytes()
    enrollment_key = f"enrollment-{uuid.uuid4()}"
    enrollment_response = client.post(
        f"/api/v1/speakers/{speaker_id}/enrollments",
        headers={**headers, "Idempotency-Key": enrollment_key},
        files={"file": ("sample.wav", audio, "audio/wav")},
    )
    assert enrollment_response.status_code == 201
    enrollment_id = enrollment_response.json()["id"]
    assert enrollment_response.json()["duration_ms"] == 100
    enrollments = client.get(f"/api/v1/speakers/{speaker_id}/enrollments", headers=headers)
    assert enrollments.status_code == 200
    assert enrollments.json()["items"][0]["id"] == enrollment_id
    audio_response = client.get(
        f"/api/v1/speakers/{speaker_id}/enrollments/{enrollment_id}/audio",
        headers=headers,
    )
    assert audio_response.status_code == 200
    assert audio_response.headers["content-type"] == "audio/wav"
    assert audio_response.headers["cache-control"] == "private, no-store"
    assert audio_response.headers["x-content-type-options"] == "nosniff"
    assert audio_response.content == audio
    enrollment_replay = client.post(
        f"/api/v1/speakers/{speaker_id}/enrollments",
        headers={**headers, "Idempotency-Key": enrollment_key},
        files={"file": ("sample.wav", audio, "audio/wav")},
    )
    assert enrollment_replay.headers["Idempotent-Replay"] == "true"
    assert enrollment_replay.json()["id"] == enrollment_id

    device_id = _device(client, headers)
    deployment_id = uuid.uuid4()
    with client.app.state.evaluation_session_factory() as db:
        db.add(
            Deployment(
                id=deployment_id,
                device_id=uuid.UUID(device_id),
                uid=uuid.uuid4().hex[:16],
                model_key="test-model",
                capabilities={"observed": {}},
                limits={},
            )
        )
        db.commit()

    missing_status = client.get(f"/api/v1/speakers/{speaker_id}", headers=headers)
    assert missing_status.status_code == 200
    assert missing_status.json()["vector_statuses"][0]["status"] == "missing"

    staged = StagedAudio(
        name="sample.wav",
        remote_path="/data/voiceprint/sample.wav",
        sha256="a" * 64,
        method="sftp",
        host_dir="/data/voiceprint",
        container_dir="/data/voiceprint",
    )
    monkeypatch.setattr("app.domain.speaker_registry.stage_audio", lambda *_args: staged)
    monkeypatch.setattr("app.domain.speaker_registry.cleanup_audio", lambda *_args: None)

    extraction_calls = 0

    async def fake_extract(*_args, **_kwargs) -> VoiceprintOutcome:
        nonlocal extraction_calls
        extraction_calls += 1
        if extraction_calls == 1:
            raise RuntimeError("临时设备错误")
        return VoiceprintOutcome(ok=True, embedding=[0.25] * 192)

    monkeypatch.setattr("app.domain.speaker_registry.extract_voiceprint", fake_extract)
    extract_key = f"extract-{uuid.uuid4()}"
    accepted = client.post(
        f"/api/v1/speakers/{speaker_id}/extract",
        headers={**headers, "Idempotency-Key": extract_key},
        params={"deployment_id": str(deployment_id)},
    )
    assert accepted.status_code == 202
    wait_for_speaker_tasks()
    replay = client.post(
        f"/api/v1/speakers/{speaker_id}/extract",
        headers={**headers, "Idempotency-Key": extract_key},
        params={"deployment_id": str(deployment_id)},
    )
    assert replay.status_code == 202
    assert replay.headers["Idempotent-Replay"] == "true"
    job = client.get(f"/api/v1/jobs/{accepted.json()['id']}", headers=headers)
    assert job.json()["status"] == "succeeded"
    assert job.json()["result"]["vector_count"] == 1
    assert extraction_calls == 2

    vectors = client.get(f"/api/v1/speakers/{speaker_id}/vectors", headers=headers)
    assert vectors.status_code == 200
    assert len(vectors.json()["items"]) == 1
    assert vectors.json()["items"][0]["enrollment_id"] == enrollment_id
    assert vectors.json()["items"][0]["vector"] == [0.25] * 192
    speaker_detail = client.get(f"/api/v1/speakers/{speaker_id}", headers=headers)
    assert speaker_detail.status_code == 200
    assert speaker_detail.json()["vector_statuses"][0]["status"] == "ready"
    assert speaker_detail.json()["vector_statuses"][0]["deployment_id"] == str(deployment_id)
    speaker_list = client.get("/api/v1/speakers", headers=headers)
    assert speaker_list.status_code == 200
    listed_speaker = next(item for item in speaker_list.json()["items"] if item["id"] == speaker_id)
    assert listed_speaker["vector_statuses"][0]["status"] == "ready"
    with client.app.state.evaluation_session_factory() as db:
        enrollment = db.get(SpeakerEnrollment, uuid.UUID(enrollment_id))
        assert enrollment is not None and store.exists(enrollment.object_key)
        assert db.get(Voiceprint, (uuid.UUID(enrollment_id), deployment_id)) is not None
