"""ETA 估算（M4）。

设计要点（docs/m4-eta-design.md）：
- 基线从历史评测结果派生（case_result.rtf_dev），只读，没有新写路径；
- 四级取值 L1 deployment → L2 model_key → L3 kind → L4 内置常量，必须给出处；
- rtf 必须按时长桶分开（实测长音频 rtf 显著上升，单一 rtf 会低估一半）；
- accuracy/burst 公式：work = Σ(count × mean_d × rtf)，ETA = work/c + fixed；
- 失败与重试膨胀 ×(1 + retry_rate × 2)，默认 retry_rate 0.02。
"""

import statistics
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models import CaseResult, Deployment, Device, Hypothesis, Item, Run, RunTarget

# 时长桶边界（M4 §3）：<5s / 5~15s / 15~60s / 1~5min / >5min
BUCKET_EDGES_MS = (5_000, 15_000, 60_000, 300_000)
BUCKETS = ("lt_5s", "5_15s", "15_60s", "1_5min", "gt_5min")

# L4 内置常量（按 kind × 桶），来自 M4 §3 实测表；缺口径的 kind 用保守 0.35
_FALLBACK_RTF: dict[str, dict[str, float]] = {
    "etsstt": {
        "lt_5s": 0.244,
        "5_15s": 0.244,
        "15_60s": 0.267,
        "1_5min": 0.373,
        "gt_5min": 0.373,
    },
    "gateway": {
        "lt_5s": 0.15,
        "5_15s": 0.11,
        "15_60s": 0.11,
        "1_5min": 0.11,
        "gt_5min": 0.11,
    },
}
_DEFAULT_FALLBACK = 0.35

# 固定开销（M4 §5.3）：probe 20s + scoring ≈ A×0.005 + 报告 5s（accuracy 无声纹准备）
FIXED_PROBE_MS = 20_000
FIXED_REPORT_MS = 5_000
SCORING_RATIO = 0.005
# 重试率估计（M4 §5.4）
DEFAULT_RETRY_RATE = 0.02
# 运行中滚动估计的 warm-up：前 3 个样本不计入（M4 §6.3）
WARMUP_SKIP = 3


class Baseline(NamedTuple):
    """一个时长桶的 rtf 基线。"""

    p50: float
    p90: float
    n: int


def duration_bucket(duration_ms: int) -> str:
    """时长 → 桶名；必须按桶分开估，否则长音频被低估一半（M4 §3）。"""
    for index, edge in enumerate(BUCKET_EDGES_MS):
        if duration_ms < edge:
            return BUCKETS[index]
    return BUCKETS[-1]


def _fallback(kind: str, bucket: str) -> float:
    return _FALLBACK_RTF.get(kind, {}).get(bucket, _DEFAULT_FALLBACK)


def _history_rows(
    db: Session,
    *,
    deployment_id: Any = None,
    model_key: str | None = None,
    kind: str | None = None,
    since_days: int | None = None,
    params_hash: str,
    mode: str,
    chunk_ms: int,
    input_rate: float,
    concurrency: int,
) -> list[tuple[str, float]]:
    """按部署、执行参数与时长桶查历史 rtf_dev（M4 ETA §3/§4）。"""
    stmt = (
        select(Item.duration_ms, CaseResult.rtf_dev)
        .join(CaseResult, CaseResult.item_id == Item.id)
        .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
        .join(Run, Run.id == RunTarget.run_id)
        .join(Deployment, Deployment.id == RunTarget.deployment_id)
        .join(Hypothesis, Hypothesis.id == CaseResult.hypothesis_id)
        .join(Device, Device.id == RunTarget.device_id)
        .where(
            CaseResult.status == "ok",
            CaseResult.rtf_dev.is_not(None),
            Hypothesis.params_hash == params_hash,
            Hypothesis.mode == mode,
            Hypothesis.chunk_ms == chunk_ms,
            Hypothesis.input_rate == input_rate,
            Hypothesis.concurrency == concurrency,
        )
        .order_by(Run.created_at.desc())
    )
    if deployment_id is not None:
        stmt = stmt.where(RunTarget.deployment_id == deployment_id)
    if model_key is not None:
        stmt = stmt.where(Deployment.model_key == model_key)
    if kind is not None:
        stmt = stmt.where(Device.kind == kind)
    if since_days is not None:
        stmt = stmt.where(Run.created_at >= datetime.now(UTC) - timedelta(days=since_days))
    rows = db.execute(stmt).all()
    return [
        (duration_bucket(int(duration)), float(rtf)) for duration, rtf in rows if rtf is not None
    ]


def _baseline(rows: Sequence[tuple[str, float]], bucket: str, min_n: int) -> Baseline | None:
    values = [rtf for b, rtf in rows if b == bucket]
    if len(values) < min_n:
        return None
    ordered = sorted(values)
    p90_index = min(len(ordered) - 1, int(len(ordered) * 0.9))
    return Baseline(
        p50=statistics.median(ordered),
        p90=ordered[p90_index],
        n=len(ordered),
    )


def resolve_baseline(
    db: Session,
    *,
    deployment_id: Any,
    model_key: str | None,
    kind: str,
    bucket: str,
    params_hash: str,
    mode: str,
    chunk_ms: int,
    input_rate: float,
    concurrency: int,
) -> tuple[Baseline | None, str]:
    """四级取值（M4 §4）：L1 → L2 → L3 → L4，返回 (基线或 None, source)。

    L1/L2 至少 20 条、L3 至少 5 条；所有层级均匹配本次执行参数。
    """
    l1 = _baseline(
        _history_rows(
            db,
            deployment_id=deployment_id,
            model_key=model_key,
            since_days=7,
            params_hash=params_hash,
            mode=mode,
            chunk_ms=chunk_ms,
            input_rate=input_rate,
            concurrency=concurrency,
        ),
        bucket,
        min_n=20,
    )
    if l1 is not None:
        return l1, "deployment"
    l2 = _baseline(
        _history_rows(
            db,
            model_key=model_key,
            params_hash=params_hash,
            mode=mode,
            chunk_ms=chunk_ms,
            input_rate=input_rate,
            concurrency=concurrency,
        ),
        bucket,
        min_n=20,
    )
    if l2 is not None:
        return l2, "cross_device"
    l3 = _baseline(
        _history_rows(
            db,
            kind=kind,
            params_hash=params_hash,
            mode=mode,
            chunk_ms=chunk_ms,
            input_rate=input_rate,
            concurrency=concurrency,
        ),
        bucket,
        min_n=5,
    )
    if l3 is not None:
        return l3, "kind"
    return None, "fallback"


def confidence_for(n: int, source: str) -> str:
    """置信度（M4 §8）：任一桶 n<5 low；5≤n<20 medium；n≥20 且本设备 high。"""
    if n >= 20 and source == "deployment":
        return "high"
    if n >= 5:
        return "medium"
    return "low"


def speed_degraded(current_rtf: float, previous_rtf: float) -> bool:
    """识别相邻 ETA 窗口的推理速率下降是否超过 30%（M4 ETA §6.3）。"""
    return previous_rtf > 0 and current_rtf > previous_rtf / 0.7


def estimate_run(
    db: Session,
    *,
    deployment_id: Any,
    model_key: str | None,
    kind: str,
    durations_ms: Sequence[int],
    lanes: int = 1,
    params_hash: str = "",
    mode: str = "burst",
    chunk_ms: int = 1000,
    input_rate: float = 1.0,
) -> dict[str, Any]:
    """Run 创建时/执行前的首估；历史只匹配同一口径与执行条件。

    stream 使用音频喂入时长与推理耗时的较大值，按并发路数估算总用时。
    """
    duration_by_bucket: dict[str, int] = {}
    for duration in durations_ms:
        bucket = duration_bucket(int(duration))
        duration_by_bucket[bucket] = duration_by_bucket.get(bucket, 0) + int(duration)
    work_p50 = 0.0
    work_p90 = 0.0
    sources: list[str] = []
    baseline_counts: list[int] = []
    for bucket, bucket_duration in duration_by_bucket.items():
        baseline, source = resolve_baseline(
            db,
            deployment_id=deployment_id,
            model_key=model_key,
            kind=kind,
            bucket=bucket,
            params_hash=params_hash,
            mode=mode,
            chunk_ms=chunk_ms,
            input_rate=input_rate,
            concurrency=max(1, lanes),
        )
        sources.append(source)
        if baseline is None:
            rtf = _fallback(kind, bucket)
            p50_rtf = p90_rtf = rtf
            baseline_counts.append(0)
        else:
            p50_rtf = baseline.p50
            p90_rtf = baseline.p90
            baseline_counts.append(baseline.n)
        if mode == "stream":
            feed_rtf = 1 / max(input_rate, 0.01)
            p50_rtf = max(feed_rtf, p50_rtf)
            p90_rtf = max(feed_rtf, p90_rtf)
        work_p50 += bucket_duration * p50_rtf
        work_p90 += bucket_duration * p90_rtf

    audio_ms = sum(durations_ms)
    fixed = FIXED_PROBE_MS + audio_ms * SCORING_RATIO + FIXED_REPORT_MS
    inflation = 1 + DEFAULT_RETRY_RATE * 2
    source = (
        min(
            sources,
            key=lambda s: {"deployment": 0, "cross_device": 1, "kind": 2, "fallback": 3}.get(s, 4),
        )
        if sources
        else "fallback"
    )
    n_for_confidence = min(baseline_counts, default=0)
    return {
        "eta_ms": int((work_p50 / max(1, lanes) + fixed) * inflation),
        "eta_p90_ms": int((work_p90 / max(1, lanes) + fixed) * inflation),
        "source": source,
        "confidence": confidence_for(n_for_confidence, source),
    }


def rolling_update(
    *,
    elapsed_ms: int,
    observed: Sequence[tuple[str, float]],
    remaining_durations_ms: Sequence[int],
    kind: str,
    lanes: int = 1,
    mode: str = "burst",
    input_rate: float = 1.0,
) -> dict[str, Any]:
    """运行中滚动修正（M4 §6.2 burst）。

    observed 是已完成样本的 (bucket, wall_ms/duration_ms)，不含 warm-up 前 3 条；
    按桶维护 median，避免被个别慢样本带偏（§6.3）。
    """
    by_bucket: dict[str, list[float]] = {}
    for bucket, ratio in observed:
        by_bucket.setdefault(bucket, []).append(ratio)
    remaining_p50 = 0.0
    remaining_p90 = 0.0
    n = 0
    for duration in remaining_durations_ms:
        bucket = duration_bucket(int(duration))
        ratios = by_bucket.get(bucket)
        if ratios:
            ordered = sorted(ratios)
            p90_index = min(len(ordered) - 1, int(len(ordered) * 0.9))
            p50_rtf = statistics.median(ordered)
            p90_rtf = ordered[p90_index]
            n += len(ordered)
        else:
            p50_rtf = p90_rtf = _fallback(kind, bucket)
        if mode == "stream":
            feed_rtf = 1 / max(input_rate, 0.01)
            p50_rtf = max(feed_rtf, p50_rtf)
            p90_rtf = max(feed_rtf, p90_rtf)
        remaining_p50 += duration * p50_rtf
        remaining_p90 += duration * p90_rtf
    source = "observed" if observed else "fallback"
    fixed_left = FIXED_REPORT_MS
    return {
        "eta_ms": int(elapsed_ms + remaining_p50 / max(1, lanes) + fixed_left),
        "eta_p90_ms": int(elapsed_ms + remaining_p90 / max(1, lanes) + fixed_left),
        "source": source,
        "confidence": confidence_for(n, source),
    }
