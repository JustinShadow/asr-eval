"""Run 创建、持久调度与结果摘要。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import _bearer, current_claims
from app.api.deps import require
from app.api.idempotency import apply_idempotent_response
from app.core.db import get_db
from app.core.errors import ApiError
from app.core.rate_limit import require_limit, sse_connection_limiter
from app.core.sse import format_sse, sse_event_buffer
from app.domain.auth import consume_stream_ticket
from app.domain.idempotency import execute_idempotently
from app.domain.models import (
    Benchmark,
    CaseResult,
    CaseScore,
    Device,
    Evaluation,
    Item,
    RefText,
    Run,
    RunResourceSample,
    RunTarget,
)
from app.domain.readout import READOUT_BUCKET_DIMS, run_readout, target_cell
from app.domain.run_groups import (
    create_run_group,
    get_run_group,
    list_run_groups,
    run_group_summary,
)
from app.domain.run_worker import ensure_run_job, get_run_worker
from app.domain.runs import (
    cancel_run,
    create_run,
    ensure_run_owner,
    preview_run,
    resume_run,
    run_matrix,
    run_summary,
)
from app.domain.runs import (
    delete_run as delete_run_domain,
)

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


class TargetIn(BaseModel):
    """Run 的一个目标。"""

    device_id: uuid.UUID
    subset: str = Field(default="all", max_length=32)
    # 可选锁定部署指纹（D-M4-11）：回归场景冻结部署，不一致 409
    deployment_uid: str | None = Field(default=None, max_length=16)


class RunCreate(BaseModel):
    """创建 Run；targets 与 device_id 二选一。"""

    name: str = Field(min_length=1, max_length=128)
    dataset_version_id: uuid.UUID
    benchmark_id: uuid.UUID | None = None
    device_id: uuid.UUID | None = None
    targets: list[TargetIn] | None = None
    kind: Literal["accuracy", "perf"] = "accuracy"
    lanes: int = Field(default=1, ge=1, le=8)
    auto_score: bool = True
    deterministic: bool = False

    @model_validator(mode="after")
    def validate_lanes(self) -> RunCreate:
        """并发路数只对性能 Run 开放，避免误用设备并发槽位。"""
        if self.kind != "perf" and self.lanes != 1:
            raise ValueError("仅 perf Run 可设置 lanes 大于 1")
        return self


class RunPreview(BaseModel):
    """创建前预览所需参数。"""

    benchmark_id: uuid.UUID
    targets: list[TargetIn] = Field(min_length=1)
    kind: Literal["accuracy", "perf"] = "accuracy"
    lanes: int = Field(default=1, ge=1, le=8)

    @model_validator(mode="after")
    def validate_lanes(self) -> RunPreview:
        """并发路数只对性能 Run 开放。"""
        if self.kind != "perf" and self.lanes != 1:
            raise ValueError("仅 perf Run 可设置 lanes 大于 1")
        return self


class RunGroupCreate(BaseModel):
    """跨多个 Benchmark 一次创建评测组。"""

    name: str = Field(min_length=1, max_length=128)
    benchmark_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    targets: list[TargetIn] = Field(min_length=1)
    kind: Literal["accuracy", "perf"] = "accuracy"
    lanes: int = Field(default=1, ge=1, le=8)
    auto_score: bool = True

    @model_validator(mode="after")
    def validate_lanes(self) -> RunGroupCreate:
        """并发路数只对性能 Run 开放。"""
        if self.kind != "perf" and self.lanes != 1:
            raise ValueError("仅 perf Run 可设置 lanes 大于 1")
        return self


def _enqueue_group_runs(db: Session, payload: dict[str, Any]) -> None:
    """与单条 Run 创建保持一致：只把 queued 状态的成员交给持久队列。"""
    for entry in payload.get("runs", []):
        if not isinstance(entry, dict) or entry.get("status") != "queued":
            continue
        run_id = uuid.UUID(str(entry["id"]))
        ensure_run_job(db, run_id)
        get_run_worker().enqueue(
            run_id,
            sessionmaker(bind=db.get_bind(), expire_on_commit=False),
        )


@router.post("/groups", status_code=201)
def create_run_group_endpoint(
    body: RunGroupCreate,
    response: Response,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("run:create")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """为多个 Benchmark 各创建一条 Run，并返回组级汇总。"""

    def create() -> dict[str, Any]:
        group = create_run_group(
            db,
            name=body.name,
            benchmark_ids=body.benchmark_ids,
            targets=[(item.device_id, item.subset) for item in body.targets],
            created_by=str(claims.get("sub")),
            kind=body.kind,
            lanes=body.lanes,
            auto_score=body.auto_score,
        )
        return run_group_summary(db, group)

    result = execute_idempotently(
        db,
        username=str(claims.get("sub", "")),
        key=idempotency_key,
        request_body=body.model_dump(mode="json"),
        operation=create,
        location=lambda value: f"/api/v1/runs/groups/{value['group']['id']}",
    )
    apply_idempotent_response(response, result)
    _enqueue_group_runs(db, result.body)
    return result.body


@router.get("/groups")
def list_run_groups_endpoint(
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("run:read")),
) -> dict[str, Any]:
    """返回最近创建的评测组。"""
    return {"items": list_run_groups(db, limit=limit)}


@router.get("/groups/{group_id}")
def get_run_group_endpoint(
    group_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("run:read")),
) -> dict[str, Any]:
    """返回评测组、成员 Run 与加权 CER 汇总。"""
    return run_group_summary(db, get_run_group(db, group_id))


@router.get("")
def list_runs(
    status: str | None = Query(default=None),
    benchmark: uuid.UUID | None = Query(default=None),
    device: uuid.UUID | None = Query(default=None),
    q: str | None = Query(default=None, max_length=64),
    cursor: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("run:read")),
) -> dict[str, Any]:
    """按筛选条件返回 Run 游标页及列表所需的轻量摘要（M8 §3.3/§9.3）。"""
    known = {
        "queued",
        "running",
        "completed",
        "partial",
        "failed",
        "cancelled",
        "aborted_condition_changed",
    }
    if status is not None and status not in known:
        raise ApiError("E_VALIDATION", f"未知状态 {status}", status_code=422)
    statement = select(Run, Benchmark.name).join(Benchmark, Benchmark.id == Run.benchmark_id)
    if status is not None:
        statement = statement.where(Run.status == status)
    if benchmark is not None:
        statement = statement.where(Run.benchmark_id == benchmark)
    if device is not None:
        statement = statement.where(
            select(RunTarget.id)
            .where(RunTarget.run_id == Run.id, RunTarget.device_id == device)
            .exists()
        )
    if q:
        statement = statement.where(Run.name.ilike(f"%{q}%"))
    if cursor:
        cursor_created_at, cursor_id = _decode_run_cursor(cursor)
        statement = statement.where(
            or_(
                Run.created_at < cursor_created_at,
                (Run.created_at == cursor_created_at) & (Run.id < cursor_id),
            )
        )
    rows = db.execute(
        statement.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    run_ids = [run.id for run, _ in rows]

    targets_by_run: dict[uuid.UUID, list[dict[str, str]]] = {run_id: [] for run_id in run_ids}
    if run_ids:
        target_rows = db.execute(
            select(RunTarget.run_id, Device.id, Device.name, RunTarget.subset)
            .join(Device, Device.id == RunTarget.device_id)
            .where(RunTarget.run_id.in_(run_ids))
            .order_by(Device.name, RunTarget.subset)
        ).all()
        for run_id, device_id, device_name, subset in target_rows:
            targets_by_run[run_id].append(
                {"device_id": str(device_id), "device_name": device_name, "subset": subset}
            )

    evaluations_by_run: dict[uuid.UUID, Evaluation] = {}
    if run_ids:
        evaluation_rows = db.scalars(
            select(Evaluation)
            .where(Evaluation.run_id.in_(run_ids), Evaluation.status == "completed")
            .order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
        ).all()
        for evaluation in evaluation_rows:
            evaluations_by_run.setdefault(evaluation.run_id, evaluation)

    benchmark_facets = db.execute(
        select(Benchmark.id, Benchmark.name)
        .join(Run, Run.benchmark_id == Benchmark.id)
        .distinct()
        .order_by(Benchmark.name)
    ).all()
    device_facets = db.execute(
        select(Device.id, Device.name)
        .join(RunTarget, RunTarget.device_id == Device.id)
        .join(Run, Run.id == RunTarget.run_id)
        .distinct()
        .order_by(Device.name)
    ).all()
    now = datetime.now().astimezone()
    return {
        "items": [
            {
                "id": str(run.id),
                "name": run.name,
                "status": run.status,
                "group_id": str(run.group_id) if run.group_id else None,
                "progress": run.progress,
                "created_at": run.created_at.isoformat(),
                "benchmark": {"id": str(run.benchmark_id), "name": benchmark_name},
                "targets": targets_by_run[run.id],
                "evaluation": (
                    {
                        "id": str(evaluations_by_run[run.id].id),
                        "status": evaluations_by_run[run.id].status,
                    }
                    if run.id in evaluations_by_run
                    else None
                ),
                "cer": (
                    (evaluations_by_run[run.id].metrics or {}).get("cer")
                    if run.id in evaluations_by_run
                    else None
                ),
                "duration_ms": (
                    max(
                        0,
                        int(((run.finished_at or now) - run.started_at).total_seconds() * 1000),
                    )
                    if run.started_at
                    else None
                ),
                "eta_ms": run.eta_ms if run.status in {"queued", "running"} else None,
                "eta_source": run.eta_source,
                "eta_confidence": run.eta_confidence,
            }
            for run, benchmark_name in rows
        ],
        "next_cursor": _encode_run_cursor(rows[-1][0]) if has_more and rows else None,
        "has_more": has_more,
        "facets": {
            "benchmarks": [
                {"id": str(item_id), "name": name} for item_id, name in benchmark_facets
            ],
            "devices": [{"id": str(item_id), "name": name} for item_id, name in device_facets],
        },
    }


def _encode_run_cursor(run: Run) -> str:
    """将稳定排序键编码为 URL 安全游标（M7 §8.2）。"""
    payload = json.dumps([run.created_at.isoformat(), str(run.id)], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_run_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """解析 Run 游标；拒绝格式错误或被篡改的值。"""
    try:
        payload = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        created_at, run_id = json.loads(payload.decode())
        return datetime.fromisoformat(created_at), uuid.UUID(run_id)
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        raise ApiError("E_VALIDATION", "Run 游标无效", status_code=422) from None


@router.post("/preview")
def preview_run_endpoint(
    body: RunPreview,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("run:create")),
) -> dict[str, Any]:
    """返回目标指标、设备排队情况与 ETA，供创建前确认（M4 §5.1）。"""
    targets = [(item.device_id, item.subset) for item in body.targets]
    deployment_uids = {
        f"{item.device_id}:{item.subset}": item.deployment_uid
        for item in body.targets
        if item.deployment_uid is not None
    }
    return preview_run(
        db,
        benchmark_id=body.benchmark_id,
        targets=targets,
        kind=body.kind,
        lanes=body.lanes,
        deployment_uids=deployment_uids or None,
    )


@router.post("", status_code=201)
def create_run_endpoint(
    body: RunCreate,
    response: Response,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("run:create")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """创建 Run 并交给 PostgreSQL 持久队列调度。"""

    def create() -> dict[str, Any]:
        target_specs = (
            [(item.device_id, item.subset) for item in body.targets] if body.targets else None
        )
        if not target_specs and body.device_id is None:
            raise ApiError("E_SEMANTIC", "至少提供一个目标设备", status_code=422)
        deployment_uids = (
            {
                f"{item.device_id}:{item.subset}": item.deployment_uid
                for item in body.targets or []
                if item.deployment_uid is not None
            }
            if body.targets
            else None
        ) or None
        run = create_run(
            db,
            name=body.name,
            dataset_version_id=body.dataset_version_id,
            benchmark_id=body.benchmark_id,
            device_id=body.device_id,
            targets=target_specs,
            deployment_uids=deployment_uids,
            created_by=str(claims.get("sub")),
            kind=body.kind,
            lanes=body.lanes,
            auto_score=body.auto_score,
            deterministic=body.deterministic,
        )
        return run_summary(db, run)

    result = execute_idempotently(
        db,
        username=str(claims.get("sub", "")),
        key=idempotency_key,
        request_body=body.model_dump(mode="json"),
        operation=create,
        location=lambda value: f"/api/v1/runs/{value['run']['id']}",
    )
    apply_idempotent_response(response, result)
    run_payload = result.body.get("run")
    if isinstance(run_payload, dict) and run_payload.get("status") == "queued":
        run_id = uuid.UUID(str(run_payload["id"]))
        ensure_run_job(db, run_id)
        get_run_worker().enqueue(
            run_id,
            sessionmaker(bind=db.get_bind(), expire_on_commit=False),
        )
    return result.body


@router.get("/{run_id}/resources")
def get_run_resources(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("run:read")),
) -> dict[str, Any]:
    """返回 Run 执行期间的设备硬件资源采样与汇总。"""
    run = db.get(Run, run_id)
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    targets = db.scalars(select(RunTarget).where(RunTarget.run_id == run_id)).all()
    samples = db.scalars(
        select(RunResourceSample)
        .where(RunResourceSample.run_id == run_id)
        .order_by(RunResourceSample.sampled_at)
    ).all()
    by_target: dict[uuid.UUID, list[RunResourceSample]] = {}
    for sample in samples:
        by_target.setdefault(sample.target_id, []).append(sample)

    def summarize(rows: list[RunResourceSample]) -> dict[str, Any]:
        metrics = [row.metrics or {} for row in rows]

        def values(key: str) -> list[float]:
            return [
                float(value)
                for metric in metrics
                if isinstance((value := metric.get(key)), int | float)
                and not isinstance(value, bool)
            ]

        cpu = values("cpu_pct")
        memory = values("mem_used_pct")
        npu = values("npu_pct")
        temp = values("temp_c_max")
        return {
            "sample_count": len(rows),
            "cpu_avg": sum(cpu) / len(cpu) if cpu else None,
            "cpu_max": max(cpu) if cpu else None,
            "mem_avg": sum(memory) / len(memory) if memory else None,
            "mem_max": max(memory) if memory else None,
            "npu_avg": sum(npu) / len(npu) if npu else None,
            "npu_max": max(npu) if npu else None,
            "temp_max": max(temp) if temp else None,
            "first_at": rows[0].sampled_at.isoformat() if rows else None,
            "last_at": rows[-1].sampled_at.isoformat() if rows else None,
        }

    target_payload = []
    for target in targets:
        device = db.get(Device, target.device_id)
        rows = by_target.get(target.id, [])
        target_payload.append(
            {
                "target_id": str(target.id),
                "device_name": device.name if device else None,
                "subset": target.subset,
                "samples": [
                    {
                        "sampled_at": row.sampled_at.isoformat(),
                        "metrics": row.metrics or {},
                    }
                    for row in rows
                ],
                "summary": summarize(rows),
            }
        )
    return {
        "run_id": str(run.id),
        "available": bool(samples),
        "targets": target_payload,
    }


@router.post("/{run_id}/cancel")
def cancel_run_endpoint(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("run:cancel")),
) -> dict[str, Any]:
    """取消 Run（M4 §5.9）。

    D-M7-14：run:cancel 限本人——engineer/admin 不限，其他角色只能取消自己创建的 Run。
    """
    run = db.get(Run, run_id)
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    ensure_run_owner(
        run,
        actor=str(claims.get("sub", "")),
        role=str(claims.get("role", "")),
    )
    return run_summary(db, cancel_run(db, run_id))


@router.post("/{run_id}/resume")
def resume_run_endpoint(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("run:cancel")),
) -> dict[str, Any]:
    """续跑 failed/cancelled 的 Run：重新入队，执行方只补缺失样本（M4 §5.9）。"""
    run = db.get(Run, run_id)
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    ensure_run_owner(
        run,
        actor=str(claims.get("sub", "")),
        role=str(claims.get("role", "")),
    )
    run = resume_run(db, run_id)
    ensure_run_job(db, run.id)
    get_run_worker().enqueue(run.id, sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    return run_summary(db, run)


@router.delete("/{run_id}", status_code=204)
def delete_run_endpoint(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("run:delete")),
) -> None:
    """删除终态 Run；仅管理员可执行，关联的报告快照需先处理。"""
    delete_run_domain(db, run_id, actor=str(claims.get("sub", "")))


@router.get("/{run_id}/events")
async def run_events(
    run_id: uuid.UUID,
    ticket: str | None = Query(default=None),
    db: Session = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    last_event_id_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    last_event_id_query: Annotated[str | None, Query(alias="last_event_id")] = None,
) -> StreamingResponse:
    """SSE 进度流；支持一次性 stream ticket（M7 §2.4 / D-M7-5）。

    EventSource 不能设 Authorization 头：带 `?ticket=` 时校验一次性票；
    否则回退 Bearer + run:read。连接建立后流最长 30min，过期由前端重取 ticket 重连。
    """
    if ticket is not None:
        username = consume_stream_ticket(db, ticket)
        if username is None:
            raise ApiError("E_TICKET_INVALID", "stream ticket 无效或已使用", status_code=401)
    else:
        claims = current_claims(credentials=credentials, db=db)
        if "run:read" not in claims.get("perms", []):
            raise ApiError("E_FORBIDDEN", "缺少 run:read 权限", status_code=403)
        username = str(claims.get("sub", ""))

    run = db.get(Run, run_id)
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    if not sse_connection_limiter.acquire(username, 5):
        require_limit(1, message="同时打开的事件流数量已达上限")
    last_event_id = last_event_id_header or last_event_id_query

    async def generate() -> AsyncIterator[str]:
        # 流最长 30min（M7 §2.4）；超过后前端重取 ticket 重连
        deadline = asyncio.get_event_loop().time() + 30 * 60
        stream_key = f"run:{run_id}"
        replay = sse_event_buffer.replay(stream_key, last_event_id)
        for event in replay.events:
            yield format_sse(event)
        event_cursor = replay.latest.event_id if replay.latest else 0
        last_payload = replay.latest.data if replay.latest else None
        last_status: str | None = None
        last_heartbeat = time.monotonic()
        if replay.resync:
            event = sse_event_buffer.publish(
                stream_key, "resync", {"reason": "history_unavailable"}
            )
            event_cursor = event.event_id
            yield format_sse(event)
        db.expire_all()
        initial = db.get(Run, run_id)
        if initial is None:
            return
        initial_payload = json.dumps(
            {
                "run_id": str(initial.id),
                "status": initial.status,
                "progress": initial.progress,
                "eta_ms": initial.eta_ms,
                "eta_p90_ms": initial.eta_p90_ms,
                "eta_source": initial.eta_source,
                "eta_confidence": initial.eta_confidence,
            },
            ensure_ascii=False,
        )
        if last_event_id is None or replay.resync or last_payload != initial_payload:
            event_name = _run_event_name(initial.status)
            event = sse_event_buffer.publish(stream_key, event_name, json.loads(initial_payload))
            event_cursor = event.event_id
            yield format_sse(event)
            last_payload = initial_payload
        last_status = initial.status
        while True:
            if asyncio.get_event_loop().time() > deadline:
                yield 'event: end\ndata: {"reason": "expired"}\n\n'
                break
            pending_events = sse_event_buffer.replay(stream_key, str(event_cursor))
            for event in pending_events.events:
                event_cursor = event.event_id
                yield format_sse(event)
            db.expire_all()
            current = db.get(Run, run_id)
            if current is None:
                break
            payload = json.dumps(
                {
                    "run_id": str(current.id),
                    "status": current.status,
                    "progress": current.progress,
                    # ETA 滚动修正约每 5s 变化一次，SSE 1s 轮询足够（M4 §6.1）
                    "eta_ms": current.eta_ms,
                    "eta_p90_ms": current.eta_p90_ms,
                    "eta_source": current.eta_source,
                    "eta_confidence": current.eta_confidence,
                },
                ensure_ascii=False,
            )
            if payload != last_payload:
                event_name = (
                    _run_event_name(current.status)
                    if current.status != last_status
                    else "run.progress"
                )
                event = sse_event_buffer.publish(stream_key, event_name, json.loads(payload))
                event_cursor = event.event_id
                yield format_sse(event)
                last_payload = payload
                last_status = current.status
                last_heartbeat = time.monotonic()
            elif time.monotonic() - last_heartbeat >= 15:
                yield ": heartbeat\n\n"
                last_heartbeat = time.monotonic()
            if current.status in {
                "completed",
                "partial",
                "failed",
                "cancelled",
                "aborted_condition_changed",
            }:
                yield "event: end\ndata: {}\n\n"
                break
            await asyncio.sleep(1)

    async def gen() -> AsyncIterator[str]:
        try:
            async for chunk in generate():
                yield chunk
        finally:
            sse_connection_limiter.release(username)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _run_event_name(status: str) -> str:
    """按 Run 状态映射 M7 定义的事件名。"""
    return {
        "created": "run.created",
        "queued": "run.created",
        "running": "run.running",
        "completed": "run.completed",
        "partial": "run.partial",
        "failed": "run.failed",
        "aborted": "run.aborted",
        "aborted_condition_changed": "run.aborted_condition_changed",
        "cancelled": "run.cancelled",
    }.get(status, "run.progress")


# 该路由支持 ticket 或 Bearer 两种认证，静态门禁据此识别其认证分支。
run_events.__dict__["requires_authentication"] = True


@router.get("/{run_id}/readout")
def get_run_readout(
    run_id: uuid.UUID,
    dim: str = Query(default="duration_bucket", max_length=32),
    evaluation_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("run:read")),
) -> dict[str, Any]:
    """横评读数：CER+CI、Δ/p、排名及所选分桶维度（M8 §9.4）。"""
    run = db.get(Run, run_id)
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    if dim not in READOUT_BUCKET_DIMS:
        raise ApiError("E_VALIDATION", "不支持的分桶维度", status_code=422)
    _require_run_evaluation(db, run.id, evaluation_id)
    return run_readout(db, run, dim=dim, evaluation_id=evaluation_id)


@router.get("/{run_id}/timeline")
def get_run_timeline(
    run_id: uuid.UUID,
    failed_case_limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("run:read")),
) -> dict[str, Any]:
    """从持久化时间戳与失败结果整理 Run 执行时间线（M8 §9.4）。"""
    run = db.get(Run, run_id)
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)

    events: list[dict[str, Any]] = []

    def add_event(
        event_id: str,
        event_type: str,
        occurred_at: datetime | None,
        title: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if occurred_at is None:
            return
        events.append(
            {
                "id": event_id,
                "type": event_type,
                "occurred_at": occurred_at.isoformat(),
                "title": title,
                "detail": detail or {},
            }
        )

    add_event("run:created", "run.created", run.created_at, "Run 已创建")
    add_event("run:started", "run.started", run.started_at, "Run 已开始")

    target_rows = db.execute(
        select(RunTarget, Device.name)
        .join(Device, Device.id == RunTarget.device_id)
        .where(RunTarget.run_id == run.id)
        .order_by(RunTarget.id)
    ).all()
    for target, device_name in target_rows:
        target_detail = {
            "target_id": str(target.id),
            "device_name": device_name,
            "subset": target.subset,
            "status": target.status,
            "done": (target.stats or {}).get("done"),
            "total": (target.stats or {}).get("total"),
            "abort_reason": target.abort_reason,
            "condition_change": (target.stats or {}).get("condition_change"),
        }
        add_event(
            f"target:{target.id}:started",
            "target.started",
            target.started_at,
            f"{device_name} · {target.subset} 已开始",
            target_detail,
        )
        if target.finished_at is not None:
            target_status = {
                "completed": "已完成",
                "partial": "部分完成",
                "failed": "失败",
                "cancelled": "已取消",
                "aborted_condition_changed": "设备条件变化中断",
            }.get(target.status, target.status)
            add_event(
                f"target:{target.id}:finished",
                "target.finished",
                target.finished_at,
                f"{device_name} · {target.subset} {target_status}",
                target_detail,
            )

    failed_cases_statement = (
        select(func.count())
        .select_from(CaseResult)
        .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
        .where(RunTarget.run_id == run.id, CaseResult.status == "failed")
    )
    failed_case_count = int(db.scalar(failed_cases_statement) or 0)
    timed_failed_case_count = int(
        db.scalar(failed_cases_statement.where(CaseResult.created_at.is_not(None))) or 0
    )
    failed_cases = db.execute(
        select(
            CaseResult,
            Item.item_key,
            Device.name,
            RunTarget.subset,
        )
        .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
        .join(Device, Device.id == RunTarget.device_id)
        .join(Item, Item.id == CaseResult.item_id)
        .where(RunTarget.run_id == run.id, CaseResult.status == "failed")
        .where(CaseResult.created_at.is_not(None))
        .order_by(CaseResult.created_at.desc(), CaseResult.id.desc())
        .limit(failed_case_limit)
    ).all()
    for case, item_key, device_name, subset in failed_cases:
        add_event(
            f"case:{case.id}:failed",
            "case.failed",
            case.created_at,
            f"样本执行失败 · {item_key}",
            {
                "case_id": case.id,
                "item_key": item_key,
                "device_name": device_name,
                "subset": subset,
                "error": (case.error or "未提供失败原因")[:512],
            },
        )

    final_status = {
        "completed": "已完成",
        "partial": "部分完成",
        "failed": "失败",
        "cancelled": "已取消",
        "aborted_condition_changed": "设备条件变化中断",
    }.get(run.status, run.status)
    add_event(
        "run:finished",
        "run.finished",
        run.finished_at,
        f"Run {final_status}",
        {"status": run.status},
    )
    events.sort(key=lambda event: (event["occurred_at"], event["id"]))
    return {
        "run_id": str(run.id),
        "events": events,
        "failed_case_count": failed_case_count,
        "omitted_failed_cases": max(0, timed_failed_case_count - len(failed_cases)),
        "untimed_failed_cases": max(0, failed_case_count - timed_failed_case_count),
    }


@router.get("/{run_id}/targets/{target_id}/cell")
def get_target_cell(
    run_id: uuid.UUID,
    target_id: uuid.UUID,
    evaluation_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("run:read")),
) -> dict[str, Any]:
    """矩阵单元格下钻：返回 CER 直方图与最多 20 条最差样本（M8 §9.4）。"""
    run = db.get(Run, run_id)
    target = db.get(RunTarget, target_id)
    if run is None or target is None or target.run_id != run.id:
        raise ApiError("E_NOT_FOUND", "目标不存在", status_code=404)
    _require_run_evaluation(db, run.id, evaluation_id)
    return target_cell(db, run, target, evaluation_id=evaluation_id)


@router.get("/{run_id}/matrix")
def get_run_matrix(
    run_id: uuid.UUID,
    evaluation_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("run:read")),
) -> dict[str, Any]:
    """按 target 的指标矩阵；P1 只有 CER。"""
    run = db.get(Run, run_id)
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    _require_run_evaluation(db, run.id, evaluation_id)
    return run_matrix(db, run, evaluation_id=evaluation_id)


@router.get("/{run_id}/cases")
def list_run_cases(
    run_id: uuid.UUID,
    evaluation_id: uuid.UUID | None = None,
    limit: int = Query(default=20, ge=1, le=200),
    only_errors: bool = Query(default=False),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("run:read")),
) -> dict[str, Any]:
    """逐样本参考/识别/CER，用于 P0 人工抽查。"""
    run = db.get(Run, run_id)
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    evaluation = _require_run_evaluation(db, run.id, evaluation_id)
    if evaluation is None:
        evaluation = db.scalar(
            select(Evaluation)
            .where(Evaluation.run_id == run.id)
            .order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
        )
    case_score_join = CaseScore.case_result_id == CaseResult.id
    if evaluation is not None:
        case_score_join = and_(case_score_join, CaseScore.evaluation_id == evaluation.id)
    stmt = (
        select(CaseResult, Item.item_key, RefText.text, CaseScore.metrics)
        .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
        .join(Item, Item.id == CaseResult.item_id)
        .join(RefText, RefText.item_id == Item.id)
        .outerjoin(CaseScore, case_score_join)
        .where(RunTarget.run_id == run_id)
        .order_by(CaseResult.id)
        .limit(limit)
    )
    rows = db.execute(stmt).all()
    items = []
    for case, item_key, ref_text, metrics in rows:
        # M5 §2.1：新数据嵌套 {cer:{value,...}}；旧数据扁平 {cer: float}
        raw_cer = (metrics or {}).get("cer")
        # M5 §2.1：新数据嵌套 {cer:{value,...}}；旧数据扁平 {cer: float}
        cer = raw_cer.get("value") if isinstance(raw_cer, dict) else raw_cer
        if only_errors and not cer:
            continue
        items.append(
            {
                "case_id": case.id,
                "item_key": item_key,
                "status": case.status,
                "ref": ref_text,
                "hyp": case.text,
                "cer": cer,
                "wall_ms": case.wall_ms,
                "error": case.error,
            }
        )
    return {"items": items}


@router.get("/{run_id}")
def get_run(
    run_id: uuid.UUID,
    evaluation_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("run:read")),
) -> dict[str, Any]:
    """Run 详情 + 最新评分摘要。"""
    run = db.get(Run, run_id)
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    _require_run_evaluation(db, run.id, evaluation_id)
    return run_summary(db, run, evaluation_id=evaluation_id)


def _require_run_evaluation(
    db: Session,
    run_id: uuid.UUID,
    evaluation_id: uuid.UUID | None,
) -> Evaluation | None:
    """确保选择的评分属于当前 Run；省略 ID 时让调用方使用最新版本。"""
    if evaluation_id is None:
        return None
    evaluation = db.scalar(
        select(Evaluation).where(
            Evaluation.id == evaluation_id,
            Evaluation.run_id == run_id,
        )
    )
    if evaluation is None:
        raise ApiError("E_NOT_FOUND", "评分不存在或不属于此 Run", status_code=404)
    return evaluation
