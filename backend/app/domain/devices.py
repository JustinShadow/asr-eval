"""设备注册与探测服务。"""

import asyncio
import hashlib
import json
import logging
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.cloud import probe_cloud_models
from app.adapters.etsstt import (
    IdentityOutcome,
    ProbeOutcome,
    TranscribeOutcome,
    probe_identity,
    probe_lite,
    transcribe_file,
)
from app.adapters.ssh_tunnel import device_endpoint
from app.core.errors import ApiError
from app.domain.credentials import CredentialUnavailable, credential_for_device
from app.domain.models import Deployment, Device, DeviceLease, Job, OpsAction, ProbeRun

IDENTITY_FIELDS = (
    "platform",
    "kind",
    "bigversion",
    "etsllm_version",
    "accel",
    "model_key",
    "model_version",
)
_probe_workers: dict[uuid.UUID, threading.Thread] = {}
logger = logging.getLogger(__name__)
_FULL_PROBE_OBSERVATIONS = (
    "partials",
    "segments",
    "speakers_in_sentence",
    "global_spk_list",
    "punctuation_output",
    "usage_field",
    "finalizes_in_stream",
    "naming",
)
# M2 §5.2 指定随代码管理的固定双人素材，保证设备能力探测可重复。
_FULL_PROBE_AUDIO = Path(__file__).resolve().parents[3] / "assets/probe/probe_two_spk_10s.wav"


def compute_uid(identity: dict[str, Any]) -> str:
    """按 M2 §2.4 计算 16 位部署指纹。

    host/port/时间/能力/性能都不参与，避免"换网络就算新部署"。
    """
    canonical = {key: identity.get(key) for key in IDENTITY_FIELDS}
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _lease_active(db: Session, device_id: Any) -> bool:
    """设备是否被 Run 独占（有未过期租约，M2 §5.3.1）。"""
    lease = db.scalar(
        select(DeviceLease).where(
            DeviceLease.device_id == device_id, DeviceLease.expires_at > datetime.now(UTC)
        )
    )
    return lease is not None


async def run_probe(
    db: Session, device: Device, mode: str = "lite", *, job: Job | None = None
) -> tuple[ProbeRun, Deployment | None]:
    """执行设备探测并落库，支持 lite、full 与 identity 模式。

    租约联动（M2 §5.3.1）：Run 独占期间禁止会建立 ASR 会话的 lite/full，
    只允许不建会话的 identity 检查。
    """
    mode = validate_probe_request(db, device, mode)

    started_at = datetime.now(UTC)
    started = time.perf_counter()
    deployment: Deployment | None = None
    probe_status = "failed"
    if mode == "full" and device.kind == "cloud":
        probe_result, deployment, probe_error, full_probe_ok = await _run_full_probe(
            db, device, started_at, job
        )
        probe_status = "ok" if full_probe_ok else "failed"
    elif mode == "identity":
        ssh_credentials = _ssh_credential_kwargs(db, device)
        asr_host, asr_port = device_endpoint(device)
        identity_outcome = await probe_identity(
            asr_host,
            asr_port,
            ssh_host=device.host,
            system_info_host=device.host,
            kind=device.kind,
            platform=device.platform,
            ssh_user=device.ssh_user,
            ssh_port=device.ssh_port,
            ssh_password=ssh_credentials.get("ssh_password"),
            ssh_private_key=ssh_credentials.get("ssh_private_key"),
            ssh_passphrase=ssh_credentials.get("ssh_passphrase"),
        )
        _check_probe_cancellation(db, job)
        if identity_outcome.ok:
            _check_device_serial_number(db, device, identity_outcome)
            deployment = _upsert_identity_deployment(db, device, identity_outcome, started_at)
            device.platform = str(identity_outcome.identity["platform"])
            if identity_outcome.sn is not None:
                device.sn = identity_outcome.sn
            if device.status != "needs_restart":
                device.status = "online" if identity_outcome.version_collection_ok else "degraded"
                device.last_error = (
                    "；".join(identity_outcome.warnings)[:1024]
                    if identity_outcome.warnings
                    else None
                )
        else:
            if device.status != "needs_restart":
                device.status = "offline"
                device.last_error = (identity_outcome.error or "设备身份检查失败")[:1024]
        probe_result = _identity_probe_result(identity_outcome)
        probe_error = identity_outcome.error
        probe_status = "ok" if deployment is not None else "failed"
    elif mode == "full":
        probe_result, deployment, probe_error, full_probe_ok = await _run_full_probe(
            db, device, started_at, job
        )
        probe_status = "ok" if full_probe_ok else "failed"
    else:
        # init 超时重试 1 次（M2 §6 异常表）；其余失败不重试。
        fallback_model = "etsstt" if device.kind == "etsstt" else None
        asr_host, asr_port = device_endpoint(device)
        outcome = await probe_lite(asr_host, asr_port, fallback_model=fallback_model)
        _check_probe_cancellation(db, job)
        if not outcome.ok and "TimeoutError" in (outcome.error or ""):
            outcome = await probe_lite(asr_host, asr_port, fallback_model=fallback_model)
        _check_probe_cancellation(db, job)
        if outcome.ok:
            deployment = _upsert_deployment(db, device, outcome, started_at)
            if device.status != "needs_restart":
                device.status = "online"
                device.last_error = None
        else:
            if device.status != "needs_restart":
                device.status = "offline"
                device.last_error = (outcome.error or "设备探测失败")[:1024]
        probe_result = _probe_result(outcome)
        probe_error = outcome.error
        probe_status = "ok" if deployment is not None else "failed"

    # duration_ms 记录本次采集耗时；init 往返延迟只放 lite 结果里。
    duration_ms = int((time.perf_counter() - started) * 1000)
    probe = ProbeRun(
        device_id=device.id,
        mode=mode,
        status=probe_status,
        duration_ms=duration_ms,
        result=probe_result,
        error=probe_error,
    )
    db.add(probe)
    device.version += 1
    device.updated_at = datetime.now(UTC)

    db.commit()
    db.refresh(probe)
    if deployment is not None:
        db.refresh(deployment)
    return probe, deployment


async def _run_full_probe(
    db: Session,
    device: Device,
    captured_at: datetime,
    job: Job | None,
) -> tuple[dict[str, Any], Deployment | None, str | None, bool]:
    """按 M2 §5.2 采集身份、声明与双人流式探测能力。"""
    if device.kind == "cloud":
        try:
            credential = credential_for_device(db, device)
        except CredentialUnavailable as exc:
            message = str(exc)
            return {"ok": False, "error": message}, None, message, False
        if credential is None or credential.kind != "cloud_api_key":
            message = "请先保存云端 API Key"
            return {"ok": False, "error": message}, None, message, False
        outcome = await probe_cloud_models(device.host, device.port, credential.secret)
        device.status = "online" if outcome.ok else "offline"
        device.last_error = outcome.error
        return (
            {
                "ok": outcome.ok,
                "identity": {"kind": "cloud"},
                "declared": {},
                "observed": {},
                "model_count": outcome.model_count,
                "warnings": [] if outcome.ok else [outcome.error or "云端 Key 校验失败"],
            },
            None,
            outcome.error,
            outcome.ok,
        )

    ssh_credentials = _ssh_credential_kwargs(db, device)

    fallback_model = "etsstt" if device.kind == "etsstt" else None
    asr_host, asr_port = device_endpoint(device)
    lite = await probe_lite(asr_host, asr_port, fallback_model=fallback_model)
    _check_probe_cancellation(db, job)
    if not lite.ok and "TimeoutError" in (lite.error or ""):
        lite = await probe_lite(asr_host, asr_port, fallback_model=fallback_model)
    _check_probe_cancellation(db, job)

    identity = await probe_identity(
        asr_host,
        asr_port,
        ssh_host=device.host,
        system_info_host=device.host,
        kind=device.kind,
        platform=device.platform,
        ssh_user=device.ssh_user,
        ssh_port=device.ssh_port,
        ssh_password=ssh_credentials.get("ssh_password"),
        ssh_private_key=ssh_credentials.get("ssh_private_key"),
        ssh_passphrase=ssh_credentials.get("ssh_passphrase"),
    )
    _check_probe_cancellation(db, job)
    if identity.ok:
        _check_device_serial_number(db, device, identity)

    model_key = lite.model_key if lite.ok else _identity_text(identity.identity.get("model_key"))
    transcription: TranscribeOutcome | None = None
    if model_key is not None:
        abort_event = asyncio.Event()
        cancellation_watcher = asyncio.create_task(_watch_probe_cancellation(db, job, abort_event))
        try:
            transcription = await transcribe_file(
                asr_host,
                asr_port,
                model_key,
                str(_FULL_PROBE_AUDIO),
                timeout_s=20,
                diarization=True,
                abort_event=abort_event,
                mode="stream",
                chunk_ms=30,
                input_rate=1.0,
            )
        finally:
            cancellation_watcher.cancel()
            await asyncio.gather(cancellation_watcher, return_exceptions=True)
        _check_probe_cancellation(db, job)

    deployment: Deployment | None = None
    if identity.ok:
        deployment = _upsert_identity_deployment(db, device, identity, captured_at)
        device.platform = str(identity.identity["platform"])
        if identity.sn is not None:
            device.sn = identity.sn
    elif lite.ok:
        # HTTP 身份端点或版本采集失败时，保留 probe_lite 能取得的部分部署信息。
        deployment = _upsert_deployment(db, device, lite, captured_at)

    observed: dict[str, Any] = {}
    observation_error: str | None = None
    if transcription is not None and transcription.ok:
        observed.update(transcription.observed)
    elif model_key is None:
        observation_error = "没有可用于探测音频的 STT 模型"
    elif transcription is not None:
        observation_error = transcription.error or "探测音频未能完成"
    else:
        observation_error = "身份和模型探测都未返回可用 STT 模型"
    # naming 由 M2 §5.2.1 的声纹链路自检单独验证，full 不自行推断。
    observed["naming"] = None
    observed_missing = [
        key for key in _FULL_PROBE_OBSERVATIONS if not isinstance(observed.get(key), bool)
    ]

    warnings = list(identity.warnings)
    if not lite.ok:
        warnings.append(f"声明能力探测失败：{lite.error or '未知错误'}")
    if observation_error:
        warnings.append(f"实测能力探测失败：{observation_error}")
    if deployment is not None:
        declared: dict[str, Any] = dict(lite.declared) if lite.ok else {}
        deployment.capabilities = {"declared": declared, "observed": observed}
        deployment.limits = _limits_from(declared, device.kind)
        deployment.probe_meta = {
            "probed_at": captured_at.isoformat(),
            "probe_version": "probe-full-1",
            "probe_audio": "probe_two_spk_10s.wav",
            "observed_missing": observed_missing,
            "warnings": warnings,
        }
        deployment.raw = {
            "identity": identity.raw,
            "lite": lite.raw,
            "observation_error": observation_error,
        }
        deployment.captured_at = captured_at

    if device.status != "needs_restart":
        if identity.ok:
            device.status = "online" if identity.version_collection_ok else "degraded"
            device.last_error = "；".join(identity.warnings)[:1024] or None
        elif lite.ok:
            device.status = "degraded"
            device.last_error = (identity.error or "设备身份采集不完整")[:1024]
        else:
            device.status = "offline"
            device.last_error = (identity.error or lite.error or "设备探测失败")[:1024]

    result = {
        "ok": identity.ok,
        "identity": _identity_probe_result(identity),
        "declared": lite.declared if lite.ok else {},
        "observed": observed,
        "observed_missing": observed_missing,
        "observation_error": observation_error,
        "warnings": warnings,
    }
    # M2 §5.2 将音频观测作为降级信息；身份采集成功即可保存部署并完成 Job。
    probe_error = None if identity.ok else identity.error or lite.error or observation_error
    return result, deployment, probe_error, identity.ok


async def _watch_probe_cancellation(
    db: Session, job: Job | None, abort_event: asyncio.Event
) -> None:
    """将 Job 取消请求转成协议层中止信号，供 WebSocket 优雅 close。"""
    if job is None:
        return
    while not abort_event.is_set():
        await asyncio.sleep(0.1)
        db.refresh(job, attribute_names=["status", "cancel_requested_at"])
        if job.status == "cancelled" or job.cancel_requested_at is not None:
            abort_event.set()


def resolve_probe_mode(db: Session, device: Device, requested: str) -> str:
    """把 auto 解析为 full 或 identity：Run 占用时只做不建会话的身份检查。"""
    if requested != "auto":
        return requested
    if device.kind == "cloud":
        return "full"
    return "identity" if _lease_active(db, device.id) else "full"


def validate_probe_request(db: Session, device: Device, mode: str) -> str:
    """在创建异步 Job 前校验探测模式并返回实际执行模式。"""
    if mode not in {"auto", "lite", "identity", "full"}:
        raise ApiError(
            "E_SEMANTIC", "当前支持 auto、lite、identity 与 full 探测模式", status_code=422
        )
    mode = resolve_probe_mode(db, device, mode)
    if mode == "identity" and device.kind == "cloud":
        raise ApiError("E_SEMANTIC", "云端设备暂不支持本地设备身份检查", status_code=422)
    if mode == "full" and device.kind == "cloud":
        try:
            credential = credential_for_device(db, device)
        except CredentialUnavailable as exc:
            raise ApiError("E_CREDENTIAL_UNAVAILABLE", str(exc), status_code=503) from exc
        if credential is None or credential.kind != "cloud_api_key":
            raise ApiError("E_CREDENTIAL_REQUIRED", "请先保存云端 API Key", status_code=409)
    active_ops = db.scalar(
        select(OpsAction.id).where(
            OpsAction.device_id == device.id,
            OpsAction.status.in_(("queued", "running")),
        )
    )
    if active_ops is not None:
        raise ApiError("E_DEVICE_BUSY", "设备正在执行运维操作，暂不能探测", status_code=409)
    if mode in {"lite", "full"} and _lease_active(db, device.id):
        raise ApiError(
            "E_DEVICE_BUSY",
            "设备正在被 Run 独占，能力探测会创建 ASR 会话",
            status_code=409,
        )
    return mode


def _ssh_credential_kwargs(db: Session, device: Device) -> dict[str, str]:
    """仅为本地设备身份采集提取 SSH 登录参数，不记录或返回凭据。"""
    try:
        credential = credential_for_device(db, device)
    except CredentialUnavailable as exc:
        raise ApiError("E_CREDENTIAL_UNAVAILABLE", str(exc), status_code=503) from exc
    if credential is None:
        return {}
    if credential.kind not in {"ssh_password", "ssh_private_key"}:
        raise ApiError("E_CREDENTIAL_UNAVAILABLE", "当前设备保存的凭据类型不匹配", status_code=503)
    if credential.kind == "ssh_password":
        return {"ssh_password": credential.secret}
    result = {"ssh_private_key": credential.secret}
    if credential.passphrase is not None:
        result["ssh_passphrase"] = credential.passphrase
    return result


def _check_device_serial_number(db: Session, device: Device, outcome: IdentityOutcome) -> None:
    """防止探测到的序列号覆盖另一台已登记设备的身份。"""
    if outcome.sn is None:
        return
    existing_id = db.scalar(
        select(Device.id).where(Device.sn == outcome.sn, Device.id != device.id)
    )
    if existing_id is not None:
        raise ApiError(
            "E_DEVICE_SN_CONFLICT",
            "设备序列号已登记到其他设备，请检查设备台账",
            status_code=409,
        )


def _upsert_identity_deployment(
    db: Session, device: Device, outcome: IdentityOutcome, captured_at: datetime
) -> Deployment:
    """按身份指纹保存快照，不把旧部署能力误当成新部署的探测结果。"""
    identity = outcome.identity
    uid = compute_uid(identity)
    deployment = db.scalar(
        select(Deployment).where(Deployment.device_id == device.id, Deployment.uid == uid)
    )
    if deployment is None:
        deployment = Deployment(
            device_id=device.id,
            uid=uid,
            capabilities={"declared": {}, "observed": {}},
            limits={},
        )
        db.add(deployment)

    deployment.platform = _identity_text(identity.get("platform"))
    deployment.bigversion = _identity_text(identity.get("bigversion"))
    deployment.etsllm_version = _identity_text(identity.get("etsllm_version"))
    accel = identity.get("accel")
    deployment.accel = accel if isinstance(accel, dict) else {}
    deployment.model_key = _identity_text(identity.get("model_key"))
    deployment.model_version = _identity_text(identity.get("model_version"))
    deployment.quant = outcome.quant
    deployment.soc = outcome.soc
    deployment.probe_meta = {
        "probed_at": captured_at.isoformat(),
        "probe_version": "probe-identity-1",
        "version_collection_ok": outcome.version_collection_ok,
        "warnings": outcome.warnings,
    }
    deployment.raw = outcome.raw
    deployment.captured_at = captured_at
    return deployment


def _identity_text(value: Any) -> str | None:
    """把身份字段限制为非空字符串，未知值保留为 null。"""
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _check_probe_cancellation(db: Session, job: Job | None) -> None:
    """探测请求完成或重试前检查是否收到 Job 取消请求。"""
    if job is None:
        return
    db.refresh(job, attribute_names=["status", "cancel_requested_at"])
    if job.status == "cancelled" or job.cancel_requested_at is not None:
        raise ApiError("E_JOB_CANCELLED", "设备探测任务已取消", status_code=409)


def start_probe(db: Session, device: Device, *, mode: str, created_by: str | None) -> Job:
    """创建设备探测 Job 并在后台执行（M7 §6）。"""
    locked_device = db.scalar(
        select(Device)
        .where(Device.id == device.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_device is None:
        raise ApiError("E_NOT_FOUND", "设备不存在", status_code=404)
    mode = validate_probe_request(db, locked_device, mode)
    active_voiceprint_check = db.scalar(
        select(Job.id).where(
            Job.kind.in_(("voiceprint_check", "speaker_extract")),
            Job.ref_type == "device",
            Job.ref_id == str(device.id),
            Job.status.in_(("queued", "running")),
        )
    )
    if active_voiceprint_check is not None:
        raise ApiError("E_DEVICE_BUSY", "该设备正在执行声纹自检", status_code=409)
    job = Job(
        kind="probe",
        ref_type="device",
        ref_id=str(device.id),
        status="queued",
        progress={"phase": "queued", "mode": mode},
        created_by=created_by,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    thread = threading.Thread(
        target=_run_probe_job,
        args=(factory, job.id, device.id, mode),
        daemon=True,
        name=f"device-probe-{job.id}",
    )
    _probe_workers[job.id] = thread
    thread.start()
    return job


def _run_probe_job(
    factory: sessionmaker[Session], job_id: uuid.UUID, device_id: uuid.UUID, mode: str
) -> None:
    """后台执行设备探测，并回写统一 Job 终态。"""
    db = factory()
    try:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        device = db.get(Device, device_id)
        if job is None:
            return
        if job.status == "cancelled":
            return
        if device is None:
            raise ApiError("E_NOT_FOUND", "设备不存在", status_code=404)
        if job.cancel_requested_at is not None:
            _finish_probe_job_cancelled(job)
            db.commit()
            return

        job.status = "running"
        job.progress = {**(job.progress or {}), "phase": "probing"}
        job.started_at = datetime.now(UTC)
        db.commit()

        previous_uid = db.scalar(
            select(Deployment.uid)
            .where(Deployment.device_id == device.id)
            .order_by(Deployment.captured_at.desc())
        )
        probe, deployment = asyncio.run(run_probe(db, device, mode=mode, job=job))
        job = db.scalar(
            select(Job)
            .where(Job.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None:
            return
        probe_payload = {
            "id": str(probe.id),
            "mode": probe.mode,
            "status": probe.status,
            "duration_ms": probe.duration_ms,
            "result": probe.result,
            "error": probe.error,
            "created_at": probe.created_at.isoformat(),
        }
        job.result = {
            "resource_id": str(probe.id),
            "url": f"/api/v1/devices/{device_id}/health",
            "probe": probe_payload,
            "deployment_id": str(deployment.id) if deployment else None,
        }
        job.progress = {"phase": "done", "probe_status": probe.status}
        if job.cancel_requested_at is not None:
            _finish_probe_job_cancelled(job)
        elif probe.status == "ok":
            job.status = "succeeded"
        else:
            job.status = "failed"
            job.error_code = "E_PROBE_FAILED"
            job.error_detail = {"message": probe.error or "设备探测失败"}
        job.finished_at = datetime.now(UTC)
        db.commit()
        if job.status == "succeeded" and deployment is not None and deployment.uid != previous_uid:
            _schedule_uid_change_voiceprint_check(db, job, device)
    except Exception as exc:
        db.rollback()
        job = db.get(Job, job_id)
        if job is not None:
            if isinstance(exc, ApiError):
                error_code = exc.code
                message = exc.message
            else:
                error_code = "E_PROBE_FAILED"
                message = str(exc)
            if error_code == "E_JOB_CANCELLED" or job.cancel_requested_at is not None:
                _finish_probe_job_cancelled(job)
            else:
                job.status = "failed"
                job.error_code = error_code
                job.error_detail = {"message": message[:1024]}
                job.progress = {**(job.progress or {}), "phase": "failed"}
                job.finished_at = datetime.now(UTC)
            db.commit()
    finally:
        db.close()
        _probe_workers.pop(job_id, None)


def _finish_probe_job_cancelled(job: Job) -> None:
    """将探测任务统一落为 cancelled 终态。"""
    job.status = "cancelled"
    job.error_code = "E_JOB_CANCELLED"
    job.error_detail = {"message": "设备探测任务已取消"}
    job.progress = {**(job.progress or {}), "phase": "cancelled"}
    job.finished_at = datetime.now(UTC)


def wait_for_probe_tasks(timeout_s: float = 30.0) -> None:
    """测试辅助：等待当前设备探测 Job 线程结束。"""
    for thread in list(_probe_workers.values()):
        thread.join(timeout=timeout_s)


def _schedule_uid_change_voiceprint_check(db: Session, job: Job, device: Device) -> None:
    """部署首次出现或 UID 改变后，在设备空闲时自动排入声纹自检。"""
    if device.kind not in {"etsstt", "gateway"}:
        return
    from app.domain.voiceprint_checks import start_voiceprint_check

    try:
        check_job = start_voiceprint_check(db, device, created_by=job.created_by)
    except ApiError as exc:
        db.rollback()
        current = db.get(Job, job.id)
        if current is not None:
            current.result = {
                **(current.result or {}),
                "voiceprint_check": {"status": "skipped", "error_code": exc.code},
            }
            current.version += 1
            db.commit()
        if exc.code != "E_DEVICE_BUSY":
            logger.warning("部署变更后的声纹自检未能自动排队：%s", exc.message)
        return
    except Exception:
        db.rollback()
        logger.exception("部署变更后的声纹自检自动排队失败")
        return

    current = db.get(Job, job.id)
    if current is not None:
        current.result = {
            **(current.result or {}),
            "voiceprint_check": {"status": "queued", "job_id": str(check_job.id)},
        }
        current.version += 1
        db.commit()


def _probe_result(outcome: ProbeOutcome) -> dict[str, Any]:
    """probe_run.result 只放诊断信息，不放性能结论。"""
    return {
        "ok": outcome.ok,
        "model_key": outcome.model_key,
        "accel": outcome.accel,
        "declared": outcome.declared,
        "init_latency_ms": outcome.init_latency_ms,
        "error": outcome.error,
    }


def _identity_probe_result(outcome: IdentityOutcome) -> dict[str, Any]:
    """保存身份采集字段和部分失败信息，供设备与部署详情展示。"""
    return {
        "ok": outcome.ok,
        "identity": outcome.identity,
        "sn": outcome.sn,
        "soc": outcome.soc,
        "quant": outcome.quant,
        "version_collection_ok": outcome.version_collection_ok,
        "warnings": outcome.warnings,
        "error": outcome.error,
    }


def _upsert_deployment(
    db: Session, device: Device, outcome: ProbeOutcome, captured_at: datetime
) -> Deployment:
    """同 uid 复用快照，只更新观测时间与原始报文。"""
    identity = {
        "platform": device.platform,
        "kind": device.kind,
        "bigversion": None,
        "etsllm_version": None,
        "accel": outcome.accel,
        "model_key": outcome.model_key,
        "model_version": None,
    }
    uid = compute_uid(identity)

    deployment = db.scalar(
        select(Deployment).where(Deployment.device_id == device.id, Deployment.uid == uid)
    )
    if deployment is None:
        deployment = Deployment(device_id=device.id, uid=uid)
        db.add(deployment)
        observed: dict[str, Any] = {}
    else:
        existing_observed = (deployment.capabilities or {}).get("observed")
        observed = dict(existing_observed) if isinstance(existing_observed, dict) else {}

    deployment.platform = device.platform
    deployment.accel = outcome.accel
    deployment.model_key = outcome.model_key
    deployment.capabilities = {"declared": outcome.declared, "observed": observed}
    deployment.limits = _limits_from(outcome.declared, device.kind)
    deployment.probe_meta = {
        "probed_at": captured_at.isoformat(),
        "probe_version": "probe-lite-1",
        "observed_missing": [
            "partials",
            "segments",
            "speakers_in_sentence",
            "global_spk_list",
            "punctuation_output",
            "usage_field",
            "finalizes_in_stream",
            "naming",
        ],
    }
    deployment.raw = outcome.raw
    deployment.captured_at = captured_at
    return deployment


def _limits_from(declared: dict[str, Any], kind: str) -> dict[str, Any]:
    """声明的并发/时长上限；probe 拿不到时按 M2 §10 风险 5 的默认口径兜底。"""
    hotwords = declared.get("hotwords") or {}
    # 未配置 realtime_slots 时的默认：网关 3，整机 1（M2 §2.3 Limits）
    default_slots = 3 if kind == "gateway" else 1
    return {
        "realtime_slots": declared.get("realtime_slots") or default_slots,
        "max_audio_sec": None,
        "max_spk": None,
        "hotword_max": hotwords.get("max_count"),
    }
