"""设备登记与探测失败路径的集成测试。"""

import asyncio
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from app.adapters.etsstt import IdentityOutcome, ProbeOutcome, TranscribeOutcome
from app.domain.devices import wait_for_probe_tasks
from app.domain.models import Device

pytestmark = pytest.mark.integration


def _login(client: TestClient) -> str:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert resp.status_code == 200
    return str(resp.json()["access_token"])


def test_offline_device_is_marked_offline(client: TestClient) -> None:
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": "offline-test",
            "platform": "205",
            "kind": "etsstt",
            "host": "127.0.0.1",
            "port": 9,  # discard 端口，必然拒绝
        },
    )
    assert created.status_code == 201
    device_id = created.json()["id"]

    idempotency_headers = {**headers, "Idempotency-Key": f"device-probe-{device_id}"}
    probed = client.post(f"/api/v1/devices/{device_id}/probe", headers=idempotency_headers)
    assert probed.status_code == 202
    job_id = probed.json()["id"]
    assert probed.json()["kind"] == "probe"
    assert probed.headers["Location"] == f"/api/v1/jobs/{job_id}"

    replay = client.post(f"/api/v1/devices/{device_id}/probe", headers=idempotency_headers)
    assert replay.status_code == 202
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json()["id"] == job_id

    wait_for_probe_tasks()
    finished = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert finished.json()["status"] == "failed"
    assert finished.json()["error_code"] == "E_PROBE_FAILED"
    assert finished.json()["result"]["probe"]["status"] == "failed"

    health = client.get(f"/api/v1/devices/{device_id}/health", headers=headers)
    assert health.json()["state"] == "offline"


def test_lite_probe_uses_ssh_tunnel_endpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """快速探测也必须走 device_endpoint，不能直连设备回环 8701。"""
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": f"tunnel-lite-{uuid.uuid4().hex[:8]}",
            "platform": "205",
            "kind": "etsstt",
            "host": "192.0.2.10",
            "port": 8701,
            "ssh_user": "etsme",
            "ssh_port": 22,
        },
    )
    assert created.status_code == 201
    device_id = created.json()["id"]
    seen: list[tuple[str, int]] = []

    def fake_endpoint(_device: Device) -> tuple[str, int]:
        return ("127.0.0.1", 34567)

    async def fake_probe(host: str, port: int, **_kwargs: object) -> ProbeOutcome:
        seen.append((host, port))
        return ProbeOutcome(
            ok=True,
            model_key="etsstt",
            declared={"streaming": True},
            raw={"init": {"type": "ready"}},
        )

    monkeypatch.setattr("app.domain.devices.device_endpoint", fake_endpoint)
    monkeypatch.setattr("app.domain.devices.probe_lite", fake_probe)
    accepted = client.post(f"/api/v1/devices/{device_id}/probe?mode=lite", headers=headers)
    assert accepted.status_code == 202
    wait_for_probe_tasks()
    finished = client.get(f"/api/v1/jobs/{accepted.json()['id']}", headers=headers)
    assert finished.json()["status"] == "succeeded"
    assert seen == [("127.0.0.1", 34567)]


def test_running_probe_job_cancels_before_persisting_probe_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运行中的探测收到取消后，在写 ProbeRun 和设备状态前结束。"""
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": f"cancel-probe-{uuid.uuid4().hex[:8]}",
            "platform": "205",
            "kind": "etsstt",
            "host": "127.0.0.1",
            "port": 9000,
        },
    )
    assert created.status_code == 201
    device_id = created.json()["id"]
    probe_started = threading.Event()
    finish_probe = threading.Event()

    async def slow_probe(_host: str, _port: int, **_kwargs: object) -> ProbeOutcome:
        probe_started.set()
        await asyncio.to_thread(finish_probe.wait, 5)
        return ProbeOutcome(ok=True, model_key="test-model")

    monkeypatch.setattr("app.domain.devices.probe_lite", slow_probe)
    accepted = client.post(f"/api/v1/devices/{device_id}/probe", headers=headers)
    assert accepted.status_code == 202
    job_id = accepted.json()["id"]
    assert probe_started.wait(5)

    cancel = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=headers)
    assert cancel.status_code == 202
    finish_probe.set()
    wait_for_probe_tasks()

    finished = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert finished.json()["status"] == "cancelled"
    assert finished.json()["error_code"] == "E_JOB_CANCELLED"
    device = client.get(f"/api/v1/devices/{device_id}", headers=headers)
    assert device.json()["status"] == "unknown"


def test_identity_probe_is_allowed_during_run_lease_but_lite_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run 租约期间身份检查可用，lite 因会建立 ASR 会话而被拒绝。"""
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": f"identity-probe-{uuid.uuid4().hex[:8]}",
            "platform": "205",
            "kind": "etsstt",
            "host": "127.0.0.1",
            "port": 8701,
        },
    )
    assert created.status_code == 201
    device_id = created.json()["id"]
    with client.app.state.evaluation_session_factory() as db:
        device = db.get(Device, uuid.UUID(device_id))
        assert device is not None
        device.status = "needs_restart"
        device.last_error = "engine_overloaded"
        db.commit()
    outcome = IdentityOutcome(
        ok=True,
        identity={
            "platform": "205",
            "kind": "etsstt",
            "bigversion": "3.2.412",
            "etsllm_version": "1.4.49",
            "accel": {"type": "cann"},
            "model_key": "qwen3-asr-1.7b",
            "model_version": None,
        },
        sn=f"SN-{uuid.uuid4().hex[:12]}",
        soc="gjwg-310p-24g-arm64",
        quant=None,
        raw={"models": [], "system_info": {}},
        version_collection_ok=True,
    )

    async def successful_identity(*_args: object, **_kwargs: object) -> IdentityOutcome:
        return outcome

    monkeypatch.setattr("app.domain.devices._lease_active", lambda *_args: True)
    monkeypatch.setattr("app.domain.devices.probe_identity", successful_identity)
    monkeypatch.setattr(
        "app.domain.devices._schedule_uid_change_voiceprint_check", lambda *_args: None
    )

    accepted = client.post(f"/api/v1/devices/{device_id}/probe?mode=identity", headers=headers)
    assert accepted.status_code == 202
    wait_for_probe_tasks()

    finished = client.get(f"/api/v1/jobs/{accepted.json()['id']}", headers=headers)
    assert finished.json()["status"] == "succeeded"
    assert finished.json()["result"]["probe"]["mode"] == "identity"
    assert finished.json()["result"]["probe"]["result"]["sn"] == outcome.sn
    device = client.get(f"/api/v1/devices/{device_id}", headers=headers)
    assert device.json()["sn"] == outcome.sn
    assert device.json()["status"] == "needs_restart"

    lite = client.post(f"/api/v1/devices/{device_id}/probe?mode=lite", headers=headers)
    assert lite.status_code == 409
    assert lite.json()["code"] == "E_DEVICE_BUSY"

    full = client.post(f"/api/v1/devices/{device_id}/probe?mode=full", headers=headers)
    assert full.status_code == 409
    assert full.json()["code"] == "E_DEVICE_BUSY"


def test_full_probe_persists_observed_capabilities_and_stable_uid(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """full 探测保存实测字段，同时让部署 uid 只随身份字段稳定。"""
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": f"full-probe-{uuid.uuid4().hex[:8]}",
            "platform": "205",
            "kind": "etsstt",
            "host": "127.0.0.1",
            "port": 8701,
        },
    )
    assert created.status_code == 201
    device_id = created.json()["id"]

    identity = IdentityOutcome(
        ok=True,
        identity={
            "platform": "205",
            "kind": "etsstt",
            "bigversion": "3.2.412",
            "etsllm_version": "1.4.49",
            "accel": {"type": "cann"},
            "model_key": "qwen3-asr-1.7b",
            "model_version": None,
        },
        raw={"models": []},
        version_collection_ok=True,
    )
    observed = {
        "partials": True,
        "segments": True,
        "speakers_in_sentence": True,
        "global_spk_list": True,
        "punctuation_output": True,
        "usage_field": True,
        "finalizes_in_stream": True,
    }
    transcription_calls = 0

    async def successful_lite(_host: str, _port: int, **_kwargs: object) -> ProbeOutcome:
        return ProbeOutcome(
            ok=True,
            model_key="qwen3-asr-1.7b",
            declared={"streaming": True, "punctuation": False},
            accel={"type": "cann"},
        )

    async def successful_identity(*_args: object, **_kwargs: object) -> IdentityOutcome:
        return identity

    async def successful_transcription(*_args: object, **kwargs: object) -> TranscribeOutcome:
        nonlocal transcription_calls
        transcription_calls += 1
        assert kwargs["mode"] == "stream"
        assert kwargs["chunk_ms"] == 30
        assert kwargs["input_rate"] == 1.0
        assert kwargs["diarization"] is True
        if transcription_calls == 2:
            return TranscribeOutcome(ok=False, error="synthetic audio probe failed")
        return TranscribeOutcome(ok=True, text="双人探测。", observed=observed)

    monkeypatch.setattr("app.domain.devices.probe_lite", successful_lite)
    monkeypatch.setattr("app.domain.devices.probe_identity", successful_identity)
    monkeypatch.setattr("app.domain.devices.transcribe_file", successful_transcription)
    monkeypatch.setattr(
        "app.domain.devices._schedule_uid_change_voiceprint_check", lambda *_args: None
    )

    uids: list[str] = []
    for attempt in range(2):
        accepted = client.post(
            f"/api/v1/devices/{device_id}/probe?mode=full",
            headers={**headers, "Idempotency-Key": f"full-probe-{device_id}-{attempt}"},
        )
        assert accepted.status_code == 202
        wait_for_probe_tasks()
        finished = client.get(f"/api/v1/jobs/{accepted.json()['id']}", headers=headers)
        assert finished.json()["status"] == "succeeded"
        assert finished.json()["result"]["probe"]["mode"] == "full"
        refreshed = client.get(f"/api/v1/devices/{device_id}", headers=headers)
        latest = refreshed.json()["latest_deployment"]
        uids.append(latest["uid"])
        if attempt == 0:
            assert latest["capabilities"]["observed"] == {**observed, "naming": None}
            assert latest["probe_meta"]["observed_missing"] == ["naming"]
        else:
            assert latest["capabilities"]["observed"] == {"naming": None}
            assert latest["probe_meta"]["observed_missing"] == [
                "partials",
                "segments",
                "speakers_in_sentence",
                "global_spk_list",
                "punctuation_output",
                "usage_field",
                "finalizes_in_stream",
                "naming",
            ]

    detail = client.get(f"/api/v1/devices/{device_id}", headers=headers)
    assert detail.status_code == 200
    deployment = detail.json()["latest_deployment"]
    assert deployment["uid"] == uids[0] == uids[1]
    assert deployment["capabilities"]["declared"] == {
        "streaming": True,
        "punctuation": False,
    }
    assert deployment["capabilities"]["observed"] == {"naming": None}
    assert deployment["probe_meta"]["probe_version"] == "probe-full-1"
    assert deployment["probe_meta"]["probe_audio"] == "probe_two_spk_10s.wav"
    assert deployment["probe_meta"]["observed_missing"][-1] == "naming"


def test_cancelled_full_probe_propagates_abort_to_transcription(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消 full Job 时向 WS 转写传递 abort，避免探测留下未收尾会话。"""
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": f"cancel-full-{uuid.uuid4().hex[:8]}",
            "platform": "205",
            "kind": "etsstt",
            "host": "127.0.0.1",
            "port": 8701,
        },
    )
    assert created.status_code == 201
    device_id = created.json()["id"]
    transcription_started = threading.Event()

    async def successful_lite(_host: str, _port: int, **_kwargs: object) -> ProbeOutcome:
        return ProbeOutcome(ok=True, model_key="qwen3-asr-1.7b")

    async def successful_identity(*_args: object, **_kwargs: object) -> IdentityOutcome:
        return IdentityOutcome(
            ok=True,
            identity={
                "platform": "205",
                "kind": "etsstt",
                "bigversion": "3.2.412",
                "etsllm_version": "1.4.49",
                "accel": {"type": "cann"},
                "model_key": "qwen3-asr-1.7b",
                "model_version": None,
            },
            version_collection_ok=True,
        )

    async def wait_for_abort(*_args: object, **kwargs: object) -> TranscribeOutcome:
        abort_event = kwargs["abort_event"]
        assert isinstance(abort_event, asyncio.Event)
        transcription_started.set()
        await abort_event.wait()
        return TranscribeOutcome(ok=False, error="aborted_condition_changed")

    monkeypatch.setattr("app.domain.devices.probe_lite", successful_lite)
    monkeypatch.setattr("app.domain.devices.probe_identity", successful_identity)
    monkeypatch.setattr("app.domain.devices.transcribe_file", wait_for_abort)

    accepted = client.post(
        f"/api/v1/devices/{device_id}/probe?mode=full",
        headers={**headers, "Idempotency-Key": f"cancel-full-{device_id}"},
    )
    assert accepted.status_code == 202
    assert transcription_started.wait(5)
    cancelled = client.post(f"/api/v1/jobs/{accepted.json()['id']}/cancel", headers=headers)
    assert cancelled.status_code == 202
    wait_for_probe_tasks()

    finished = client.get(f"/api/v1/jobs/{accepted.json()['id']}", headers=headers)
    assert finished.json()["status"] == "cancelled"
    detail = client.get(f"/api/v1/devices/{device_id}", headers=headers)
    assert detail.json()["latest_deployment"] is None
