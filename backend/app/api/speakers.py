"""说话人注册录音与声纹提取接口（M7 §5.1、M2 §5.5）。"""

import hashlib
import io
import struct
import uuid
import wave
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, File, Header, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import require
from app.api.idempotency import apply_idempotent_response
from app.core.db import get_db
from app.core.errors import ApiError
from app.core.object_store import get_object_store
from app.domain.idempotency import execute_idempotently
from app.domain.imports import job_dict
from app.domain.models import Deployment, Device, Speaker, SpeakerEnrollment, Voiceprint
from app.domain.speaker_registry import start_speaker_extraction

router = APIRouter(prefix="/api/v1/speakers", tags=["speakers"])
MAX_ENROLLMENT_BYTES = 50 * 1024 * 1024


class SpeakerCreate(BaseModel):
    """新增说话人注册档案。"""

    name: str = Field(min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        """名称去除首尾空白并拒绝纯空白。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("说话人名称不能为空")
        return normalized


def _speaker_dict(
    speaker: Speaker,
    *,
    enrollment_count: int = 0,
    vector_count: int = 0,
    vector_statuses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": str(speaker.id),
        "name": speaker.name,
        "enrollment_count": enrollment_count,
        "vector_count": vector_count,
        "vector_statuses": vector_statuses or [],
        "created_by": speaker.created_by,
        "created_at": speaker.created_at.isoformat(),
    }


def _speaker_or_404(db: Session, speaker_id: uuid.UUID) -> Speaker:
    speaker = db.get(Speaker, speaker_id)
    if speaker is None:
        raise ApiError("E_NOT_FOUND", "说话人不存在", status_code=404)
    return speaker


def _deployment_vector_statuses(
    db: Session,
    speaker_ids: list[uuid.UUID],
    enrollment_counts: dict[uuid.UUID, int],
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """汇总每个说话人在各 deployment 上的向量覆盖状态，不返回向量本身。"""
    if not speaker_ids:
        return {}
    deployments = db.execute(
        select(Deployment, Device.name)
        .join(Device, Device.id == Deployment.device_id)
        .order_by(Device.name, Deployment.captured_at.desc(), Deployment.id)
    ).all()
    vector_counts = {
        (speaker_id, deployment_id): int(count)
        for speaker_id, deployment_id, count in db.execute(
            select(
                SpeakerEnrollment.speaker_id,
                Voiceprint.deployment_id,
                func.count(),
            )
            .join(Voiceprint, Voiceprint.enrollment_id == SpeakerEnrollment.id)
            .where(SpeakerEnrollment.speaker_id.in_(speaker_ids))
            .group_by(SpeakerEnrollment.speaker_id, Voiceprint.deployment_id)
        ).all()
    }
    result: dict[uuid.UUID, list[dict[str, Any]]] = {speaker_id: [] for speaker_id in speaker_ids}
    for speaker_id in speaker_ids:
        enrollment_count = enrollment_counts.get(speaker_id, 0)
        for deployment, device_name in deployments:
            vector_count = vector_counts.get((speaker_id, deployment.id), 0)
            status = (
                "no_enrollment"
                if enrollment_count == 0
                else "ready"
                if vector_count >= enrollment_count
                else "partial"
                if vector_count > 0
                else "missing"
            )
            result[speaker_id].append(
                {
                    "deployment_id": str(deployment.id),
                    "deployment_uid": deployment.uid,
                    "device_id": str(deployment.device_id),
                    "device_name": device_name,
                    "captured_at": deployment.captured_at.isoformat(),
                    "enrollment_count": enrollment_count,
                    "vector_count": vector_count,
                    "status": status,
                }
            )
    return result


@router.get("")
def list_speakers(
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("speaker:read")),
) -> dict[str, Any]:
    """列出说话人及注册录音、已提取向量数量。"""
    speakers = db.scalars(select(Speaker).order_by(Speaker.name, Speaker.id)).all()
    enrollment_counts = dict(
        db.execute(
            select(SpeakerEnrollment.speaker_id, func.count()).group_by(
                SpeakerEnrollment.speaker_id
            )
        ).all()
    )
    vector_rows = db.execute(
        select(SpeakerEnrollment.speaker_id, Voiceprint.deployment_id, func.count())
        .join(Voiceprint, Voiceprint.enrollment_id == SpeakerEnrollment.id)
        .group_by(SpeakerEnrollment.speaker_id, Voiceprint.deployment_id)
    ).all()
    vector_counts: dict[uuid.UUID, int] = {}
    for speaker_id, _deployment_id, count in vector_rows:
        vector_counts[speaker_id] = vector_counts.get(speaker_id, 0) + int(count)
    speaker_ids = [speaker.id for speaker in speakers]
    vector_statuses = _deployment_vector_statuses(db, speaker_ids, enrollment_counts)
    return {
        "items": [
            _speaker_dict(
                speaker,
                enrollment_count=int(enrollment_counts.get(speaker.id, 0)),
                vector_count=int(vector_counts.get(speaker.id, 0)),
                vector_statuses=vector_statuses.get(speaker.id, []),
            )
            for speaker in speakers
        ]
    }


@router.post("", status_code=201)
def create_speaker(
    body: SpeakerCreate,
    response: Response,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("speaker:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """创建说话人档案并支持幂等重放。"""

    def create() -> dict[str, Any]:
        speaker = Speaker(name=body.name, created_by=str(claims.get("sub", "")))
        db.add(speaker)
        db.commit()
        db.refresh(speaker)
        return _speaker_dict(speaker)

    result = execute_idempotently(
        db,
        username=str(claims.get("sub", "")),
        key=idempotency_key,
        request_body=body.model_dump(),
        operation=create,
        location=lambda value: f"/api/v1/speakers/{value['id']}",
        status_code=201,
    )
    apply_idempotent_response(response, result)
    return result.body


@router.get("/{speaker_id}")
def get_speaker(
    speaker_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("speaker:read")),
) -> dict[str, Any]:
    """读取说话人档案及其注册、向量数量。"""
    speaker = _speaker_or_404(db, speaker_id)
    enrollment_count = db.scalar(
        select(func.count())
        .select_from(SpeakerEnrollment)
        .where(SpeakerEnrollment.speaker_id == speaker_id)
    )
    vector_count = db.scalar(
        select(func.count())
        .select_from(Voiceprint)
        .join(SpeakerEnrollment, SpeakerEnrollment.id == Voiceprint.enrollment_id)
        .where(SpeakerEnrollment.speaker_id == speaker_id)
    )
    statuses = _deployment_vector_statuses(
        db, [speaker_id], {speaker_id: int(enrollment_count or 0)}
    )
    return _speaker_dict(
        speaker,
        enrollment_count=int(enrollment_count or 0),
        vector_count=int(vector_count or 0),
        vector_statuses=statuses.get(speaker_id, []),
    )


@router.get("/{speaker_id}/enrollments")
def list_enrollments(
    speaker_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("speaker:read")),
) -> dict[str, Any]:
    """列出注册录音元信息；音频通过单独鉴权端点按需读取。"""
    _speaker_or_404(db, speaker_id)
    rows = db.scalars(
        select(SpeakerEnrollment)
        .where(SpeakerEnrollment.speaker_id == speaker_id)
        .order_by(SpeakerEnrollment.created_at.desc(), SpeakerEnrollment.id)
    ).all()
    return {
        "items": [
            {
                "id": str(row.id),
                "speaker_id": str(row.speaker_id),
                "filename": row.filename,
                "sha256": row.sha256,
                "content_type": row.content_type,
                "duration_ms": row.duration_ms,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


@router.get(
    "/{speaker_id}/enrollments/{enrollment_id}/audio",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "受限访问的 WAV 注册录音",
            "content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
def get_enrollment_audio(
    speaker_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("speaker:read")),
) -> StreamingResponse:
    """按需流式读取受管注册音频，不向客户端暴露对象存储路径。"""
    enrollment = db.get(SpeakerEnrollment, enrollment_id)
    if enrollment is None or enrollment.speaker_id != speaker_id:
        raise ApiError("E_NOT_FOUND", "注册录音不存在", status_code=404)
    try:
        source = get_object_store().open(enrollment.object_key)
    except FileNotFoundError as exc:
        raise ApiError("E_NOT_FOUND", "注册录音文件不存在", status_code=404) from exc

    def chunks() -> Iterator[bytes]:
        with source:
            while chunk := source.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        chunks(),
        media_type="audio/wav",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/{speaker_id}/enrollments", status_code=201)
def create_enrollment(
    speaker_id: uuid.UUID,
    response: Response,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("speaker:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """接收不超过 50MB 的 WAV 注册录音并写入 ObjectStore。"""
    _speaker_or_404(db, speaker_id)
    content = file.file.read(MAX_ENROLLMENT_BYTES + 1)
    if not content:
        raise ApiError("E_VALIDATION", "注册录音不能为空", status_code=422)
    if len(content) > MAX_ENROLLMENT_BYTES:
        raise ApiError("E_UPLOAD_TOO_LARGE", "注册录音不能超过 50MB", status_code=413)
    filename = PurePosixPath((file.filename or "enrollment.wav").replace("\\", "/")).name
    if not filename.lower().endswith(".wav"):
        raise ApiError("E_AUDIO_FORMAT", "注册录音必须是 WAV 文件", status_code=422)
    try:
        with wave.open(io.BytesIO(content), "rb") as audio:
            sample_rate = audio.getframerate()
            frame_count = audio.getnframes()
            if sample_rate <= 0 or frame_count <= 0:
                raise wave.Error("空音频")
            duration_ms = round(frame_count * 1000 / sample_rate)
    except (wave.Error, EOFError) as exc:
        raise ApiError("E_AUDIO_FORMAT", "注册录音不是有效 WAV 音频", status_code=422) from exc

    digest = hashlib.sha256(content).hexdigest()
    safe_filename = filename[:255]
    object_key = f"speakers/{speaker_id}/enrollments/{uuid.uuid4()}.wav"
    store = get_object_store()

    def create() -> dict[str, Any]:
        store.put(object_key, content, file.content_type or "audio/wav")
        enrollment = SpeakerEnrollment(
            speaker_id=speaker_id,
            object_key=object_key,
            filename=safe_filename,
            content_type=file.content_type or "audio/wav",
            sha256=digest,
            duration_ms=duration_ms,
        )
        db.add(enrollment)
        try:
            db.commit()
            db.refresh(enrollment)
        except Exception:
            db.rollback()
            store.delete(object_key)
            raise
        return {
            "id": str(enrollment.id),
            "speaker_id": str(speaker_id),
            "filename": enrollment.filename,
            "sha256": digest,
            "duration_ms": duration_ms,
            "created_at": enrollment.created_at.isoformat(),
        }

    result = execute_idempotently(
        db,
        username=str(claims.get("sub", "")),
        key=idempotency_key,
        request_body={"speaker_id": str(speaker_id), "sha256": digest},
        operation=create,
        location=lambda value: f"/api/v1/speakers/{speaker_id}/vectors",
        status_code=201,
    )
    apply_idempotent_response(response, result)
    return result.body


@router.post("/{speaker_id}/extract", status_code=202)
def extract_speaker_vectors(
    speaker_id: uuid.UUID,
    deployment_id: uuid.UUID,
    response: Response,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("speaker:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """异步为注册录音提取所选部署的声纹向量。"""

    def create() -> dict[str, Any]:
        job = start_speaker_extraction(
            db,
            speaker_id=speaker_id,
            deployment_id=deployment_id,
            created_by=str(claims.get("sub", "")),
        )
        return job_dict(job)

    result = execute_idempotently(
        db,
        username=str(claims.get("sub", "")),
        key=idempotency_key,
        request_body={"speaker_id": str(speaker_id), "deployment_id": str(deployment_id)},
        operation=create,
        location=lambda value: f"/api/v1/jobs/{value['id']}",
        status_code=202,
    )
    apply_idempotent_response(response, result)
    return result.body


@router.get("/{speaker_id}/vectors")
def list_speaker_vectors(
    speaker_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("speaker:read")),
) -> dict[str, Any]:
    """读取该说话人在各设备部署上的声纹向量及来源注册录音。"""
    _speaker_or_404(db, speaker_id)
    rows = db.execute(
        select(SpeakerEnrollment, Voiceprint, Deployment, Device)
        .join(Voiceprint, Voiceprint.enrollment_id == SpeakerEnrollment.id)
        .join(Deployment, Deployment.id == Voiceprint.deployment_id)
        .join(Device, Device.id == Deployment.device_id)
        .where(SpeakerEnrollment.speaker_id == speaker_id)
        .order_by(Device.name, Deployment.captured_at, SpeakerEnrollment.created_at)
    ).all()
    vectors = []
    for enrollment, voiceprint, deployment, device in rows:
        if voiceprint.dimension != 192 or len(voiceprint.vector) != 192 * 4:
            raise ApiError("E_DATA_CORRUPT", "声纹向量维度或数据长度异常", status_code=500)
        vectors.append(
            {
                "enrollment_id": str(enrollment.id),
                "filename": enrollment.filename,
                "deployment_id": str(deployment.id),
                "deployment_uid": deployment.uid,
                "device_id": str(device.id),
                "device_name": device.name,
                "dimension": voiceprint.dimension,
                "vector": list(struct.unpack("<192f", voiceprint.vector)),
                "created_at": voiceprint.created_at.isoformat(),
            }
        )
    return {"items": vectors}
