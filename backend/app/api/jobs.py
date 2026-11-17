"""统一异步任务查询、取消与进度事件流（M7 §6/§7）。"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import _bearer, current_claims
from app.core.db import get_db
from app.core.errors import ApiError
from app.core.rate_limit import require_limit, sse_connection_limiter
from app.core.sse import format_sse, sse_event_buffer
from app.domain.auth import consume_stream_ticket
from app.domain.imports import job_dict
from app.domain.models import AppUser, Job

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


def _authenticated_job_claims(
    claims: dict[str, Any] = Depends(current_claims),
) -> dict[str, Any]:
    """要求登录后读取 job；对象级授权在资源加载后判断。"""
    return claims


_authenticated_job_claims.__dict__["requires_authentication"] = True


def _job_or_404(db: Session, job_id: uuid.UUID) -> Job:
    """读取统一 Job，不存在时返回标准 404。"""
    job = db.get(Job, job_id)
    if job is None:
        raise ApiError("E_NOT_FOUND", "异步任务不存在", status_code=404)
    return job


def _require_job_access(job: Job, *, username: str, is_admin: bool) -> None:
    """仅创建者或管理员可查看、取消任务。"""
    if not is_admin and job.created_by != username:
        raise ApiError("E_NOT_OWNER", "只能访问自己创建的任务", status_code=403)


def _event_payload(job: Job) -> dict[str, Any]:
    """生成不含请求私密信息的任务进度帧。"""
    return {
        "id": str(job.id),
        "kind": job.kind,
        "ref": {"type": job.ref_type, "id": job.ref_id},
        "status": job.status,
        "progress": job.progress or {},
        "result": job.result,
        "error_code": job.error_code,
        "error_detail": job.error_detail,
        "version": job.version,
    }


def _event_name(status: str) -> str:
    """按 job 状态映射 M7 §7.4 事件名。"""
    if status == "succeeded":
        return "job.succeeded"
    if status == "failed":
        return "job.failed"
    if status == "cancelled":
        return "job.cancelled"
    return "job.progress"


@router.get("/{job_id}")
def get_job(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(_authenticated_job_claims),
) -> dict[str, Any]:
    """读取统一任务状态；按创建者或管理员做对象级授权。"""
    job = _job_or_404(db, job_id)
    _require_job_access(
        job,
        username=str(claims.get("sub", "")),
        is_admin=claims.get("role") == "admin",
    )
    return job_dict(job)


@router.post("/{job_id}/cancel")
def cancel_job(
    job_id: uuid.UUID,
    response: Response,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(_authenticated_job_claims),
) -> dict[str, Any]:
    """请求取消支持取消的任务；长任务会在安全检查点停止。"""
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job is None:
        raise ApiError("E_NOT_FOUND", "异步任务不存在", status_code=404)
    _require_job_access(
        job,
        username=str(claims.get("sub", "")),
        is_admin=claims.get("role") == "admin",
    )
    if job.status == "cancelled":
        return job_dict(job)
    if job.status not in {"queued", "running"}:
        raise ApiError("E_CONFLICT", "仅 queued 或 running 任务可取消", status_code=409)
    if job.kind not in {"import", "probe", "voiceprint_check", "report"}:
        raise ApiError("E_CONFLICT", "此类任务不支持取消", status_code=409)

    now = datetime.now(UTC)
    if job.status == "queued":
        job.status = "cancelled"
        job.error_code = "E_JOB_CANCELLED"
        messages = {
            "import": "导入任务已取消",
            "probe": "设备探测任务已取消",
            "voiceprint_check": "声纹自检任务已取消",
            "report": "报告生成任务已取消",
        }
        job.error_detail = {"message": messages[job.kind]}
        job.finished_at = now
    else:
        job.cancel_requested_at = job.cancel_requested_at or now
        response.status_code = 202
    db.commit()
    db.refresh(job)
    return job_dict(job)


@router.get("/{job_id}/events")
async def job_events(
    job_id: uuid.UUID,
    ticket: str | None = Query(default=None),
    db: Session = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    last_event_id_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    last_event_id_query: Annotated[str | None, Query(alias="last_event_id")] = None,
) -> StreamingResponse:
    """输出 Job 进度 SSE，支持一次性 ticket、Last-Event-ID 与 resync。"""
    if ticket is not None:
        username = consume_stream_ticket(db, ticket)
        if username is None:
            raise ApiError("E_TICKET_INVALID", "stream ticket 无效或已使用", status_code=401)
        user = db.get(AppUser, username)
        if user is None or not user.active:
            raise ApiError("E_AUTH_INVALID", "账号不可用", status_code=401)
        is_admin = user.role == "admin"
    else:
        claims = current_claims(credentials=credentials, db=db)
        username = str(claims.get("sub", ""))
        is_admin = claims.get("role") == "admin"

    job = _job_or_404(db, job_id)
    _require_job_access(job, username=username, is_admin=is_admin)
    if not sse_connection_limiter.acquire(username, 5):
        require_limit(1, message="同时打开的事件流数量已达上限")
    last_event_id = last_event_id_header or last_event_id_query

    async def generate() -> AsyncIterator[str]:
        deadline = asyncio.get_running_loop().time() + 30 * 60
        stream_key = f"job:{job_id}"
        replay = sse_event_buffer.replay(stream_key, last_event_id)
        for event in replay.events:
            yield format_sse(event)
        last_payload = replay.latest.data if replay.latest else None
        if replay.resync:
            event = sse_event_buffer.publish(
                stream_key, "resync", {"reason": "history_unavailable"}
            )
            yield format_sse(event)

        last_heartbeat = time.monotonic()
        while asyncio.get_running_loop().time() <= deadline:
            db.expire_all()
            current = db.get(Job, job_id)
            if current is None:
                break
            payload = json.dumps(_event_payload(current), ensure_ascii=False)
            if last_event_id is None or replay.resync or payload != last_payload:
                event = sse_event_buffer.publish(
                    stream_key, _event_name(current.status), json.loads(payload)
                )
                yield format_sse(event)
                last_payload = payload
                last_heartbeat = time.monotonic()
            elif time.monotonic() - last_heartbeat >= 15:
                yield ": heartbeat\n\n"
                last_heartbeat = time.monotonic()

            if current.status in {"succeeded", "failed", "cancelled"}:
                yield "event: end\ndata: {}\n\n"
                break
            await asyncio.sleep(0.5)
        else:
            yield 'event: end\ndata: {"reason":"expired"}\n\n'

    async def managed_stream() -> AsyncIterator[str]:
        try:
            async for chunk in generate():
                yield chunk
        finally:
            sse_connection_limiter.release(username)

    return StreamingResponse(
        managed_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


job_events.__dict__["requires_authentication"] = True
