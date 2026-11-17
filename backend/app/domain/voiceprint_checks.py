"""执行设备声纹自检并将结果写入 ProbeRun、Deployment 和统一 Job。"""

import asyncio
import math
import threading
import uuid
import wave
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.etsstt import extract_voiceprint, transcribe_file
from app.adapters.ssh_tunnel import device_endpoint
from app.core.errors import ApiError
from app.domain.devices import _lease_active
from app.domain.models import Deployment, Device, Job, OpsAction, ProbeRun, StagingProbe
from app.domain.remote_staging import (
    StagedAudio,
    StagingError,
    cleanup_audio,
    stage_audio,
)

ASSET_DIR = Path(__file__).resolve().parents[3] / "assets" / "probe"
STAGING_CACHE_TTL = timedelta(minutes=30)
CONFIDENCE_THRESHOLD = 0.5
_voiceprint_workers: dict[uuid.UUID, threading.Thread] = {}


@dataclass
class VoiceprintCheckOutcome:
    """一次自检的安全摘要；不保留声纹向量和设备原始报文。"""

    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error: str | None = None


def validate_voiceprint_check_request(db: Session, device: Device) -> Deployment:
    """在创建任务前校验设备类型、部署快照、租约和本地素材。"""
    if device.kind not in {"etsstt", "gateway"}:
        raise ApiError("E_UNSUPPORTED", "该设备形态不支持声纹暂存", status_code=422)
    if _lease_active(db, device.id):
        raise ApiError("E_DEVICE_BUSY", "设备正在被 Run 独占，禁止启动声纹自检", status_code=409)
    active_ops = db.scalar(
        select(OpsAction.id).where(
            OpsAction.device_id == device.id,
            OpsAction.status.in_(("queued", "running")),
        )
    )
    if active_ops is not None:
        raise ApiError("E_DEVICE_BUSY", "设备正在执行运维操作，暂不能启动声纹自检", status_code=409)
    deployment = db.scalar(
        select(Deployment)
        .where(Deployment.device_id == device.id)
        .order_by(Deployment.captured_at.desc())
    )
    if deployment is None or not deployment.model_key:
        raise ApiError("E_DEPLOYMENT_REQUIRED", "设备需先完成 probe_lite", status_code=409)
    for filename in ("probe_spk_A.wav", "probe_spk_B.wav", "probe_two_spk_10s.wav"):
        if not (ASSET_DIR / filename).is_file():
            raise ApiError("E_PROBE_ASSET_MISSING", "声纹自检素材缺失", status_code=503)
    return deployment


def start_voiceprint_check(db: Session, device: Device, *, created_by: str | None) -> Job:
    """创建设备声纹自检 Job，并在后台执行。"""
    locked_device = db.scalar(
        select(Device)
        .where(Device.id == device.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_device is None:
        raise ApiError("E_NOT_FOUND", "设备不存在", status_code=404)
    validate_voiceprint_check_request(db, locked_device)
    active_job = db.scalar(
        select(Job.id).where(
            Job.kind.in_(("probe", "voiceprint_check", "speaker_extract")),
            Job.ref_type == "device",
            Job.ref_id == str(device.id),
            Job.status.in_(("queued", "running")),
        )
    )
    if active_job is not None:
        raise ApiError("E_DEVICE_BUSY", "该设备已有探测或声纹自检任务正在运行", status_code=409)
    job = Job(
        kind="voiceprint_check",
        ref_type="device",
        ref_id=str(device.id),
        status="queued",
        progress={"phase": "queued"},
        created_by=created_by,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    thread = threading.Thread(
        target=_run_voiceprint_check_job,
        args=(factory, job.id, device.id),
        daemon=True,
        name=f"device-voiceprint-check-{job.id}",
    )
    _voiceprint_workers[job.id] = thread
    thread.start()
    return job


async def _check_voiceprint(
    db: Session,
    job: Job,
    device: Device,
    deployment: Deployment,
) -> VoiceprintCheckOutcome:
    """执行上传、向量提取、matching 转写和 matched_id 验收。"""
    started = datetime.now(UTC)
    asr_host, asr_port = device_endpoint(device)
    staged: list[StagedAudio] = []
    voiceprint_groups: list[dict[str, Any]] = []
    dimensions: list[int] = []
    cache = db.get(StagingProbe, device.id)
    preferred_method = _cached_method(cache, device, deployment)
    staging_method: str | None = preferred_method
    try:
        for speaker in ("A", "B"):
            filename = f"probe_spk_{speaker}.wav"
            local_path = ASSET_DIR / filename
            _checkpoint(db, job, f"staging_{speaker}")
            try:
                staged_audio = stage_audio(
                    device,
                    local_path,
                    preferred_method=preferred_method,
                )
            except StagingError as exc:
                _save_staging_failure(db, device, deployment, str(exc))
                return VoiceprintCheckOutcome(
                    ok=False,
                    result={"checks": {"staging": False}, "staging_method": "unsupported"},
                    error_code="E_STAGING_UNAVAILABLE",
                    error=str(exc),
                )
            staged.append(staged_audio)
            staging_method = staged_audio.method
            _save_staging_success(db, device, deployment, staged_audio)
            _checkpoint(db, job, f"extracting_{speaker}")
            duration_ms = _audio_duration_ms(local_path)
            extracted = await extract_voiceprint(
                asr_host,
                asr_port,
                str(deployment.model_key),
                staged_audio.remote_path,
                speaker,
                duration_ms,
            )
            if not extracted.ok:
                return VoiceprintCheckOutcome(
                    ok=False,
                    result={
                        "checks": {"staging": True, "extraction": False},
                        "staging_method": staging_method,
                        "extracted_speakers": [group["id"] for group in voiceprint_groups],
                    },
                    error_code="E_VOICEPRINT_EXTRACT",
                    error=extracted.error or f"说话人 {speaker} 声纹提取失败",
                )
            dimensions.append(len(extracted.embedding))
            weight = extracted.sample_duration_ms or duration_ms
            voiceprint_groups.append(
                {
                    "id": speaker,
                    "voiceprints": [
                        {"embedding": extracted.embedding, "weight": weight},
                    ],
                }
            )

        _checkpoint(db, job, "transcribing")
        transcript = await transcribe_file(
            asr_host,
            asr_port,
            str(deployment.model_key),
            str(ASSET_DIR / "probe_two_spk_10s.wav"),
            timeout_s=30,
            diarization=True,
            voiceprints=voiceprint_groups,
            speaker_match_threshold=0.6,
            max_num_spks=2,
        )
        if not transcript.ok:
            return VoiceprintCheckOutcome(
                ok=False,
                result={
                    "checks": {"staging": True, "extraction": True, "transcription": False},
                    "staging_method": staging_method,
                    "voiceprint_dimensions": dimensions,
                },
                error_code="E_VOICEPRINT_TRANSCRIBE",
                error=transcript.error or "声纹匹配转写失败",
            )

        _checkpoint(db, job, "validating_matches")
        matches = _matched_speakers(transcript.speaker_match)
        matched_ids = {str(item["matched_id"]) for item in matches}
        confident_ids = {
            str(item["matched_id"])
            for item in matches
            if float(item["confidence"]) > CONFIDENCE_THRESHOLD
        }
        extraction_ok = len(dimensions) == 2 and dimensions == [192, 192]
        matching_ok = bool(confident_ids)
        ok = extraction_ok and matching_ok
        result = {
            "checks": {
                "staging": True,
                "extraction": extraction_ok,
                "transcription": True,
                "matching": matching_ok,
            },
            "staging_method": staging_method,
            "voiceprint_dimensions": dimensions,
            "matched_speakers": sorted(matched_ids),
            "confident_speakers": sorted(confident_ids),
            "confidence_threshold": CONFIDENCE_THRESHOLD,
            "matches": matches,
            "duration_ms": round((datetime.now(UTC) - started).total_seconds() * 1000),
        }
        return VoiceprintCheckOutcome(
            ok=ok,
            result=result,
            error_code=None if ok else "E_VOICEPRINT_LINK_FAILED",
            error=None if ok else "设备未返回置信度大于 0.5 的 A/B 注册声纹匹配",
        )
    finally:
        for staged_audio in reversed(staged):
            cleanup_audio(device, staged_audio)


def _matched_speakers(speaker_match: dict[str, Any]) -> list[dict[str, Any]]:
    """提取可用于验收的 matched_id 和簇级 confidence。"""
    matches: list[dict[str, Any]] = []
    for cluster_id, speaker in speaker_match.items():
        if not isinstance(speaker, dict):
            continue
        info = speaker.get("match_info")
        if not isinstance(info, dict):
            continue
        matched_id = info.get("matched_id")
        confidence = info.get("confidence")
        if (
            not isinstance(matched_id, str)
            or matched_id not in {"A", "B"}
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
        ):
            continue
        matches.append(
            {
                "cluster_id": str(cluster_id),
                "matched_id": str(matched_id),
                "confidence": float(confidence),
            }
        )
    return matches


def _audio_duration_ms(path: Path) -> int:
    """从固定 PCM WAV 头读取注册音频时长。"""
    with wave.open(str(path), "rb") as audio:
        if audio.getframerate() != 16000 or audio.getnchannels() != 1:
            raise ApiError("E_PROBE_ASSET_INVALID", "注册音频格式不是 16kHz 单声道", 503)
        return round(audio.getnframes() * 1000 / audio.getframerate())


def _cached_method(
    probe: StagingProbe | None, device: Device, deployment: Deployment
) -> str | None:
    """仅复用 30 分钟内且设备配置、deployment uid 未变的暂存方式。"""
    if probe is None or probe.error is not None or probe.deployment_uid != deployment.uid:
        return None
    verified_at = probe.verified_at
    if verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=UTC)
    updated_at = device.updated_at
    if updated_at is not None and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    if datetime.now(UTC) - verified_at > STAGING_CACHE_TTL:
        return None
    if updated_at is not None and updated_at > verified_at:
        return None
    return probe.method


def _save_staging_success(
    db: Session,
    device: Device,
    deployment: Deployment,
    staged_audio: StagedAudio,
) -> None:
    """缓存已通过远端 SHA-256 校验的暂存方式。"""
    row = db.get(StagingProbe, device.id)
    if row is None:
        row = StagingProbe(device_id=device.id, method=staged_audio.method)
        db.add(row)
    row.method = staged_audio.method
    row.host_dir = staged_audio.host_dir
    row.container_dir = staged_audio.container_dir
    row.deployment_uid = deployment.uid
    row.verified_at = datetime.now(UTC)
    row.error = None


def _save_staging_failure(
    db: Session,
    device: Device,
    deployment: Deployment,
    error: str,
) -> None:
    """记录暂存不可用，便于后续诊断并在缓存期内避免重复探测。"""
    row = db.get(StagingProbe, device.id)
    if row is None:
        row = StagingProbe(device_id=device.id, method="unsupported")
        db.add(row)
    row.method = "unsupported"
    row.host_dir = None
    row.container_dir = None
    row.deployment_uid = deployment.uid
    row.verified_at = datetime.now(UTC)
    row.error = error[:1024]


def _checkpoint(db: Session, job: Job, phase: str) -> None:
    """写入 Job 阶段并在安全边界响应取消请求。"""
    db.refresh(job, attribute_names=["status", "cancel_requested_at"])
    if job.status == "cancelled" or job.cancel_requested_at is not None:
        raise ApiError("E_JOB_CANCELLED", "声纹自检任务已取消", status_code=409)
    job.progress = {"phase": phase}
    job.version += 1
    db.commit()


def _run_voiceprint_check_job(
    factory: sessionmaker[Session], job_id: uuid.UUID, device_id: uuid.UUID
) -> None:
    """后台执行自检，并回写部署能力、ProbeRun 和 Job。"""
    db = factory()
    try:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        device = db.get(Device, device_id)
        if job is None or job.status == "cancelled":
            return
        if device is None:
            raise ApiError("E_NOT_FOUND", "设备不存在", status_code=404)
        deployment = db.scalar(
            select(Deployment)
            .where(Deployment.device_id == device.id)
            .order_by(Deployment.captured_at.desc())
        )
        if deployment is None:
            raise ApiError("E_DEPLOYMENT_REQUIRED", "设备需先完成 probe_lite", status_code=409)
        if job.cancel_requested_at is not None:
            _finish_cancelled(job)
            db.commit()
            return

        job.status = "running"
        job.progress = {"phase": "starting"}
        job.started_at = datetime.now(UTC)
        job.version += 1
        db.commit()
        try:
            outcome = asyncio.run(_check_voiceprint(db, job, device, deployment))
        except ApiError as exc:
            if exc.code == "E_JOB_CANCELLED":
                _finish_cancelled(job)
                db.commit()
                return
            outcome = VoiceprintCheckOutcome(
                ok=False,
                result={"checks": {}, "staging_method": None},
                error_code=exc.code,
                error=exc.message,
            )
        except Exception as exc:
            outcome = VoiceprintCheckOutcome(
                ok=False,
                result={"checks": {}, "staging_method": None},
                error_code="E_VOICEPRINT_CHECK_FAILED",
                error=f"{type(exc).__name__}: {exc}",
            )

        db.refresh(job, attribute_names=["status", "cancel_requested_at"])
        if job.status == "cancelled" or job.cancel_requested_at is not None:
            _finish_cancelled(job)
            db.commit()
            return
        _persist_check_result(db, job, device, deployment, outcome)
    except Exception as exc:
        db.rollback()
        job = db.get(Job, job_id)
        if job is not None:
            if isinstance(exc, ApiError) and exc.code == "E_JOB_CANCELLED":
                _finish_cancelled(job)
            else:
                job.status = "failed"
                job.error_code = (
                    exc.code if isinstance(exc, ApiError) else "E_VOICEPRINT_CHECK_FAILED"
                )
                message = exc.message if isinstance(exc, ApiError) else str(exc)
                job.error_detail = {"message": message[:1024]}
                job.progress = {"phase": "failed"}
                job.finished_at = datetime.now(UTC)
            db.commit()
    finally:
        db.close()
        _voiceprint_workers.pop(job_id, None)


def _persist_check_result(
    db: Session,
    job: Job,
    device: Device,
    deployment: Deployment,
    outcome: VoiceprintCheckOutcome,
) -> None:
    """保存声纹链路结果，并同步更新 observed.naming。"""
    checked_at = datetime.now(UTC)
    capabilities = dict(deployment.capabilities or {})
    observed_value = capabilities.get("observed")
    observed = dict(observed_value) if isinstance(observed_value, dict) else {}
    observed["naming"] = outcome.ok
    capabilities["observed"] = observed
    deployment.capabilities = capabilities
    probe_meta = dict(deployment.probe_meta or {})
    probe_meta["voiceprint_check"] = {
        "checked_at": checked_at.isoformat(),
        "probe_audio": "probe_two_spk_10s.wav",
        "result": "ok" if outcome.ok else "failed",
        "confidence_threshold": CONFIDENCE_THRESHOLD,
    }
    deployment.probe_meta = probe_meta

    probe_result = {**outcome.result, "ok": outcome.ok, "checked_at": checked_at.isoformat()}
    duration_ms = outcome.result.get("duration_ms")
    if not isinstance(duration_ms, int):
        started_at = job.started_at or checked_at
        duration_ms = max(0, round((checked_at - started_at).total_seconds() * 1000))
        probe_result["duration_ms"] = duration_ms
    probe = ProbeRun(
        device_id=device.id,
        mode="voiceprint",
        status="ok" if outcome.ok else "failed",
        duration_ms=duration_ms,
        result=probe_result,
        error=outcome.error[:1024] if outcome.error else None,
    )
    db.add(probe)
    db.flush()
    job.result = {
        "resource_id": str(probe.id),
        "url": f"/api/v1/devices/{device.id}/health",
        "deployment_id": str(deployment.id),
        "voiceprint_check": probe_result,
    }
    job.progress = {"phase": "done", "check_status": probe.status}
    if outcome.ok:
        job.status = "succeeded"
        job.error_code = None
        job.error_detail = None
    else:
        job.status = "failed"
        job.error_code = outcome.error_code or "E_VOICEPRINT_LINK_FAILED"
        job.error_detail = {"message": (outcome.error or "声纹链路自检失败")[:1024]}
    job.finished_at = checked_at
    job.version += 1
    db.commit()


def _finish_cancelled(job: Job) -> None:
    """将已取消的声纹自检任务落为统一终态。"""
    job.status = "cancelled"
    job.error_code = "E_JOB_CANCELLED"
    job.error_detail = {"message": "声纹自检任务已取消"}
    job.progress = {"phase": "cancelled"}
    job.finished_at = datetime.now(UTC)
    job.version += 1


def wait_for_voiceprint_check_tasks(timeout_s: float = 30.0) -> None:
    """测试辅助：等待当前声纹自检 Job 线程结束。"""
    for thread in list(_voiceprint_workers.values()):
        thread.join(timeout=timeout_s)
