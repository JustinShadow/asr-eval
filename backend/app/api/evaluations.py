"""Evaluation 逐样本明细与 Badcase 数据（M5 §3.2）。"""

import asyncio
import base64
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import Float, Integer, and_, case, cast, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import _bearer, current_claims
from app.api.deps import require
from app.core.db import get_db
from app.core.errors import ApiError
from app.core.rate_limit import require_limit, sse_connection_limiter
from app.core.sse import format_sse, sse_event_buffer
from app.domain.audio_variants import resolve_audio_path
from app.domain.auth import consume_stream_ticket
from app.domain.evaluation_worker import get_evaluation_worker
from app.domain.evaluations import cancel_evaluation, delete_evaluation, resume_evaluation
from app.domain.models import (
    AudioAsset,
    CaseResult,
    CaseScore,
    CaseTag,
    DatasetSubset,
    Device,
    Evaluation,
    Hypothesis,
    Item,
    MetricProfile,
    MetricValue,
    RefSegment,
    RefText,
    Run,
    RunTarget,
)
from app.domain.runs import create_evaluation_record
from app.scoring.diff import char_diff
from app.scoring.segments import annotate_segments, segment_bucket_summary
from app.scoring.speaker import der as speaker_der

router = APIRouter(prefix="/api/v1", tags=["evaluations"])


class EvaluationCreateIn(BaseModel):
    """对已有 Run 选择评分口径。"""

    metric_profile_id: uuid.UUID


def _evaluation_payload(evaluation: Evaluation) -> dict[str, Any]:
    return {
        "id": str(evaluation.id),
        "run_id": str(evaluation.run_id),
        "metric_profile_id": str(evaluation.metric_profile_id),
        "profile_hash": evaluation.profile_hash,
        "scorer_version": evaluation.scorer_version,
        "trigger": evaluation.trigger,
        "status": evaluation.status,
        "progress": evaluation.progress,
        "coverage": evaluation.coverage,
        "metrics": evaluation.metrics,
        "created_at": evaluation.created_at.isoformat() if evaluation.created_at else None,
        "finished_at": evaluation.finished_at.isoformat() if evaluation.finished_at else None,
    }


@router.post("/runs/{run_id}/evaluations", status_code=201)
def create_run_evaluation(
    run_id: uuid.UUID,
    body: EvaluationCreateIn,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("evaluation:create")),
) -> dict[str, Any]:
    """将已有识别结果送入有界评分队列，不重新执行设备。"""
    evaluation = create_evaluation_record(db, run_id, body.metric_profile_id, trigger="manual")
    get_evaluation_worker().enqueue(
        evaluation.id,
        sessionmaker(bind=db.get_bind(), expire_on_commit=False),
    )
    return _evaluation_payload(evaluation)


@router.get("/runs/{run_id}/evaluations")
def list_run_evaluations(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("evaluation:read")),
) -> dict[str, Any]:
    """列出 Run 的所有评分版本，最新一次在前。"""
    if db.get(Run, run_id) is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    rows = db.scalars(
        select(Evaluation)
        .where(Evaluation.run_id == run_id)
        .order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
    ).all()
    return {"items": [_evaluation_payload(row) for row in rows]}


@router.get("/evaluations/{evaluation_id}")
def get_evaluation(
    evaluation_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("evaluation:read")),
) -> dict[str, Any]:
    """读取评分版本、口径 hash、覆盖率和聚合结果。"""
    evaluation = db.get(Evaluation, evaluation_id)
    if evaluation is None:
        raise ApiError("E_NOT_FOUND", "Evaluation 不存在", status_code=404)
    return _evaluation_payload(evaluation)


@router.get("/evaluations/{evaluation_id}/speaker-analysis")
def speaker_analysis(
    evaluation_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("evaluation:read")),
) -> dict[str, Any]:
    """按样本返回 DER 错误区间与 speaker 映射，供说话人分析仪表使用。"""
    evaluation = db.get(Evaluation, evaluation_id)
    if evaluation is None:
        raise ApiError("E_NOT_FOUND", "Evaluation 不存在", status_code=404)
    run = db.get(Run, evaluation.run_id)
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    profile = db.get(MetricProfile, evaluation.metric_profile_id)
    der_config = (profile.params or {}).get("der") if profile is not None else None
    der_config = der_config if isinstance(der_config, dict) else {}
    if der_config.get("enabled") is False:
        return {"available": False, "reason": "metric_disabled", "cases": [], "summary": None}
    collar_ms = int(der_config.get("collar_ms", 250))
    skip_overlap = bool(der_config.get("skip_overlap", False))

    targets = db.scalars(select(RunTarget).where(RunTarget.run_id == run.id)).all()
    target_ids = [target.id for target in targets]
    if not target_ids:
        return {"available": False, "reason": "no_targets", "cases": [], "summary": None}
    rows = db.execute(
        select(CaseResult, CaseScore, Item, Hypothesis)
        .join(CaseScore, CaseScore.case_result_id == CaseResult.id)
        .join(Item, Item.id == CaseResult.item_id)
        .outerjoin(Hypothesis, Hypothesis.id == CaseResult.hypothesis_id)
        .where(
            CaseResult.run_target_id.in_(target_ids),
            CaseResult.status.in_(["ok", "cached"]),
        )
        .order_by(Item.id)
        .limit(limit)
    ).all()
    if not rows:
        return {"available": False, "reason": "no_scored_cases", "cases": [], "summary": None}

    item_ids = [item.id for _case, _score, item, _hyp in rows]
    ref_by_item: dict[int, list[tuple[str, int, int]]] = {}
    for segment in db.scalars(select(RefSegment).where(RefSegment.item_id.in_(item_ids))).all():
        ref_by_item.setdefault(segment.item_id, []).append(
            (segment.speaker or "unknown", int(segment.start_ms), int(segment.end_ms))
        )

    cases: list[dict[str, Any]] = []
    totals = {"miss": 0, "fa": 0, "conf": 0, "ref": 0}
    top_regions: list[dict[str, Any]] = []
    for case_result, _score, item, hypothesis in rows:
        ref_segments = sorted(ref_by_item.get(item.id, []), key=lambda seg: (seg[1], seg[2]))
        raw_segments = (
            (hypothesis.segments or {}).get("items")
            if hypothesis is not None and hypothesis.segments
            else []
        )
        hyp_segments: list[tuple[str, int, int]] = []
        for segment in raw_segments if isinstance(raw_segments, list) else []:
            try:
                hyp_segments.append(
                    (
                        str(segment.get("speaker") or "unknown"),
                        int(segment["start_ms"]),
                        int(segment["end_ms"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        reference_speakers = {speaker for speaker, _start, _end in ref_segments}
        hypothesis_speakers = {speaker for speaker, _start, _end in hyp_segments}
        result: dict[str, Any]
        if (
            hyp_segments
            and hypothesis_speakers <= {"spk", "-1", "unknown"}
            and len(reference_speakers) > 1
        ):
            result = {
                "value": None,
                "reason": "no_diarization_output",
                "errors": [],
                "mapping": {},
                "miss_frames": 0,
                "fa_frames": 0,
                "conf_frames": 0,
                "ref_frames": 0,
                "speakers": {"ref": len(reference_speakers), "hyp": 0},
            }
        else:
            result = speaker_der(
                ref_segments,
                hyp_segments,
                int(item.duration_ms),
                collar_ms=collar_ms,
                skip_overlap=skip_overlap,
                collect_errors=True,
            )
        errors = list(result.get("errors") or [])[:100]
        for region in errors:
            top_regions.append(
                {
                    **region,
                    "case_id": int(case_result.id),
                    "item_key": item.item_key,
                }
            )
        for key, source in (("miss", "miss_frames"), ("fa", "fa_frames"), ("conf", "conf_frames")):
            totals[key] += int(result.get(source) or 0)
        totals["ref"] += int(result.get("ref_frames") or 0)
        cases.append(
            {
                "case_id": int(case_result.id),
                "item_key": item.item_key,
                "duration_ms": int(item.duration_ms),
                "ref_speakers": result.get("speakers", {}).get("ref", 0),
                "hyp_speakers": result.get("speakers", {}).get("hyp", 0),
                "mapping": result.get("mapping") or {},
                "der": {
                    "value": result.get("value"),
                    "miss": result.get("miss"),
                    "fa": result.get("fa"),
                    "conf": result.get("conf"),
                    "miss_frames": result.get("miss_frames", 0),
                    "fa_frames": result.get("fa_frames", 0),
                    "conf_frames": result.get("conf_frames", 0),
                    "ref_frames": result.get("ref_frames", 0),
                    "reason": result.get("reason"),
                },
                "errors": errors,
            }
        )

    top_regions.sort(key=lambda region: region["end_ms"] - region["start_ms"], reverse=True)
    denominator = totals["ref"]
    missing_diarization = bool(cases) and all(
        case["der"].get("reason") == "no_diarization_output" for case in cases
    )
    return {
        "available": bool(denominator),
        "reason": None
        if denominator
        else ("no_diarization_output" if missing_diarization else "no_segment_timeline"),
        "summary": {
            "value": ((totals["miss"] + totals["fa"] + totals["conf"]) / denominator)
            if denominator
            else None,
            "miss": (totals["miss"] / denominator) if denominator else None,
            "fa": (totals["fa"] / denominator) if denominator else None,
            "conf": (totals["conf"] / denominator) if denominator else None,
            "miss_frames": totals["miss"],
            "fa_frames": totals["fa"],
            "conf_frames": totals["conf"],
            "ref_frames": denominator,
        },
        "cases": cases,
        "top_regions": top_regions[:200],
        "collar_ms": collar_ms,
        "skip_overlap": skip_overlap,
    }


@router.post("/evaluations/{evaluation_id}/cancel")
def cancel_evaluation_endpoint(
    evaluation_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("evaluation:cancel")),
) -> dict[str, Any]:
    """取消 queued 任务，或在当前 500 条评分页结束后停止。"""
    return _evaluation_payload(cancel_evaluation(db, evaluation_id))


@router.delete("/evaluations/{evaluation_id}", status_code=204)
def delete_evaluation_endpoint(
    evaluation_id: uuid.UUID,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("evaluation:delete")),
) -> Response:
    """删除终态评分；被报告引用时拒绝（M5 §9/M6 §5.8）。"""
    delete_evaluation(db, evaluation_id, actor=str(claims.get("sub", "")))
    return Response(status_code=204)


@router.post("/evaluations/{evaluation_id}/resume")
def resume_evaluation_endpoint(
    evaluation_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("evaluation:create")),
) -> dict[str, Any]:
    """从最后已提交的 case_result 游标继续评分。"""
    evaluation = resume_evaluation(db, evaluation_id)
    get_evaluation_worker().enqueue(
        evaluation.id,
        sessionmaker(bind=db.get_bind(), expire_on_commit=False),
    )
    return _evaluation_payload(evaluation)


@router.get("/evaluations/{evaluation_id}/events")
async def evaluation_events(
    evaluation_id: uuid.UUID,
    ticket: str | None = Query(default=None),
    db: Session = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    last_event_id_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    last_event_id_query: Annotated[str | None, Query(alias="last_event_id")] = None,
) -> StreamingResponse:
    """输出 Evaluation 评分进度 SSE；支持 Bearer 或一次性 stream ticket。"""
    if ticket is not None:
        username = consume_stream_ticket(db, ticket)
        if username is None:
            raise ApiError("E_TICKET_INVALID", "stream ticket 无效或已使用", status_code=401)
    else:
        claims = current_claims(credentials=credentials, db=db)
        if "evaluation:read" not in claims.get("perms", []):
            raise ApiError("E_FORBIDDEN", "缺少 evaluation:read 权限", status_code=403)
        username = str(claims.get("sub", ""))

    if db.get(Evaluation, evaluation_id) is None:
        raise ApiError("E_NOT_FOUND", "Evaluation 不存在", status_code=404)
    if not sse_connection_limiter.acquire(username, 5):
        require_limit(1, message="同时打开的事件流数量已达上限")
    last_event_id = last_event_id_header or last_event_id_query

    async def generate() -> AsyncIterator[str]:
        deadline = asyncio.get_running_loop().time() + 30 * 60
        stream_key = f"evaluation:{evaluation_id}"
        replay = sse_event_buffer.replay(stream_key, last_event_id)
        for event in replay.events:
            yield format_sse(event)
        last_payload = replay.latest.data if replay.latest else None
        last_status: str | None = None
        last_heartbeat = time.monotonic()
        if replay.resync:
            yield format_sse(
                sse_event_buffer.publish(stream_key, "resync", {"reason": "history_unavailable"})
            )
        db.expire_all()
        initial = db.get(Evaluation, evaluation_id)
        if initial is None:
            return
        initial_payload = json.dumps(
            {
                "evaluation_id": str(initial.id),
                "status": initial.status,
                "progress": initial.progress,
                "coverage": initial.coverage,
            },
            ensure_ascii=False,
        )
        if last_event_id is None or replay.resync or last_payload != initial_payload:
            yield format_sse(
                sse_event_buffer.publish(
                    stream_key,
                    _evaluation_event_name(initial.status),
                    json.loads(initial_payload),
                )
            )
            last_payload = initial_payload
        last_status = initial.status
        while asyncio.get_running_loop().time() <= deadline:
            db.expire_all()
            current = db.get(Evaluation, evaluation_id)
            if current is None:
                break
            payload = json.dumps(
                {
                    "evaluation_id": str(current.id),
                    "status": current.status,
                    "progress": current.progress,
                    "coverage": current.coverage,
                },
                ensure_ascii=False,
            )
            if payload != last_payload:
                event_name = (
                    _evaluation_event_name(current.status)
                    if current.status != last_status
                    else "evaluation.progress"
                )
                yield format_sse(
                    sse_event_buffer.publish(stream_key, event_name, json.loads(payload))
                )
                last_payload = payload
                last_status = current.status
                last_heartbeat = time.monotonic()
            elif time.monotonic() - last_heartbeat >= 15:
                yield ": heartbeat\n\n"
                last_heartbeat = time.monotonic()
            if current.status in {"completed", "degraded", "failed", "cancelled"}:
                yield "event: end\ndata: {}\n\n"
                break
            await asyncio.sleep(1)
        else:
            yield 'event: end\ndata: {"reason": "expired"}\n\n'

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


def _evaluation_event_name(status: str) -> str:
    """按 Evaluation 状态映射 M7 定义的事件名。"""
    return {
        "queued": "evaluation.queued",
        "scoring": "evaluation.scoring",
        "completed": "evaluation.completed",
        "degraded": "evaluation.completed",
        "failed": "evaluation.failed",
        "cancelled": "evaluation.failed",
    }.get(status, "evaluation.progress")


# 该路由支持 ticket 或 Bearer 两种认证，静态门禁据此识别其认证分支。
evaluation_events.__dict__["requires_authentication"] = True


def _cer_expr() -> Any:
    """样本 CER 表达式：新数据取 metrics.cer.value（M5 §2.1），旧扁平数据兜底。"""
    return func.coalesce(
        cast(CaseScore.metrics["cer"]["value"].astext, Float),
        cast(CaseScore.metrics["cer"].astext, Float),
    )


def _encode_cursor(cer: float, score_id: int) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({"cer": cer, "id": score_id}, separators=(",", ":")).encode()
    ).decode()


def _decode_cursor(cursor: str) -> tuple[float, int]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return float(payload["cer"]), int(payload["id"])
    except Exception as exc:
        raise ApiError("E_VALIDATION", "非法游标", status_code=400) from exc


@router.get("/evaluations/{evaluation_id}/cases")
def list_cases(
    evaluation_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    only_errors: bool = Query(default=True),
    cer_min: float = Query(default=0.0, ge=0.0, le=10.0),
    lang: str | None = Query(default=None, max_length=16),
    error_type: Literal["substitution", "deletion", "insertion"] | None = None,
    bucket: str | None = Query(default=None, max_length=160),
    subset: str | None = Query(default=None, max_length=32),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("evaluation:read")),
) -> dict[str, Any]:
    """按指标分数降序列出 Badcase，并应用 URL 可分享的筛选条件。"""
    if db.get(Evaluation, evaluation_id) is None:
        raise ApiError("E_NOT_FOUND", "Evaluation 不存在", status_code=404)

    cer = _cer_expr()
    stmt = (
        select(
            CaseScore.id,
            cer,
            CaseResult.id,
            Item.item_key,
            RefText.text,
            CaseResult.text,
            Item.lang,
            Item.buckets,
            Item.content_uid,
            Item.pair_key,
            RunTarget.subset,
        )
        .join(CaseResult, CaseResult.id == CaseScore.case_result_id)
        .join(Item, Item.id == CaseResult.item_id)
        .join(RefText, RefText.item_id == Item.id)
        .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
        .where(CaseScore.evaluation_id == evaluation_id)
        .order_by(cer.desc(), CaseScore.id.desc())
        .limit(limit + 1)
    )
    threshold = max(cer_min, 0.0 if only_errors else -1.0)
    if only_errors or cer_min > 0:
        stmt = stmt.where(cer > threshold)
    if lang:
        stmt = stmt.where(Item.lang == lang)
    if error_type:
        component = {
            "substitution": "s",
            "deletion": "d",
            "insertion": "i",
        }[error_type]
        component_count = cast(CaseScore.metrics["cer"][component].astext, Integer)
        stmt = stmt.where(func.coalesce(component_count, 0) > 0)
    if bucket:
        dimension, separator, value = bucket.partition(":")
        if not separator or not dimension or not value:
            raise ApiError("E_VALIDATION", "bucket 筛选格式应为 dimension:value", status_code=400)
        if dimension == "duration_bucket":
            duration_bucket_expr = case(
                (Item.duration_ms < 5_000, "lt_5s"),
                (Item.duration_ms < 15_000, "5_15s"),
                (Item.duration_ms < 60_000, "15_60s"),
                (Item.duration_ms < 300_000, "1_5min"),
                else_="gt_5min",
            )
            stmt = stmt.where(duration_bucket_expr == value)
        else:
            stmt = stmt.where(Item.buckets[dimension].astext == value)
    if subset:
        stmt = stmt.where(RunTarget.subset == subset)
    if cursor:
        last_cer, last_id = _decode_cursor(cursor)
        stmt = stmt.where(or_(cer < last_cer, and_(cer == last_cer, CaseScore.id < last_id)))

    rows = db.execute(stmt).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    tags = {
        int(score_id): int(tag_value)
        for score_id, tag_value in db.execute(
            select(CaseTag.case_score_id, CaseTag.tag).where(
                CaseTag.case_score_id.in_([int(row[0]) for row in rows])
            )
        ).all()
    }
    items = [
        {
            "case_id": int(row[0]),
            "cer": float(row[1]),
            "case_result_id": int(row[2]),
            "item_key": row[3],
            "ref": row[4],
            "hyp": row[5],
            "tag": tags.get(int(row[0])),
            "lang": row[6],
            "buckets": row[7] or {},
            "content_uid": row[8],
            "pair_key": row[9],
            "subset": row[10],
        }
        for row in rows
    ]
    next_cursor = (
        _encode_cursor(items[-1]["cer"], items[-1]["case_id"]) if has_more and items else None
    )
    return {"items": items, "next_cursor": next_cursor}


@router.get("/evaluations/{evaluation_id}/cases/{case_id}")
def get_case(
    evaluation_id: uuid.UUID,
    case_id: int,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("evaluation:read")),
) -> dict[str, Any]:
    """单条 Badcase：参考/识别/字符对齐/音频/指标。"""
    row = db.execute(
        select(CaseScore, CaseResult, Item, RefText, AudioAsset)
        .join(CaseResult, CaseResult.id == CaseScore.case_result_id)
        .join(Item, Item.id == CaseResult.item_id)
        .join(RefText, RefText.item_id == Item.id)
        .join(AudioAsset, AudioAsset.id == Item.audio_asset_id)
        .where(CaseScore.id == case_id, CaseScore.evaluation_id == evaluation_id)
    ).first()
    if row is None:
        raise ApiError("E_NOT_FOUND", "Badcase 不存在", status_code=404)

    score, case, item, ref, _asset = row
    alignment = char_diff(ref.text, case.text or "")
    raw_segments = db.scalars(
        select(RefSegment)
        .where(RefSegment.item_id == item.id)
        .order_by(RefSegment.start_ms, RefSegment.seq)
    ).all()
    segment_payload = annotate_segments(
        [
            {
                "speaker": segment.speaker,
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "text": segment.text,
            }
            for segment in raw_segments
        ],
        alignment,
    )
    return {
        "case_id": int(score.id),
        "item_id": int(item.id),
        "item_key": item.item_key,
        "ref": ref.text,
        "hyp": case.text or "",
        "alignment": alignment,
        "metrics": score.metrics,
        "wall_ms": case.wall_ms,
        "status": case.status,
        "duration_ms": item.duration_ms,
        "speakers": item.speakers,
        "buckets": item.buckets,
        "audio_url": f"/api/v1/items/{item.id}/audio",
        "tag": db.scalar(select(CaseTag.tag).where(CaseTag.case_score_id == case_id)),
        # 参考分段（M1 §4）+ 分段级对齐（M5 §5）：Badcase 泳道/通读/概览的数据源
        "segments": segment_payload,
        "segment_buckets": segment_bucket_summary(segment_payload) if segment_payload else [],
    }


@router.get("/items/{item_id}/audio")
def item_audio(
    item_id: int,
    profile: Literal["as_is", "mono_mix", "mono_ch0", "16k_mono", "16k_mono_ch0"] | None = Query(
        default=None
    ),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("dataset:read")),
) -> FileResponse:
    """返回样本 profile 音频；缺省采用其 subset profile（M1 §3.3/§5.4）。"""
    item = db.get(Item, item_id)
    if item is None:
        raise ApiError("E_NOT_FOUND", "item 不存在", status_code=404)
    asset = db.get(AudioAsset, item.audio_asset_id)
    if asset is None:
        raise ApiError("E_NOT_FOUND", "音频文件不存在", status_code=404)
    subset = db.get(DatasetSubset, item.subset_id)
    selected_profile = profile or (subset.audio_profile if subset else "as_is")
    audio_path = resolve_audio_path(db, asset, selected_profile)
    if not audio_path.is_file():
        raise ApiError("E_NOT_FOUND", "音频文件不存在", status_code=404)
    db.commit()
    return FileResponse(audio_path, media_type="audio/wav", filename=audio_path.name)


@router.get("/evaluations/{evaluation_id}/summary")
def evaluation_summary(
    evaluation_id: uuid.UUID,
    level: str = Query(default="summary"),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("evaluation:read")),
) -> dict[str, Any]:
    """报告 payload 预览（M6 §3.1）：指标/分桶/样本状态，只读。"""
    from app.reports.builder import build_payload

    evaluation = db.get(Evaluation, evaluation_id)
    if evaluation is None:
        raise ApiError("E_NOT_FOUND", "Evaluation 不存在", status_code=404)
    return build_payload(db, evaluation, level=level)


@router.get("/evaluations/{evaluation_id}/matrix")
def evaluation_matrix(
    evaluation_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("evaluation:read")),
) -> dict[str, Any]:
    """目标 × 指标矩阵（M5 §3.2/§4）。

    单元格 = {metric, subset, value, ci, status, reason}，直接读 metric_value；
    报告/前端不再二次聚合。
    """
    evaluation = db.get(Evaluation, evaluation_id)
    if evaluation is None:
        raise ApiError("E_NOT_FOUND", "Evaluation 不存在", status_code=404)
    rows = db.execute(
        select(MetricValue, RunTarget, Device)
        .outerjoin(RunTarget, RunTarget.id == MetricValue.run_target_id)
        .outerjoin(Device, Device.id == RunTarget.device_id)
        .where(MetricValue.evaluation_id == evaluation_id)
        .order_by(RunTarget.id.nulls_first(), MetricValue.id)
    ).all()
    result = []
    for metric_value, _target, device in rows:
        result.append(
            {
                "target_id": str(metric_value.run_target_id)
                if metric_value.run_target_id
                else None,
                "device_name": device.name if device else None,
                "subset": metric_value.subset,
                "metric": metric_value.metric,
                "value": metric_value.value,
                "ci": [metric_value.ci_low, metric_value.ci_high]
                if metric_value.ci_low is not None
                else None,
                "n": metric_value.n,
                "scope": metric_value.scope,
                "detail": metric_value.detail or {},
                "status": metric_value.status,
                "reason": metric_value.reason,
                "bucket": metric_value.bucket or {},
            }
        )
    return {
        "evaluation_id": str(evaluation_id),
        "status": evaluation.status,
        "coverage": evaluation.coverage,
        "cells": result,
    }


@router.get("/evaluations/{evaluation_id}/buckets")
def evaluation_buckets(
    evaluation_id: uuid.UUID,
    dim: str = Query(default="duration_bucket"),
    scope: str = Query(default="micro", pattern="^(micro|macro)$"),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("evaluation:read")),
) -> dict[str, Any]:
    """按桶聚合指标（M5 §3.2）。

    分母只算可评（有 cer.value）的样本；桶值来自 item.buckets[dim]，
    duration_bucket 由时长实时推导（与 M4 §3 边界一致）。
    """
    evaluation = db.get(Evaluation, evaluation_id)
    if evaluation is None:
        raise ApiError("E_NOT_FOUND", "Evaluation 不存在", status_code=404)

    if dim == "duration_bucket":
        stored_rows = db.scalars(
            select(MetricValue)
            .where(
                MetricValue.evaluation_id == evaluation_id,
                MetricValue.metric == "cer",
                MetricValue.scope == scope,
                MetricValue.run_target_id.is_(None),
                MetricValue.bucket[dim].astext.is_not(None),
            )
            .order_by(MetricValue.bucket[dim].astext)
        ).all()
        if stored_rows:
            return {
                "evaluation_id": str(evaluation_id),
                "dim": dim,
                "metric": "cer",
                "scope": scope,
                "buckets": [
                    {
                        "bucket": row.bucket,
                        "metric": row.metric,
                        "value": row.value,
                        "ci": [row.ci_low, row.ci_high] if row.ci_low is not None else None,
                        "n": row.n,
                        "status": row.status,
                        "reason": row.reason,
                    }
                    for row in stored_rows
                ],
            }

    value_expr: Any
    if dim == "duration_bucket":
        # 与 M4 §3 边界一致（duration_bucket 复用同一套桶）
        value_expr = case(
            (Item.duration_ms < 5_000, "lt_5s"),
            (Item.duration_ms < 15_000, "5_15s"),
            (Item.duration_ms < 60_000, "15_60s"),
            (Item.duration_ms < 300_000, "1_5min"),
            else_="gt_5min",
        )
    else:
        value_expr = func.coalesce(Item.buckets[dim].astext, "unknown")

    s_expr = func.coalesce(
        cast(CaseScore.metrics["cer"]["s"].astext, Integer),
        cast(CaseScore.metrics["edits"].astext, Integer),
    )
    d_expr = func.coalesce(cast(CaseScore.metrics["cer"]["d"].astext, Integer), 0)
    i_expr = func.coalesce(cast(CaseScore.metrics["cer"]["i"].astext, Integer), 0)
    n_expr = func.coalesce(
        cast(CaseScore.metrics["cer"]["n"].astext, Integer),
        cast(CaseScore.metrics["ref_len"].astext, Integer),
    )
    rows = db.execute(
        select(
            value_expr,
            func.sum(s_expr),
            func.sum(d_expr),
            func.sum(i_expr),
            func.sum(n_expr),
            func.count(),
        )
        .join(CaseResult, CaseResult.id == CaseScore.case_result_id)
        .join(Item, Item.id == CaseResult.item_id)
        .where(CaseScore.evaluation_id == evaluation_id)
        .group_by(value_expr)
        .order_by(value_expr)
    ).all()
    buckets = []
    for bucket_value, s_sum, d_sum, i_sum, n_sum, count in rows:
        ref_len = int(n_sum or 0)
        edits = int(s_sum or 0) + int(d_sum or 0) + int(i_sum or 0)
        buckets.append(
            {
                "bucket": {dim: str(bucket_value)},
                "metric": "cer",
                "value": (edits / ref_len) if ref_len else None,
                "n": int(count),
                "status": "ok" if ref_len else "na",
                "reason": None if ref_len else "empty_bucket",
            }
        )
    return {"evaluation_id": str(evaluation_id), "dim": dim, "metric": "cer", "buckets": buckets}


class TagRequest(BaseModel):
    """打标请求：1-9。"""

    tag: int = Field(ge=1, le=9)


@router.put("/evaluations/{evaluation_id}/cases/{case_id}/tag")
def set_case_tag(
    evaluation_id: uuid.UUID,
    case_id: int,
    body: TagRequest,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("evaluation:create")),
) -> dict[str, Any]:
    """人工打标（M8 §9.7）：一条样本一个 1-9 标签，upsert。"""
    score = db.get(CaseScore, case_id)
    if score is None or score.evaluation_id != evaluation_id:
        raise ApiError("E_NOT_FOUND", "Badcase 不存在", status_code=404)
    tag = db.scalar(select(CaseTag).where(CaseTag.case_score_id == case_id))
    if tag is None:
        tag = CaseTag(
            evaluation_id=evaluation_id,
            case_score_id=case_id,
            tag=body.tag,
            created_by=str(claims.get("sub")) or None,
        )
        db.add(tag)
    else:
        tag.tag = body.tag
        tag.created_by = str(claims.get("sub")) or None
    db.commit()
    return {"case_id": case_id, "tag": tag.tag}


@router.delete("/evaluations/{evaluation_id}/cases/{case_id}/tag", status_code=204)
def clear_case_tag(
    evaluation_id: uuid.UUID,
    case_id: int,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("evaluation:create")),
) -> None:
    """清除打标。"""
    score = db.get(CaseScore, case_id)
    if score is None or score.evaluation_id != evaluation_id:
        raise ApiError("E_NOT_FOUND", "Badcase 不存在", status_code=404)
    tag = db.scalar(select(CaseTag).where(CaseTag.case_score_id == case_id))
    if tag is not None:
        db.delete(tag)
        db.commit()
