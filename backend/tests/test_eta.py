"""ETA 估算的单元测试（M4 §3/§4/§5）。"""

from app.domain import eta as eta_module
from app.domain.eta import (
    Baseline,
    confidence_for,
    duration_bucket,
    estimate_run,
    resolve_baseline,
    rolling_update,
    speed_degraded,
)


def test_duration_bucket_boundaries() -> None:
    assert duration_bucket(4_999) == "lt_5s"
    assert duration_bucket(5_000) == "5_15s"
    assert duration_bucket(15_000) == "15_60s"
    assert duration_bucket(60_000) == "1_5min"
    assert duration_bucket(300_000) == "gt_5min"


def test_rolling_update_uses_observed_median() -> None:
    # 剩余 2 条各 10s，桶 5_15s 观测 rtf median=0.2 → 剩余工作量 4000ms
    result = rolling_update(
        elapsed_ms=1_000,
        observed=[("5_15s", 0.1), ("5_15s", 0.2), ("5_15s", 0.3), ("lt_5s", 0.9)],
        remaining_durations_ms=[10_000, 10_000],
        kind="etsstt",
    )
    assert result["eta_ms"] == 1_000 + 4_000 + 5_000
    assert result["source"] == "observed"


def test_rolling_update_fallback_without_observed() -> None:
    result = rolling_update(
        elapsed_ms=0,
        observed=[],
        remaining_durations_ms=[1_000],
        kind="etsstt",
    )
    # 无观测 → fallback 常量 0.244（etsstt lt_5s）
    assert result["source"] == "fallback"
    assert result["confidence"] == "low"


def test_confidence_levels() -> None:
    assert confidence_for(25, "deployment") == "high"
    assert confidence_for(10, "cross_device") == "medium"
    assert confidence_for(2, "kind") == "low"


def test_perf_eta_accounts_for_parallel_lanes(monkeypatch) -> None:
    """性能评测 ETA 按并发路数折算推理工作量。"""
    monkeypatch.setattr(
        eta_module,
        "resolve_baseline",
        lambda *_args, **_kwargs: (Baseline(p50=0.5, p90=0.8, n=10), "deployment"),
    )
    one_lane = estimate_run(
        object(),
        deployment_id="deployment",
        model_key="model",
        kind="gateway",
        durations_ms=[10_000],
        params_hash="profile",
        mode="burst",
    )
    two_lanes = estimate_run(
        object(),
        deployment_id="deployment",
        model_key="model",
        kind="gateway",
        durations_ms=[10_000],
        lanes=2,
        params_hash="profile",
        mode="burst",
    )

    assert two_lanes["eta_ms"] < one_lane["eta_ms"]
    assert two_lanes["eta_p90_ms"] < one_lane["eta_p90_ms"]


def test_stream_eta_respects_realtime_feed_rate(monkeypatch) -> None:
    """实时模式不能估得比音频喂入时间更短。"""
    monkeypatch.setattr(
        eta_module,
        "resolve_baseline",
        lambda *_args, **_kwargs: (Baseline(p50=0.2, p90=0.3, n=30), "deployment"),
    )
    estimate = estimate_run(
        object(),
        deployment_id="deployment",
        model_key="model",
        kind="gateway",
        durations_ms=[10_000],
        params_hash="profile",
        mode="stream",
        input_rate=1.0,
    )

    # 单条 10 秒音频喂入后，还必须有 probe 与报告固定时长。
    assert estimate["eta_ms"] >= 10_000 + 20_000 + 5_000


def test_stream_rolling_eta_respects_feed_time_and_speed_warning_threshold() -> None:
    """滚动 ETA 保留实时喂入下限，速度异常阈值对应 30% 下降。"""
    estimate = rolling_update(
        elapsed_ms=0,
        observed=[("5_15s", 0.2)],
        remaining_durations_ms=[10_000],
        kind="gateway",
        mode="stream",
        input_rate=1.0,
    )

    assert estimate["eta_ms"] >= 10_000 + 5_000
    assert speed_degraded(0.15, 0.10)
    assert not speed_degraded(0.14, 0.10)


def test_baseline_uses_exact_conditions_and_documented_sample_thresholds(monkeypatch) -> None:
    """历史层级按参数条件匹配，deployment/model 需 20 条、kind 需 5 条。"""
    calls: list[dict[str, object]] = []

    def history(_db, **conditions):
        calls.append(conditions)
        if conditions.get("deployment_id") is not None:
            return [("lt_5s", 0.2)] * 19
        if conditions.get("model_key") is not None:
            return [("lt_5s", 0.3)] * 20
        return [("lt_5s", 0.4)] * 5

    monkeypatch.setattr(eta_module, "_history_rows", history)
    baseline, source = resolve_baseline(
        object(),
        deployment_id="deployment",
        model_key="model",
        kind="gateway",
        bucket="lt_5s",
        params_hash="profile-hash",
        mode="stream",
        chunk_ms=30,
        input_rate=1.0,
        concurrency=3,
    )

    assert source == "cross_device"
    assert baseline is not None and baseline.n == 20
    assert all(
        call["params_hash"] == "profile-hash"
        and call["mode"] == "stream"
        and call["chunk_ms"] == 30
        and call["input_rate"] == 1.0
        and call["concurrency"] == 3
        for call in calls
    )
