"""M2 声纹链路自检与 M7 Job 接口集成验收。"""

import asyncio
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.adapters.etsstt import ProbeOutcome, TranscribeOutcome, VoiceprintOutcome
from app.core.errors import ApiError
from app.domain.devices import wait_for_probe_tasks
from app.domain.models import (
    Benchmark,
    Dataset,
    DatasetVersion,
    Deployment,
    Device,
    Job,
    MetricProfile,
    ProbeRun,
    Run,
    RunTarget,
    StagingProbe,
)
from app.domain.remote_staging import StagedAudio
from app.domain.runs import _acquire_lease
from app.domain.voiceprint_checks import wait_for_voiceprint_check_tasks

pytestmark = pytest.mark.integration


def _login(client: TestClient) -> dict[str, str]:
    """登录管理员并返回 API 鉴权头。"""
    client.cookies.clear()
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_device(client: TestClient, headers: dict[str, str], *, kind: str = "etsstt") -> str:
    """创建集成测试设备。"""
    response = client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": f"声纹自检-{uuid.uuid4().hex[:8]}",
            "platform": "502" if kind == "gateway" else "205",
            "kind": kind,
            "host": "127.0.0.1",
            "port": 9000,
            "ssh_user": "tester" if kind == "etsstt" else None,
        },
    )
    assert response.status_code == 201
    return str(response.json()["id"])


def _add_deployment(client: TestClient, device_id: str) -> None:
    """为设备写入已完成 probe_lite 的部署快照。"""
    factory = client.app.state.evaluation_session_factory
    with factory() as db:
        db.add(
            Deployment(
                device_id=uuid.UUID(device_id),
                uid=uuid.uuid4().hex[:16],
                model_key="test-stt-model",
                capabilities={"observed": {"naming": None}},
            )
        )
        db.commit()


def _fake_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    """用可追踪的假暂存替代真实 SSH/kubectl。"""

    def stage(
        device: Device, local_path: Path, *, preferred_method: str | None = None
    ) -> StagedAudio:
        method = preferred_method or ("sftp" if device.kind == "etsstt" else "exec_cat")
        remote_dir = "/data/voiceprint" if device.kind == "etsstt" else "/mnt/voiceprint"
        return StagedAudio(
            name=local_path.name,
            remote_path=f"{remote_dir}/{local_path.name}",
            sha256="a" * 64,
            method=method,
            host_dir=remote_dir if device.kind == "etsstt" else None,
            container_dir=remote_dir,
        )

    monkeypatch.setattr("app.domain.voiceprint_checks.stage_audio", stage)
    monkeypatch.setattr("app.domain.voiceprint_checks.cleanup_audio", lambda _device, _staged: None)


def _fake_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回格式有效的两组 192 维声纹。"""

    async def extract(*_args: Any, **_kwargs: Any) -> VoiceprintOutcome:
        return VoiceprintOutcome(ok=True, embedding=[0.25] * 192, sample_duration_ms=6000)

    monkeypatch.setattr("app.domain.voiceprint_checks.extract_voiceprint", extract)


def _matching_result() -> dict[str, Any]:
    return {
        "0": {"match_info": {"matched_id": "A", "confidence": 0.91}},
        "1": {"match_info": {"matched_id": "B", "confidence": 0.86}},
    }


def _fake_transcription(
    monkeypatch: pytest.MonkeyPatch, speaker_match: dict[str, Any] | None = None
) -> None:
    """验证匹配模式参数，并返回指定的设备 matching 结果。"""

    async def transcribe(*_args: Any, **kwargs: Any) -> TranscribeOutcome:
        assert kwargs["diarization"] is True
        assert kwargs["speaker_match_threshold"] == 0.6
        assert kwargs["max_num_spks"] == 2
        voiceprints = kwargs["voiceprints"]
        assert [group["id"] for group in voiceprints] == ["A", "B"]
        assert all(len(group["voiceprints"][0]["embedding"]) == 192 for group in voiceprints)
        return TranscribeOutcome(
            ok=True,
            text="甲说话，乙回答。",
            speaker_match=speaker_match or _matching_result(),
        )

    monkeypatch.setattr("app.domain.voiceprint_checks.transcribe_file", transcribe)


def test_voiceprint_check_succeeds_and_is_idempotent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """匹配成功后保存命名能力、ProbeRun、暂存缓存及 Job 结果。"""
    headers = _login(client)
    _fake_staging(monkeypatch)
    _fake_extraction(monkeypatch)
    _fake_transcription(monkeypatch)
    device_id = _create_device(client, headers)
    _add_deployment(client, device_id)
    request_headers = {**headers, "Idempotency-Key": f"voiceprint-{uuid.uuid4().hex}"}

    accepted = client.post(f"/api/v1/devices/{device_id}/voiceprint-check", headers=request_headers)
    assert accepted.status_code == 202
    job_id = accepted.json()["id"]
    assert accepted.json()["kind"] == "voiceprint_check"
    assert accepted.headers["Location"] == f"/api/v1/jobs/{job_id}"
    wait_for_voiceprint_check_tasks()

    finished = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert finished.json()["status"] == "succeeded"
    assert finished.json()["result"]["voiceprint_check"]["matched_speakers"] == ["A", "B"]
    replay = client.post(f"/api/v1/devices/{device_id}/voiceprint-check", headers=request_headers)
    assert replay.status_code == 202
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json()["id"] == job_id

    factory = client.app.state.evaluation_session_factory
    with factory() as db:
        device = db.get(Device, uuid.UUID(device_id))
        assert device is not None
        deployment = db.query(Deployment).filter_by(device_id=device.id).one()
        assert deployment.capabilities["observed"]["naming"] is True
        assert deployment.probe_meta["voiceprint_check"]["result"] == "ok"
        assert db.query(ProbeRun).filter_by(device_id=device.id, mode="voiceprint").count() == 1
        cache = db.get(StagingProbe, device.id)
        assert cache is not None and cache.method == "sftp"


def test_probe_lite_schedules_voiceprint_check_only_for_new_deployment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首次部署 UID 自动排自检；同 UID 重探保留命名结果且不重复排队。"""
    headers = _login(client)
    _fake_staging(monkeypatch)
    _fake_extraction(monkeypatch)
    _fake_transcription(monkeypatch)

    async def successful_probe(_host: str, _port: int, **_kwargs: object) -> ProbeOutcome:
        return ProbeOutcome(ok=True, model_key="auto-check-model")

    monkeypatch.setattr("app.domain.devices.probe_lite", successful_probe)
    device_id = _create_device(client, headers)

    first_probe = client.post(f"/api/v1/devices/{device_id}/probe", headers=headers)
    assert first_probe.status_code == 202
    wait_for_probe_tasks()
    first_job = client.get(f"/api/v1/jobs/{first_probe.json()['id']}", headers=headers)
    assert first_job.json()["status"] == "succeeded"
    auto_check_id = first_job.json()["result"]["voiceprint_check"]["job_id"]
    wait_for_voiceprint_check_tasks()
    auto_check = client.get(f"/api/v1/jobs/{auto_check_id}", headers=headers)
    assert auto_check.json()["status"] == "succeeded"

    second_probe = client.post(f"/api/v1/devices/{device_id}/probe", headers=headers)
    assert second_probe.status_code == 202
    wait_for_probe_tasks()
    second_job = client.get(f"/api/v1/jobs/{second_probe.json()['id']}", headers=headers)
    assert second_job.json()["result"].get("voiceprint_check") is None

    factory = client.app.state.evaluation_session_factory
    with factory() as db:
        deployment = db.query(Deployment).filter_by(device_id=uuid.UUID(device_id)).one()
        assert deployment.capabilities["observed"]["naming"] is True
        assert (
            db.query(Job)
            .filter_by(kind="voiceprint_check", ref_type="device", ref_id=device_id)
            .count()
            == 1
        )


def test_voiceprint_check_failure_marks_naming_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """設備未匹配回兩個註冊 id 時保留失败诊断并将 naming 置为 false。"""
    headers = _login(client)
    _fake_staging(monkeypatch)
    _fake_extraction(monkeypatch)
    _fake_transcription(
        monkeypatch,
        {
            "0": {"match_info": {"matched_id": "A", "confidence": 0.5}},
            "1": {"match_info": {"matched_id": "B", "confidence": 0.49}},
        },
    )
    device_id = _create_device(client, headers)
    _add_deployment(client, device_id)

    accepted = client.post(f"/api/v1/devices/{device_id}/voiceprint-check", headers=headers)
    assert accepted.status_code == 202
    wait_for_voiceprint_check_tasks()

    finished = client.get(f"/api/v1/jobs/{accepted.json()['id']}", headers=headers)
    assert finished.json()["status"] == "failed"
    assert finished.json()["error_code"] == "E_VOICEPRINT_LINK_FAILED"
    check = finished.json()["result"]["voiceprint_check"]
    assert check["matched_speakers"] == ["A", "B"]
    assert check["checks"]["matching"] is False
    factory = client.app.state.evaluation_session_factory
    with factory() as db:
        deployment = db.query(Deployment).filter_by(device_id=uuid.UUID(device_id)).one()
        assert deployment.capabilities["observed"]["naming"] is False
        probe = db.query(ProbeRun).filter_by(device_id=device_id, mode="voiceprint").one()
        assert probe.status == "failed"
        assert probe.duration_ms is not None


def test_voiceprint_check_requires_deployment_and_rejects_active_lease(
    client: TestClient,
) -> None:
    """未完成 probe_lite 或设备已被 Run 租用时拒绝声纹检查。"""
    headers = _login(client)
    device_id = _create_device(client, headers)
    no_deployment = client.post(f"/api/v1/devices/{device_id}/voiceprint-check", headers=headers)
    assert no_deployment.status_code == 409
    assert no_deployment.json()["code"] == "E_DEPLOYMENT_REQUIRED"

    factory = client.app.state.evaluation_session_factory
    with factory() as db:
        device = db.get(Device, uuid.UUID(device_id))
        assert device is not None
        dataset = Dataset(name=f"声纹租约-{uuid.uuid4().hex[:8]}", source_type="test")
        db.add(dataset)
        db.flush()
        version = DatasetVersion(
            dataset_id=dataset.id,
            version="v1",
            status="ready",
        )
        profile = MetricProfile(
            name=f"声纹租约-{uuid.uuid4().hex[:8]}",
            params={},
            profile_hash=uuid.uuid4().hex[:16],
        )
        db.add_all([version, profile])
        db.flush()
        benchmark = Benchmark(
            name=f"声纹租约-{uuid.uuid4().hex[:8]}",
            dataset_version_id=version.id,
            metric_profile_id=profile.id,
            subsets=["all"],
            sampling={},
            params={},
            bucket_rules={},
        )
        db.add(benchmark)
        db.flush()
        run = Run(benchmark_id=benchmark.id, name="声纹占用", created_by="admin")
        db.add(run)
        db.flush()
        target = RunTarget(run_id=run.id, device_id=device.id)
        db.add(target)
        db.flush()
        db.add(
            Deployment(
                device_id=device.id,
                uid=uuid.uuid4().hex[:16],
                model_key="test-stt-model",
            )
        )
        active_check = Job(
            kind="voiceprint_check",
            ref_type="device",
            ref_id=str(device.id),
            status="running",
            created_by="admin",
        )
        db.add(active_check)
        db.commit()
        target_id = target.id
        run_id = run.id
        active_check_id = active_check.id

    with factory() as db:
        with pytest.raises(ApiError) as busy_run:
            _acquire_lease(db, uuid.UUID(device_id), target_id, str(run_id))
        assert busy_run.value.code == "E_DEVICE_BUSY"

    with factory() as db:
        active_check = db.get(Job, active_check_id)
        assert active_check is not None
        db.delete(active_check)
        _acquire_lease(db, uuid.UUID(device_id), target_id, str(run_id))

    busy = client.post(f"/api/v1/devices/{device_id}/voiceprint-check", headers=headers)
    assert busy.status_code == 409
    assert busy.json()["code"] == "E_DEVICE_BUSY"


def test_running_voiceprint_check_cancels_before_persisting_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运行中取消在声纹提取边界生效，且不会回写能力结果。"""
    headers = _login(client)
    factory = client.app.state.evaluation_session_factory
    with factory() as db:
        queued_job = Job(
            kind="voiceprint_check",
            ref_type="device",
            ref_id=str(uuid.uuid4()),
            status="queued",
            created_by="admin",
        )
        db.add(queued_job)
        db.commit()
        queued_job_id = str(queued_job.id)
    queued_cancel = client.post(f"/api/v1/jobs/{queued_job_id}/cancel", headers=headers)
    assert queued_cancel.status_code == 200
    assert queued_cancel.json()["status"] == "cancelled"
    assert queued_cancel.json()["error_detail"]["message"] == "声纹自检任务已取消"

    _fake_staging(monkeypatch)
    _fake_transcription(monkeypatch)
    device_id = _create_device(client, headers)
    _add_deployment(client, device_id)
    extraction_started = threading.Event()
    finish_extraction = threading.Event()

    async def slow_extract(*_args: Any, **_kwargs: Any) -> VoiceprintOutcome:
        extraction_started.set()
        await asyncio.to_thread(finish_extraction.wait, 5)
        return VoiceprintOutcome(ok=True, embedding=[0.25] * 192, sample_duration_ms=6000)

    monkeypatch.setattr("app.domain.voiceprint_checks.extract_voiceprint", slow_extract)
    accepted = client.post(f"/api/v1/devices/{device_id}/voiceprint-check", headers=headers)
    assert accepted.status_code == 202
    job_id = accepted.json()["id"]
    assert extraction_started.wait(5)

    cancelled = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=headers)
    assert cancelled.status_code == 202
    finish_extraction.set()
    wait_for_voiceprint_check_tasks()

    finished = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert finished.json()["status"] == "cancelled"
    with factory() as db:
        deployment = db.query(Deployment).filter_by(device_id=uuid.UUID(device_id)).one()
        assert deployment.capabilities["observed"]["naming"] is None
        assert (
            db.query(ProbeRun).filter_by(device_id=uuid.UUID(device_id), mode="voiceprint").count()
            == 0
        )
        job = db.get(Job, uuid.UUID(job_id))
        assert job is not None and job.error_code == "E_JOB_CANCELLED"
