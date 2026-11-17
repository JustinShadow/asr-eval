"""管理说话人注册录音，并按设备部署提取与缓存声纹。"""

import asyncio
import math
import struct
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.etsstt import extract_voiceprint
from app.adapters.ssh_tunnel import device_endpoint
from app.core.errors import ApiError
from app.core.object_store import ObjectStore
from app.domain.devices import _lease_active
from app.domain.models import (
    Deployment,
    Device,
    Job,
    OpsAction,
    Speaker,
    SpeakerEnrollment,
    Voiceprint,
)
from app.domain.remote_staging import StagedAudio, cleanup_audio, stage_audio

_speaker_workers: dict[uuid.UUID, threading.Thread] = {}


def start_speaker_extraction(
    db: Session,
    *,
    speaker_id: uuid.UUID,
    deployment_id: uuid.UUID,
    created_by: str | None,
) -> Job:
    """校验设备空闲并创建按 enrollment 续跑的声纹提取 Job。"""
    speaker = db.scalar(select(Speaker).where(Speaker.id == speaker_id).with_for_update())
    if speaker is None:
        raise ApiError("E_NOT_FOUND", "说话人不存在", status_code=404)
    deployment = db.get(Deployment, deployment_id)
    if deployment is None:
        raise ApiError("E_NOT_FOUND", "部署快照不存在", status_code=404)
    device = db.scalar(select(Device).where(Device.id == deployment.device_id).with_for_update())
    if device is None:
        raise ApiError("E_NOT_FOUND", "部署所属设备不存在", status_code=404)
    if device.kind not in {"etsstt", "gateway"} or not deployment.model_key:
        raise ApiError("E_UNSUPPORTED", "该部署不支持声纹提取", status_code=422)
    if _lease_active(db, device.id):
        raise ApiError("E_DEVICE_BUSY", "设备正在被 Run 独占，禁止提取声纹", status_code=409)
    active_ops = db.scalar(
        select(OpsAction.id).where(
            OpsAction.device_id == device.id,
            OpsAction.status.in_(("queued", "running")),
        )
    )
    if active_ops is not None:
        raise ApiError("E_DEVICE_BUSY", "设备正在执行运维操作，暂不能提取声纹", status_code=409)

    enrollments = db.scalars(
        select(SpeakerEnrollment)
        .where(SpeakerEnrollment.speaker_id == speaker_id)
        .order_by(SpeakerEnrollment.created_at, SpeakerEnrollment.id)
    ).all()
    if not enrollments:
        raise ApiError("E_ENROLLMENT_REQUIRED", "说话人尚无注册录音", status_code=409)

    active_job = db.scalar(
        select(Job.id).where(
            Job.kind.in_(("probe", "voiceprint_check", "speaker_extract")),
            Job.ref_type == "device",
            Job.ref_id == str(device.id),
            Job.status.in_(("queued", "running")),
        )
    )
    if active_job is not None:
        raise ApiError("E_DEVICE_BUSY", "设备已有探测或声纹任务正在运行", status_code=409)

    job = Job(
        kind="speaker_extract",
        ref_type="device",
        ref_id=str(device.id),
        status="queued",
        progress={"phase": "queued", "completed": 0, "total": len(enrollments)},
        created_by=created_by,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    thread = threading.Thread(
        target=_run_speaker_extraction,
        args=(factory, job.id, speaker_id, deployment_id),
        daemon=True,
        name=f"speaker-extract-{job.id}",
    )
    _speaker_workers[job.id] = thread
    thread.start()
    return job


def _run_speaker_extraction(
    factory: sessionmaker[Session],
    job_id: uuid.UUID,
    speaker_id: uuid.UUID,
    deployment_id: uuid.UUID,
) -> None:
    """逐条处理注册录音；成功向量独立提交，失败项可重试续跑。"""
    db = factory()
    try:
        job = db.get(Job, job_id)
        speaker = db.get(Speaker, speaker_id)
        deployment = db.get(Deployment, deployment_id)
        device = db.get(Device, deployment.device_id) if deployment else None
        if job is None or speaker is None or deployment is None or device is None:
            return
        if job.cancel_requested_at is not None:
            _finish_cancelled(job)
            db.commit()
            return

        job.status = "running"
        job.started_at = datetime.now(UTC)
        db.commit()
        enrollments = db.scalars(
            select(SpeakerEnrollment)
            .where(SpeakerEnrollment.speaker_id == speaker_id)
            .order_by(SpeakerEnrollment.created_at, SpeakerEnrollment.id)
        ).all()
        store = _object_store()
        failures: list[dict[str, str]] = []
        completed = 0
        for enrollment in enrollments:
            db.refresh(job)
            if job.cancel_requested_at is not None:
                _finish_cancelled(job)
                db.commit()
                return
            existing = db.get(Voiceprint, (enrollment.id, deployment_id))
            if existing is None:
                try:
                    _extract_one(db, store, device, deployment, enrollment)
                except Exception as exc:
                    db.rollback()
                    failures.append(
                        {"enrollment_id": str(enrollment.id), "message": str(exc)[:500]}
                    )
            completed += 1
            job.progress = {
                "phase": "extracting",
                "completed": completed,
                "total": len(enrollments),
                "failures": len(failures),
            }
            db.commit()

        job.result = {
            "speaker_id": str(speaker_id),
            "deployment_id": str(deployment_id),
            "vector_count": completed - len(failures),
            "failures": failures,
        }
        job.status = "failed" if failures else "succeeded"
        job.error_code = "E_VOICEPRINT_EXTRACT" if failures else None
        job.error_detail = {"failures": failures} if failures else None
        job.progress = {"phase": "done", "completed": completed, "total": len(enrollments)}
        job.finished_at = datetime.now(UTC)
        job.version += 1
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.get(Job, job_id)
        if job is not None:
            job.status = "failed"
            job.error_code = "E_VOICEPRINT_EXTRACT"
            job.error_detail = {"message": str(exc)[:1024]}
            job.progress = {"phase": "failed"}
            job.finished_at = datetime.now(UTC)
            job.version += 1
            db.commit()
    finally:
        db.close()
        _speaker_workers.pop(job_id, None)


def _extract_one(
    db: Session,
    store: ObjectStore,
    device: Device,
    deployment: Deployment,
    enrollment: SpeakerEnrollment,
) -> None:
    """暂存单条录音、调用设备提取并缓存 192 维 float32 向量。"""
    with store.open(enrollment.object_key) as source:
        content = source.read()
    temporary_path: Path | None = None
    staged: StagedAudio | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        staged = stage_audio(device, temporary_path)
        asr_host, asr_port = device_endpoint(device)
        embedding: list[float] | None = None
        failure = "设备声纹提取失败"
        # M2 §6 规定单条注册向量提取失败后重试一次。
        for _ in range(2):
            try:
                outcome = asyncio.run(
                    extract_voiceprint(
                        asr_host,
                        asr_port,
                        str(deployment.model_key),
                        staged.remote_path,
                        str(enrollment.id),
                        enrollment.duration_ms,
                    )
                )
                if not outcome.ok:
                    failure = outcome.error or failure
                elif len(outcome.embedding) != 192 or not all(
                    math.isfinite(value) for value in outcome.embedding
                ):
                    failure = "设备返回的声纹向量不是有效的 192 维向量"
                else:
                    embedding = outcome.embedding
                    break
            except Exception as exc:
                failure = str(exc) or failure
        if embedding is None:
            raise RuntimeError(failure)
        db.add(
            Voiceprint(
                enrollment_id=enrollment.id,
                deployment_id=deployment.id,
                dimension=192,
                vector=struct.pack("<192f", *embedding),
            )
        )
        db.commit()
    finally:
        if staged is not None:
            cleanup_audio(device, staged)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _object_store() -> ObjectStore:
    """延迟创建对象存储，便于后台任务按运行环境注入。"""
    from app.core.object_store import get_object_store

    return get_object_store()


def _finish_cancelled(job: Job) -> None:
    """把取消请求落为统一 Job 终态。"""
    job.status = "cancelled"
    job.error_code = "E_JOB_CANCELLED"
    job.error_detail = {"message": "声纹提取任务已取消"}
    job.progress = {"phase": "cancelled"}
    job.finished_at = datetime.now(UTC)
    job.version += 1


def wait_for_speaker_tasks(timeout_s: float = 30.0) -> None:
    """测试辅助：等待当前说话人声纹任务结束。"""
    for thread in list(_speaker_workers.values()):
        thread.join(timeout=timeout_s)
