"""Run 创建、执行与评分聚合。"""

import asyncio
import hashlib
import logging
import statistics
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Integer, cast, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.etsstt import TranscribeOutcome, transcribe_file
from app.adapters.ssh_tunnel import device_endpoint
from app.core.errors import ApiError
from app.core.sse import sse_event_buffer
from app.domain import audit
from app.domain.audio_variants import resolve_audio_path
from app.domain.benchmarks import sampled_item_ids
from app.domain.credentials import CredentialUnavailable, credential_for_device
from app.domain.devices import run_probe
from app.domain.eta import BUCKETS, WARMUP_SKIP, duration_bucket, rolling_update, speed_degraded
from app.domain.metric_profiles import TEMPLATE_PARAMS, profile_hash
from app.domain.models import (
    AudioAsset,
    Benchmark,
    CaseResult,
    CaseScore,
    DatasetSubset,
    DatasetVersion,
    Deployment,
    Device,
    DeviceLease,
    Evaluation,
    Hypothesis,
    Item,
    Job,
    MetricProfile,
    MetricValue,
    OpsAction,
    RefSegment,
    RefText,
    Report,
    Run,
    RunTarget,
)
from app.domain.resource_sampler import sample_resources_loop, stop_sampler
from app.scoring.bootstrap import f1_ci, mean_ci, ratio_ci
from app.scoring.cer import cer as compute_cer
from app.scoring.coverage import coverage_state
from app.scoring.numeric import NumericCerResult, numeric_cer, numeric_reference_length
from app.scoring.performance import summarize_latency, throughput

DEFAULT_PROFILE_NAME = "default_zh"
# 口径结构按 M3 §2.1；default_zh 默认全开，由 resolver 判 na（M3 §6）
DEFAULT_PROFILE_PARAMS: dict[str, Any] = TEMPLATE_PARAMS[DEFAULT_PROFILE_NAME]

# 单条失败处理（M4 §5.6）：连接错误/超时重试 2 次，退避 1s/3s
RETRY_DELAYS_S = (1.0, 3.0)
# 连续失败熔断阈值；超过则中断当前 target（M4 §5.6）
BREAKER_THRESHOLD = 5
# Run 期间每 5 分钟采集一次身份；不创建 ASR/WebSocket 会话（M4 §5.8）。
IDENTITY_CHECK_INTERVAL_S = 300
logger = logging.getLogger(__name__)
# 可重试的错误前缀：网络层失败；引擎错误与 finalize 超时不重试
_RETRYABLE_PREFIXES = (
    "TimeoutError",
    "ConnectionError",
    "ConnectionClosed",
    "OSError",
    "WebSocketException",
    "InvalidStatus",
)


def ensure_default_profile(db: Session) -> MetricProfile:
    """P0 只用一个固定口径；P1 再做编辑器。"""
    profile = db.scalar(
        select(MetricProfile)
        .where(MetricProfile.name == DEFAULT_PROFILE_NAME)
        .order_by(MetricProfile.version.desc())
    )
    if profile is not None:
        return profile
    profile = MetricProfile(
        name=DEFAULT_PROFILE_NAME,
        params=DEFAULT_PROFILE_PARAMS,
        profile_hash=profile_hash(DEFAULT_PROFILE_PARAMS),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def ensure_benchmark(db: Session, version: DatasetVersion, profile: MetricProfile) -> Benchmark:
    """每个数据集版本自动生成一个默认 Benchmark。"""
    name = f"{version.version}-default"
    benchmark = db.scalar(
        select(Benchmark).where(Benchmark.dataset_version_id == version.id, Benchmark.name == name)
    )
    if benchmark is not None:
        return benchmark
    benchmark = Benchmark(
        name=name,
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        subsets=["all"],
        sampling={},
    )
    db.add(benchmark)
    db.commit()
    db.refresh(benchmark)
    return benchmark


def create_run(
    db: Session,
    *,
    name: str,
    dataset_version_id: uuid.UUID,
    created_by: str | None,
    benchmark_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
    targets: list[tuple[uuid.UUID, str]] | None = None,
    deployment_uids: dict[str, str] | None = None,
    kind: str = "accuracy",
    lanes: int = 1,
    auto_score: bool = True,
    deterministic: bool = False,
    group_id: uuid.UUID | None = None,
) -> Run:
    """创建 Run 与一个或多个 target；device_id 为单目标兼容参数。

    D-M4-11：可通过 deployment_uids 锁定部署（key 为 "device_id:subset"），
    当前部署指纹不一致 → 409 E_DEPLOYMENT_CHANGED（回归场景用）。
    """
    if kind not in {"accuracy", "perf"}:
        raise ApiError("E_VALIDATION", "Run kind 只能是 accuracy 或 perf", status_code=422)
    if not 1 <= lanes <= 8 or (kind != "perf" and lanes != 1):
        raise ApiError("E_VALIDATION", "仅 perf Run 可使用 1–8 路并发", status_code=422)
    version = db.get(DatasetVersion, dataset_version_id)
    if version is None:
        raise ApiError("E_NOT_FOUND", "数据集版本不存在", status_code=404)
    # M4 §5.2：ready / ready_with_errors 都可执行
    if version.status not in {"ready", "ready_with_errors"}:
        raise ApiError("E_SEMANTIC", "数据集版本未就绪", status_code=422)

    specs = targets or ([(device_id, "all")] if device_id is not None else [])
    if not specs:
        raise ApiError("E_SEMANTIC", "至少需要一个目标设备", status_code=422)
    if len({(str(d), s) for d, s in specs}) != len(specs):
        raise ApiError("E_SEMANTIC", "目标设备与 subset 不能重复", status_code=422)

    uids = deployment_uids or {}
    snapshot_targets: list[dict[str, object]] = []
    for target_device_id, subset in specs:
        device = db.get(Device, target_device_id)
        if device is None:
            raise ApiError("E_NOT_FOUND", f"设备 {target_device_id} 不存在", status_code=404)
        deployment = db.scalar(
            select(Deployment)
            .where(Deployment.device_id == device.id)
            .order_by(Deployment.captured_at.desc())
        )
        if deployment is None or not deployment.model_key:
            raise ApiError("E_SEMANTIC", "设备尚未探测，缺少部署快照", status_code=422)
        if kind == "perf":
            limits = deployment.limits if isinstance(deployment.limits, dict) else {}
            slot_limit = limits.get("realtime_slots")
            if isinstance(slot_limit, bool) or not isinstance(slot_limit, int):
                slot_limit = 3 if device.kind == "gateway" else 1
            if lanes > slot_limit:
                raise ApiError(
                    "E_SEMANTIC",
                    f"设备 {device.name} 当前最多支持 {slot_limit} 路并发",
                    status_code=422,
                )
        # 可选锁定：回归场景冻结部署，当前指纹不一致直接拒绝（D-M4-11）
        locked_uid = uids.get(f"{target_device_id}:{subset}") or uids.get(str(target_device_id))
        if locked_uid is not None and locked_uid != deployment.uid:
            raise ApiError(
                "E_DEPLOYMENT_CHANGED",
                f"设备 {device.name} 部署已变化（锁定 {locked_uid}，当前 {deployment.uid}）",
                status_code=409,
            )
        snapshot_targets.append(
            {
                "device_id": str(device.id),
                "name": device.name,
                "sn": device.sn,
                "subset": subset,
                "model_key": deployment.model_key,
                "deployment_id": str(deployment.id),
                "deployment_uid": deployment.uid,
                "deployment_uid_locked": locked_uid is not None,
            }
        )

    # target 需要冻结 deployment_id（D-M4-9：RunTarget = (deployment, subset)）
    deployment_ids = {
        f"{t['device_id']}:{t['subset']}": uuid.UUID(str(t["deployment_id"]))
        for t in snapshot_targets
    }

    if benchmark_id is None:
        profile = ensure_default_profile(db)
        benchmark = ensure_benchmark(db, version, profile)
    else:
        selected_benchmark = db.get(Benchmark, benchmark_id)
        if selected_benchmark is None:
            raise ApiError("E_NOT_FOUND", "Benchmark 不存在", status_code=404)
        if selected_benchmark.dataset_version_id != version.id:
            raise ApiError("E_SEMANTIC", "Benchmark 与所选数据集版本不一致", status_code=422)
        if selected_benchmark.archived:
            raise ApiError("E_SEMANTIC", "已归档的 Benchmark 不能用于新 Run", status_code=422)
        allowed_subsets = selected_benchmark.subsets or ["all"]
        if "all" not in allowed_subsets and any(
            subset not in allowed_subsets for _device_id, subset in specs
        ):
            raise ApiError("E_SEMANTIC", "Run 目标 subset 不属于所选 Benchmark", status_code=422)
        benchmark = selected_benchmark
    locked_benchmark = db.scalar(
        select(Benchmark)
        .where(Benchmark.id == benchmark.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_benchmark is None:
        raise ApiError("E_NOT_FOUND", "Benchmark 不存在", status_code=404)
    benchmark = locked_benchmark
    benchmark_profile = db.get(MetricProfile, benchmark.metric_profile_id)
    if benchmark_profile is None:
        raise ApiError("E_NOT_FOUND", "Benchmark 口径不存在", status_code=404)
    db.refresh(benchmark_profile)

    # 提交事务前完成预览校验，避免无有效样本或部署已变化时留下 queued Run。
    initial_preview = preview_run(
        db,
        benchmark_id=benchmark.id,
        targets=specs,
        kind=kind,
        lanes=lanes,
        deployment_uids=uids,
    )
    if any(item["item_count"] == 0 for item in initial_preview["targets"]):
        raise ApiError("E_SEMANTIC", "至少一个目标子集在当前采样规则下没有样本", status_code=422)
    preview_by_target = {
        (item["device_id"], item["subset"]): item for item in initial_preview["targets"]
    }
    slowest = max(initial_preview["targets"], key=lambda item: item["eta"]["total_ms"])

    run = Run(
        benchmark_id=benchmark.id,
        group_id=group_id,
        name=name,
        kind=kind,
        status="queued",
        auto_score=auto_score,
        created_by=created_by,
        snapshot={
            "dataset_version_id": str(version.id),
            "benchmark_id": str(benchmark.id),
            "benchmark_version_hash": benchmark.version_hash,
            "benchmark_subsets": benchmark.subsets,
            "sampling": benchmark.sampling,
            "bucket_rules": benchmark.bucket_rules,
            "benchmark_params": benchmark.params,
            "metric_profile_id": str(benchmark_profile.id),
            "metric_profile_params": benchmark_profile.params,
            "targets": snapshot_targets,
            "profile_hash": benchmark_profile.profile_hash,
            "item_count": version.item_count,
            "kind": kind,
            "lanes": lanes,
            "auto_score": auto_score,
            "deterministic": deterministic,
        },
    )
    db.add(run)
    db.flush()
    for target_device_id, subset in specs:
        db.add(
            RunTarget(
                run_id=run.id,
                device_id=target_device_id,
                deployment_id=deployment_ids[f"{target_device_id}:{subset}"],
                subset=subset,
                status="pending",
                lanes=lanes if kind == "perf" else 1,
                stats={
                    "done": 0,
                    "total": preview_by_target[(str(target_device_id), subset)]["item_count"],
                    "ok": 0,
                    "failed": 0,
                    "eta": preview_by_target[(str(target_device_id), subset)]["eta"],
                },
            )
        )
    run.eta_ms = initial_preview["eta_ms"]
    run.eta_p90_ms = initial_preview["eta_p90_ms"]
    run.eta_source = slowest["eta"]["source"]
    run.eta_confidence = slowest["eta"]["confidence"]
    db.commit()

    db.refresh(run)
    return run


def preview_run(
    db: Session,
    *,
    benchmark_id: uuid.UUID,
    targets: list[tuple[uuid.UUID, str]],
    kind: str = "accuracy",
    lanes: int = 1,
    deployment_uids: dict[str, str] | None = None,
) -> dict[str, Any]:
    """创建前检查目标、指标、设备队列与 ETA（M4 §5.1）。"""
    from app.domain.benchmarks import sampled_item_ids
    from app.domain.metric_resolver import resolve_metrics

    if kind not in {"accuracy", "perf"}:
        raise ApiError("E_VALIDATION", "Run kind 只能是 accuracy 或 perf", status_code=422)
    if not 1 <= lanes <= 8 or (kind != "perf" and lanes != 1):
        raise ApiError("E_VALIDATION", "仅 perf Run 可使用 1–8 路并发", status_code=422)
    if not targets:
        raise ApiError("E_SEMANTIC", "至少需要一个目标设备", status_code=422)
    if len({(str(device_id), subset) for device_id, subset in targets}) != len(targets):
        raise ApiError("E_SEMANTIC", "目标设备与 subset 不能重复", status_code=422)

    benchmark = db.get(Benchmark, benchmark_id)
    if benchmark is None:
        raise ApiError("E_NOT_FOUND", "Benchmark 不存在", status_code=404)
    if benchmark.archived:
        raise ApiError("E_SEMANTIC", "已归档的 Benchmark 不能用于新 Run", status_code=422)
    version = db.get(DatasetVersion, benchmark.dataset_version_id)
    profile = db.get(MetricProfile, benchmark.metric_profile_id)
    if version is None or profile is None:
        raise ApiError("E_NOT_FOUND", "Benchmark 数据集版本或口径不存在", status_code=404)
    if version.status not in {"ready", "ready_with_errors"}:
        raise ApiError("E_SEMANTIC", "数据集版本未就绪", status_code=422)

    benchmark_subsets = [str(value) for value in (benchmark.subsets or [])] or ["all"]
    if "all" not in benchmark_subsets and any(
        subset not in benchmark_subsets for _device_id, subset in targets
    ):
        raise ApiError("E_SEMANTIC", "Run 目标 subset 不属于所选 Benchmark", status_code=422)

    selected_ids = sampled_item_ids(db, benchmark)
    selected_rows = (
        db.execute(
            select(Item.id, Item.duration_ms, DatasetSubset.name)
            .join(DatasetSubset, DatasetSubset.id == Item.subset_id)
            .where(Item.id.in_(selected_ids))
        ).all()
        if selected_ids
        else []
    )
    selected_by_subset: dict[str, list[int]] = {}
    for _item_id, duration_ms, subset_name in selected_rows:
        selected_by_subset.setdefault(str(subset_name), []).append(int(duration_ms))
    all_durations = [duration for values in selected_by_subset.values() for duration in values]

    target_previews: list[dict[str, Any]] = []
    maximum_eta_ms = 0
    maximum_eta_p90_ms = 0
    for device_id, subset_name in targets:
        device = db.get(Device, device_id)
        if device is None:
            raise ApiError("E_NOT_FOUND", f"设备 {device_id} 不存在", status_code=404)
        deployment = db.scalar(
            select(Deployment)
            .where(Deployment.device_id == device.id)
            .order_by(Deployment.captured_at.desc())
        )
        if deployment is None or not deployment.model_key:
            raise ApiError("E_SEMANTIC", f"设备 {device.name} 尚无有效部署快照", status_code=422)
        locked_uid = (deployment_uids or {}).get(f"{device_id}:{subset_name}") or (
            deployment_uids or {}
        ).get(str(device_id))
        if locked_uid is not None and locked_uid != deployment.uid:
            raise ApiError(
                "E_DEPLOYMENT_CHANGED",
                f"设备 {device.name} 部署已变化（锁定 {locked_uid}，当前 {deployment.uid}）",
                status_code=409,
            )
        if kind == "perf":
            limits = deployment.limits if isinstance(deployment.limits, dict) else {}
            slots = limits.get("realtime_slots")
            if isinstance(slots, bool) or not isinstance(slots, int):
                slots = 3 if device.kind == "gateway" else 1
            if lanes > slots:
                raise ApiError(
                    "E_SEMANTIC",
                    f"设备 {device.name} 当前最多支持 {slots} 路并发",
                    status_code=422,
                )

        subset = None
        if subset_name != "all":
            subset = db.scalar(
                select(DatasetSubset).where(
                    DatasetSubset.dataset_version_id == version.id,
                    DatasetSubset.name == subset_name,
                )
            )
            if subset is None:
                raise ApiError("E_NOT_FOUND", f"数据子集 {subset_name} 不存在", status_code=404)
        subset_durations = (
            all_durations if subset_name == "all" else selected_by_subset.get(subset_name, [])
        )
        capabilities = (subset.capabilities if subset else version.capabilities) or {}
        metric_rows = resolve_metrics(
            capabilities,
            deployment.capabilities if isinstance(deployment.capabilities, dict) else {},
            profile.params if isinstance(profile.params, dict) else {},
            benchmark.params if isinstance(benchmark.params, dict) else {},
        )
        from app.domain.eta import estimate_run

        perf_config = profile.params.get("perf", {}) if isinstance(profile.params, dict) else {}
        perf_config = perf_config if isinstance(perf_config, dict) else {}
        execution_mode = str(perf_config.get("mode", "batch")) if kind == "perf" else "burst"
        estimate_mode = execution_mode
        # 批量走 burst，固定 1 秒块且不节流；分块/速率只在流式下有意义。
        chunk_ms = (
            int(perf_config.get("chunk_ms", 1000))
            if kind == "perf" and execution_mode == "stream"
            else 1000
        )
        input_rate = (
            float(perf_config.get("stream_input_rate", 1.0))
            if kind == "perf" and execution_mode == "stream"
            else 1.0
        )
        estimate = estimate_run(
            db,
            deployment_id=deployment.id,
            model_key=deployment.model_key,
            kind=device.kind,
            durations_ms=subset_durations,
            lanes=lanes if kind == "perf" else 1,
            params_hash=profile.profile_hash,
            mode=estimate_mode,
            chunk_ms=chunk_ms,
            input_rate=input_rate,
        )
        active_lease = db.scalar(
            select(DeviceLease).where(
                DeviceLease.device_id == device.id,
                DeviceLease.expires_at > datetime.now(UTC),
            )
        )
        queued_runs = db.execute(
            select(Run.id, Run.eta_ms, Run.created_at)
            .join(RunTarget, RunTarget.run_id == Run.id)
            .where(RunTarget.device_id == device.id, Run.status == "queued")
            .distinct()
            .order_by(Run.created_at, Run.id)
        ).all()
        queue_ahead = len(queued_runs) + (1 if active_lease else 0)
        queue_wait_ms = sum(max(0, int(eta or 0)) for _run_id, eta, _created_at in queued_runs)
        if active_lease:
            active_run = db.get(RunTarget, active_lease.run_target_id)
            active_run_record = db.get(Run, active_run.run_id) if active_run else None
            queue_wait_ms += max(0, int(active_run_record.eta_ms or 0)) if active_run_record else 0
        own_eta_ms = int(estimate["eta_ms"])
        own_eta_p90_ms = int(estimate["eta_p90_ms"])
        total_eta_ms = own_eta_ms + queue_wait_ms
        total_eta_p90_ms = own_eta_p90_ms + queue_wait_ms
        maximum_eta_ms = max(maximum_eta_ms, total_eta_ms)
        maximum_eta_p90_ms = max(maximum_eta_p90_ms, total_eta_p90_ms)

        warnings: list[dict[str, str]] = []
        if device.status != "online":
            warnings.append({"code": "device_not_online", "message": f"设备状态：{device.status}"})
        if active_lease:
            warnings.append({"code": "device_busy", "message": "设备当前被其他 Run 占用"})
        for metric in metric_rows:
            status = str(metric.get("status"))
            if status in {"na", "partial"}:
                warnings.append(
                    {
                        "code": f"metric_{status}",
                        "message": f"{metric['name']}：{metric.get('reason') or status}",
                    }
                )
        target_previews.append(
            {
                "device_id": str(device.id),
                "device_name": device.name,
                "device_status": device.status,
                "deployment_uid": deployment.uid,
                "subset": subset_name,
                "item_count": len(subset_durations),
                "total_duration_ms": sum(subset_durations),
                "metrics": metric_rows,
                "eta": {
                    **estimate,
                    "execution_ms": own_eta_ms,
                    "queue_wait_ms": queue_wait_ms,
                    "total_ms": total_eta_ms,
                    "p90_total_ms": total_eta_p90_ms,
                },
                "queue": {
                    "busy": active_lease is not None,
                    "queued_ahead": len(queued_runs),
                    "position": queue_ahead + 1,
                },
                "warnings": warnings,
            }
        )
    slowest_target: dict[str, Any] = max(
        target_previews, key=lambda item: item["eta"]["total_ms"], default={"eta": {}}
    )
    return {
        "benchmark_id": str(benchmark.id),
        "kind": kind,
        "lanes": lanes,
        "targets": target_previews,
        "eta_ms": maximum_eta_ms,
        "eta_p90_ms": maximum_eta_p90_ms,
        "eta_source": slowest_target["eta"].get("source"),
        "eta_confidence": slowest_target["eta"].get("confidence"),
        "warnings": [warning for target in target_previews for warning in target["warnings"]],
    }


async def execute_run(
    db: Session,
    run_id: uuid.UUID,
    limit: int | None = None,
    *,
    _target_id: uuid.UUID | None = None,
    _abort_event: asyncio.Event | None = None,
    _stop_state: dict[str, Any] | None = None,
) -> Run:
    """并行调度不同设备的 target，结束后按 auto_score 评分。

    - 取消：每条样本前检查 run.status，"cancelling" → 停止派发（M4 §5.9）；
    - 续跑：failed/cancelled 重入，跳过已有 ok 结果的样本（§5.9 只补 failed/timeout/pending）；
    - 同一设备由租约串行化；每条样本落 server_ms/rtf_dev，并滚动修正 ETA。

    `_target_id` 等参数只供本函数创建的并发 target 会话使用。
    """
    run = db.get(Run, run_id)
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    if _target_id is None and run.status != "queued":
        raise ApiError("E_CONFLICT", f"Run 当前状态为 {run.status}，不能重复执行", status_code=409)
    if _target_id is not None and run.status not in {"running", "cancelling"}:
        raise ApiError(
            "E_CONFLICT", f"Run 当前状态为 {run.status}，不能派发 target", status_code=409
        )
    benchmark = db.get(Benchmark, run.benchmark_id)
    if benchmark is None:
        raise ApiError("E_SEMANTIC", "Run 缺少 benchmark", status_code=422)
    run_profile = db.get(MetricProfile, benchmark.metric_profile_id)
    run_profile_params = run_profile.params if run_profile is not None else {}
    der_config = run_profile_params.get("der") if isinstance(run_profile_params, dict) else None
    naming_config = (
        run_profile_params.get("naming") if isinstance(run_profile_params, dict) else None
    )
    diarization_enabled = not (
        isinstance(der_config, dict)
        and der_config.get("enabled") is False
        and isinstance(naming_config, dict)
        and naming_config.get("enabled") is False
    )
    perf_config = run_profile_params.get("perf") if isinstance(run_profile_params, dict) else None
    perf_config = perf_config if isinstance(perf_config, dict) else {}
    profile_perf_mode = perf_config.get("mode", "batch")
    execution_mode = str(profile_perf_mode) if run.kind == "perf" else "burst"
    transcribe_mode = "stream" if execution_mode == "stream" else "burst"
    # burst 固定 1 秒块；口径里的 chunk_ms/input_rate 只对 stream 生效。
    chunk_ms = (
        int(perf_config.get("chunk_ms", 1000))
        if run.kind == "perf" and execution_mode == "stream"
        else 1000
    )
    input_rate = (
        float(perf_config.get("stream_input_rate", 1.0))
        if run.kind == "perf" and execution_mode == "stream"
        else 1.0
    )
    all_targets = db.scalars(
        select(RunTarget).where(RunTarget.run_id == run.id).order_by(RunTarget.id)
    ).all()
    if not all_targets:
        raise ApiError("E_SEMANTIC", "Run 缺少 target", status_code=422)
    targets = (
        [target for target in all_targets if target.id == _target_id]
        if _target_id is not None
        else all_targets
    )
    if _target_id is not None and not targets:
        raise ApiError("E_NOT_FOUND", "Run target 不存在", status_code=404)

    selected_ids = sampled_item_ids(db, benchmark, limit)
    if not selected_ids:
        raise ApiError("E_SEMANTIC", "采样结果为空", status_code=422)
    selected_item_rows = db.execute(
        select(Item.id, DatasetSubset.name)
        .join(DatasetSubset, DatasetSubset.id == Item.subset_id)
        .where(Item.id.in_(selected_ids))
    ).all()
    selected_ids_by_subset: dict[str, list[int]] = {}
    for item_id, subset_name in selected_item_rows:
        selected_ids_by_subset.setdefault(str(subset_name), []).append(int(item_id))
    target_item_ids = {
        target.id: (
            selected_ids
            if target.subset == "all"
            else selected_ids_by_subset.get(target.subset, [])
        )
        for target in all_targets
    }
    total_all = sum(len(target_item_ids[target.id]) for target in all_targets)

    if _target_id is None:
        claimed = db.scalar(
            update(Run)
            .where(Run.id == run_id, Run.status == "queued")
            .values(
                status="running",
                started_at=func.coalesce(Run.started_at, datetime.now(UTC)),
                version=Run.version + 1,
            )
            .returning(Run.id)
        )
        if claimed is None:
            db.refresh(run)
            raise ApiError(
                "E_CONFLICT", f"Run 当前状态为 {run.status}，不能重复执行", status_code=409
            )
        db.commit()
        db.refresh(run)

        factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
        abort_event = asyncio.Event()
        stop_state: dict[str, Any] = {"reason": None}
        cancel_watcher = asyncio.create_task(
            _watch_run_cancellation(factory, run_id, abort_event, stop_state)
        )

        async def dispatch_target(target_id: uuid.UUID) -> None:
            """每个 target 使用独立 Session，避免并发设备共享事务。"""
            with factory() as target_db:
                try:
                    await execute_run(
                        target_db,
                        run_id,
                        limit,
                        _target_id=target_id,
                        _abort_event=abort_event,
                        _stop_state=stop_state,
                    )
                except ApiError as exc:
                    if exc.code == "E_CONFLICT":
                        return
                    _mark_target_failed(target_db, target_id, exc.code)
                except Exception as exc:
                    target_db.rollback()
                    logger.exception("Run target 执行异常：%s", target_id)
                    _mark_target_failed(target_db, target_id, type(exc).__name__)

        try:
            dispatchable = [
                target
                for target in all_targets
                if target.status != "completed" and target.status != "aborted_condition_changed"
            ]
            target_ids_by_device: dict[uuid.UUID, list[uuid.UUID]] = {}
            for target in dispatchable:
                target_ids_by_device.setdefault(target.device_id, []).append(target.id)

            async def dispatch_device(target_ids: list[uuid.UUID]) -> None:
                """同设备 target 串行派发；不同设备仍并行执行（M4 §5.4）。"""
                for target_id in target_ids:
                    await dispatch_target(target_id)

            await asyncio.gather(
                *(dispatch_device(target_ids) for target_ids in target_ids_by_device.values())
            )
        finally:
            cancel_watcher.cancel()
            await asyncio.gather(cancel_watcher, return_exceptions=True)

        db.expire_all()
        run = db.get(Run, run_id)
        if run is None:
            raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
        final_targets = db.scalars(select(RunTarget).where(RunTarget.run_id == run_id)).all()
        pending_count = sum(target.status in {"pending", "running"} for target in final_targets)
        condition_changed = stop_state.get("reason") == "condition_changed" or any(
            target.status == "aborted_condition_changed" for target in final_targets
        )
        counts = db.execute(
            select(CaseResult.status, func.count(CaseResult.id))
            .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
            .where(RunTarget.run_id == run_id)
            .group_by(CaseResult.status)
        ).all()
        case_counts = {str(status): int(count) for status, count in counts}
        ok_total = case_counts.get("ok", 0) + case_counts.get("cached", 0)
        failed_total = sum(
            count
            for status, count in case_counts.items()
            if status in {"failed", "timeout", "aborted"}
        )
        done_total = sum(case_counts.values())

        if condition_changed:
            run.status = "aborted_condition_changed"
        elif stop_state.get("reason") == "cancelled" or run.status == "cancelling":
            run.status = "cancelled"
        elif pending_count:
            run.status = "queued"
            run.finished_at = None
        elif ok_total + failed_total == 0:
            run.status = "failed"
        elif failed_total == 0:
            run.status = "completed"
        elif ok_total / (ok_total + failed_total) >= 0.9:
            run.status = "partial"
        else:
            run.status = "failed"
        run.progress = {"done": done_total, "total": total_all}
        if stop_state.get("detail") is not None:
            run.progress = {**run.progress, "condition_change": stop_state["detail"]}
        run.finished_at = None if run.status == "queued" else datetime.now(UTC)
        db.commit()

        if run.status in {"completed", "partial"} and run.auto_score:
            evaluation = create_evaluation_record(
                db, run.id, benchmark.metric_profile_id, trigger="auto"
            )
            from app.domain.evaluation_worker import get_evaluation_worker

            get_evaluation_worker().enqueue(
                evaluation.id,
                sessionmaker(bind=db.get_bind(), expire_on_commit=False),
            )
        db.refresh(run)
        return run

    def _cancelled() -> bool:
        """每条样本前查取消标记；取消 30s 内生效（M4 §5.9）。"""
        db.expire(run)
        fresh = db.get(Run, run_id)
        return fresh is not None and fresh.status == "cancelling"

    ok_total = 0
    failed_total = 0
    done_total = 0
    pending_targets = 0
    cancelled = False
    condition_changed = False
    live_progress: dict[int, dict[str, Any]] = {}
    last_live_flush = 0.0
    for target in targets:
        if _abort_event is not None and _abort_event.is_set():
            stop_reason = _stop_state.get("reason") if _stop_state is not None else None
            target.status = (
                "aborted_condition_changed"
                if stop_reason == "condition_changed"
                else ("cancelled" if stop_reason == "cancelled" else "pending")
            )
            target.abort_reason = stop_reason
            target.finished_at = datetime.now(UTC) if target.status != "pending" else None
            db.commit()
            break
        if cancelled or _cancelled():
            cancelled = True
            target.status = "cancelled"
            target.abort_reason = "cancelled"
            target.finished_at = datetime.now(UTC)
            db.commit()
            break
        selected_for_target = target_item_ids[target.id]
        if not selected_for_target:
            target.status = "failed"
            target.stats = {**(target.stats or {}), "error": "采样后目标子集没有样本"}
            target.finished_at = datetime.now(UTC)
            db.commit()
            failed_total += 1
            continue
        device = db.get(Device, target.device_id)
        deployment = db.get(Deployment, target.deployment_id) if target.deployment_id else None
        if deployment is None and target.deployment_id is None:
            deployment = db.scalar(
                select(Deployment)
                .where(Deployment.device_id == target.device_id)
                .order_by(Deployment.captured_at.desc())
            )
        if device is None or deployment is None or not deployment.model_key:
            # 缺快照按 target 失败处理，不再让整场 Run 卡死
            target.status = "failed"
            target.stats = {"error": "device or deployment snapshot missing"}
            db.commit()
            failed_total += 1
            continue

        try:
            _acquire_lease(db, device.id, target.id, str(run.id))
        except ApiError as exc:
            if exc.code == "E_DEVICE_BUSY":
                # 设备被占用 → target 保持 pending 排队（M4 §5.4），继续其他目标
                pending_targets += 1
                continue
            raise
        target.status = "running"
        target.started_at = datetime.now(UTC)
        db.commit()
        snapshot_target = _run_target_snapshot(run, target, device)
        expected_uid = str(snapshot_target.get("deployment_uid") or deployment.uid)
        expected_sn = snapshot_target.get("sn", device.sn)
        identity_change: dict[str, Any] | None = None
        abort_event = _abort_event or asyncio.Event()
        watchdog_changes: dict[str, Any] = {}
        watchdog_task: asyncio.Task[None] | None = None
        resource_stop = asyncio.Event()
        resource_task: asyncio.Task[None] | None = None
        try:
            probe_version = (deployment.probe_meta or {}).get("probe_version")
            if device.kind in {"etsstt", "gateway"} and probe_version:
                identity_probe, current_deployment = await run_probe(db, device, mode="identity")
                identity_result = identity_probe.result or {}
                current_sn = identity_result.get("sn")
                if (
                    identity_probe.status != "ok"
                    or current_deployment is None
                    or identity_result.get("version_collection_ok") is not True
                ):
                    target.status = "failed"
                    target.abort_reason = "identity_check_failed"
                    target.stats = {
                        "error": identity_probe.error or "设备身份检查未能完整采集",
                        "identity_check": identity_result,
                    }
                    target.finished_at = datetime.now(UTC)
                    db.commit()
                    failed_total += 1
                    continue
                if current_deployment.uid != expected_uid or current_sn != expected_sn:
                    identity_change = _identity_change_detail(
                        run,
                        target,
                        device,
                        expected_uid=expected_uid,
                        expected_sn=expected_sn,
                        new_uid=current_deployment.uid,
                        new_sn=current_sn,
                        new_model_key=current_deployment.model_key,
                    )
                    _abort_target_for_condition_change(db, run, target, identity_change)
                    _publish_condition_change(run, target, device, identity_change)
                    if _stop_state is not None:
                        _stop_state["reason"] = "condition_changed"
                        _stop_state["detail"] = identity_change
                    abort_event.set()
                    condition_changed = True
                    break

            if device.kind in {"etsstt", "gateway"}:
                watchdog_task = asyncio.create_task(
                    _watch_target_identity(
                        sessionmaker(bind=db.get_bind(), expire_on_commit=False),
                        run_id=run.id,
                        target_id=target.id,
                        device_id=device.id,
                        expected_uid=expected_uid,
                        expected_sn=expected_sn,
                        abort_event=abort_event,
                        change_holder=watchdog_changes,
                        stop_state=_stop_state,
                    )
                )
            if device.ssh_user:
                try:
                    resource_credential = credential_for_device(db, device)
                except CredentialUnavailable:
                    resource_credential = None
                resource_task = asyncio.create_task(
                    sample_resources_loop(
                        sessionmaker(bind=db.get_bind(), expire_on_commit=False),
                        run_id=run.id,
                        target_id=target.id,
                        host=device.host,
                        ssh_port=device.ssh_port,
                        ssh_user=device.ssh_user,
                        credential=resource_credential,
                        stop_event=resource_stop,
                    )
                )
            # 续跑：跳过该 target 已有 ok 结果的样本（M4 §5.9 只补 failed/timeout/pending）
            done_ok = set(
                db.scalars(
                    select(CaseResult.item_id).where(
                        CaseResult.run_target_id == target.id,
                        CaseResult.status.in_(["ok", "cached"]),
                    )
                ).all()
            )
            rows = db.execute(
                select(Item, AudioAsset, RefText, DatasetSubset.audio_profile)
                .join(AudioAsset, AudioAsset.id == Item.audio_asset_id)
                .join(DatasetSubset, DatasetSubset.id == Item.subset_id)
                .outerjoin(
                    RefText,
                    (RefText.item_id == Item.id) & (RefText.kind == "verbatim"),
                )
                .where(Item.id.in_(selected_for_target))
                .order_by(Item.id)
            ).all()
            planned_count = len(rows)
            rows = [row for row in rows if row[0].id not in done_ok]

            # 校准切片按时长桶最多取 3 条、总计不超过 12 条，并先于剩余样本执行（M4 ETA §7）。
            calibration_by_bucket: dict[str, list[Any]] = {bucket: [] for bucket in BUCKETS}
            for row in rows:
                calibration_by_bucket[duration_bucket(int(row[0].duration_ms))].append(row)
            calibration_rows: list[Any] = []
            for bucket in BUCKETS:
                remaining_slots = 12 - len(calibration_rows)
                if remaining_slots <= 0:
                    break
                calibration_rows.extend(calibration_by_bucket[bucket][: min(3, remaining_slots)])
            calibration_item_ids = {row[0].id for row in calibration_rows}
            rows = calibration_rows + [row for row in rows if row[0].id not in calibration_item_ids]
            calibration_count = len(calibration_rows)

            target_started = time.monotonic()
            target_ok = 0
            target_failed = 0
            consecutive_failed = 0
            aborted_reason: str | None = None
            last_beat = time.monotonic()
            # ETA 滚动修正（M4 §6.2）：已完成 (bucket, wall/duration)，用时剔除 warm-up 前 3 条
            observed_ratios: list[tuple[str, float]] = []
            previous_window_rtf: float | None = None
            consecutive_slow_windows = 0
            speed_anomaly = False
            last_eta_update = target_started
            pending_durations = [
                int(item.duration_ms) for item, _asset, _ref, _audio_profile in rows
            ]
            live_progress.clear()
            last_live_flush = 0.0

            def _clear_live(target: RunTarget = target, run: Run = run) -> None:
                """样本结束后移除单条实时进度，避免残留成假进度。"""
                stats = dict(target.stats or {})
                stats.pop("live", None)
                target.stats = stats
                progress = dict(run.progress or {})
                progress.pop("live", None)
                run.progress = progress

            def _flush_live(
                force: bool = False,
                *,
                target: RunTarget = target,
                run: Run = run,
            ) -> None:
                """把单条音频的内部进度节流写入 target/run，供前端实时展示。"""
                nonlocal last_live_flush
                now = time.monotonic()
                if not force and now - last_live_flush < 2:
                    return
                last_live_flush = now
                active = list(live_progress.values())
                if not active:
                    _clear_live()
                else:
                    processed_ms = sum(int(entry.get("processed_ms", 0)) for entry in active)
                    audio_ms = sum(int(entry.get("audio_ms", 0)) for entry in active)
                    live = {
                        "processed_ms": processed_ms,
                        "audio_ms": audio_ms,
                        "percent": (processed_ms / audio_ms) if audio_ms else 0.0,
                        "items": active[:8],
                    }
                    target.stats = {**(target.stats or {}), "live": live}
                    run.progress = {**(run.progress or {}), "live": live}
                db.commit()

            def _make_progress(item: Item, duration_ms: int) -> Callable[[dict[str, Any]], None]:
                """单条音频的进度回调：partial/segment/final 都映射到已处理音频位置。"""

                def callback(
                    payload: dict[str, Any],
                    *,
                    item: Item = item,
                    duration_ms: int = duration_ms,
                ) -> None:
                    if payload.get("kind") == "partial":
                        # partial 不代表完整 segment 已产出，不能推进转写进度。
                        return
                    if payload.get("kind") == "final":
                        processed = duration_ms
                    else:
                        processed = int(payload.get("processed_ms") or 0)
                    if duration_ms > 0:
                        processed = max(0, min(duration_ms, processed))
                    else:
                        processed = max(0, processed)
                    previous = int(live_progress.get(int(item.id), {}).get("processed_ms", 0))
                    processed = max(previous, processed)
                    live_progress[int(item.id)] = {
                        "item_id": int(item.id),
                        "item_key": item.item_key,
                        "audio_ms": duration_ms,
                        "processed_ms": processed,
                        "percent": (processed / duration_ms) if duration_ms > 0 else 0.0,
                        "kind": str(payload.get("kind") or ""),
                        "partials": int(payload.get("partials") or 0),
                        "segments": int(payload.get("segments") or 0),
                    }
                    _flush_live()

                return callback

            lane_count = max(1, target.lanes if run.kind == "perf" else 1)
            batch_start = 0
            while batch_start < len(rows):
                await asyncio.sleep(0)
                if watchdog_changes:
                    identity_change = watchdog_changes["detail"]
                    if _stop_state is not None:
                        _stop_state["reason"] = "condition_changed"
                        _stop_state["detail"] = identity_change
                    aborted_reason = "condition_changed"
                    break
                if cancelled or _cancelled():
                    cancelled = True
                    if _stop_state is not None:
                        _stop_state["reason"] = "cancelled"
                    abort_event.set()
                    aborted_reason = "cancelled"
                    break
                if time.monotonic() - last_beat > 30:
                    _renew_lease(db, device.id, str(run.id))
                    last_beat = time.monotonic()
                batch_end = min(batch_start + lane_count, len(rows))
                if batch_start < calibration_count < batch_end:
                    batch_end = calibration_count
                batch = rows[batch_start:batch_end]
                prepared: list[dict[str, Any]] = []
                transcribe_tasks: list[Any] = []
                params_hash = str(run.snapshot.get("profile_hash", ""))
                deterministic = run.snapshot.get("deterministic") is True
                cache_scope = "shared" if deterministic else str(run.id)
                for batch_offset, (item, asset, _ref, audio_profile) in enumerate(batch):
                    index = batch_start + batch_offset + 1
                    cache_key = hashlib.sha256(
                        f"{cache_scope}|{deployment.uid}|{asset.sha256}|{deployment.model_key}|{params_hash}|{audio_profile}|{execution_mode}|{chunk_ms}|{input_rate}|{target.lanes}".encode()
                    ).hexdigest()[:32]
                    cache_query = select(Hypothesis).where(Hypothesis.cache_key == cache_key)
                    if not deterministic:
                        cache_query = (
                            cache_query.join(CaseResult, CaseResult.hypothesis_id == Hypothesis.id)
                            .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
                            .where(RunTarget.run_id == run.id)
                        )
                    cached = db.scalar(cache_query.limit(1))
                    if cached is not None:
                        outcome = TranscribeOutcome(ok=True, text=cached.text or "", wall_ms=0)
                        task_index = None
                    else:
                        audio_path = resolve_audio_path(db, asset, audio_profile)
                        task_index = len(transcribe_tasks)
                        transcribe_tasks.append(
                            _transcribe_with_retry(
                                device,
                                deployment,
                                str(audio_path),
                                diarization=diarization_enabled,
                                abort_event=abort_event,
                                mode=transcribe_mode,
                                chunk_ms=chunk_ms,
                                input_rate=input_rate,
                                on_progress=_make_progress(item, int(item.duration_ms)),
                            )
                        )
                        outcome = TranscribeOutcome(ok=False)
                    prepared.append(
                        {
                            "index": index,
                            "item": item,
                            "cached": cached,
                            "outcome": outcome,
                            "task_index": task_index,
                            "cache_key": cache_key,
                        }
                    )

                outcomes = await asyncio.gather(*transcribe_tasks)
                for sample in prepared:
                    live_progress.pop(int(sample["item"].id), None)
                _flush_live(force=True)
                should_stop = False
                if watchdog_changes:
                    identity_change = watchdog_changes["detail"]
                    if _stop_state is not None:
                        _stop_state["reason"] = "condition_changed"
                        _stop_state["detail"] = identity_change
                    aborted_reason = "condition_changed"
                    should_stop = True
                for sample in prepared:
                    if aborted_reason == "condition_changed":
                        break
                    item = sample["item"]
                    cached = sample["cached"]
                    task_index = sample["task_index"]
                    outcome = sample["outcome"] if task_index is None else outcomes[task_index]
                    if outcome.error == "aborted_condition_changed":
                        stop_reason = _stop_state.get("reason") if _stop_state is not None else None
                        if stop_reason == "condition_changed" or watchdog_changes:
                            identity_change = watchdog_changes.get("detail")
                            aborted_reason = "condition_changed"
                        else:
                            cancelled = stop_reason == "cancelled" or _cancelled()
                            aborted_reason = "cancelled" if cancelled else "run_stopped"
                        should_stop = True
                        continue
                    error = outcome.error or ""
                    if not outcome.ok and "overload" in error.lower():
                        # engine_overloaded：立即中断该 target，设备标 needs_restart（M4 §5.6）
                        device.status = "needs_restart"
                        device.last_error = error[:1024]
                        aborted_reason = "engine_overloaded"
                        should_stop = True
                        continue
                    server_ms: int | None = None
                    if isinstance(outcome.usage, dict):
                        raw_server = outcome.usage.get("inference_ms")
                        if isinstance(raw_server, (int, float)):
                            server_ms = int(raw_server)
                    duration = int(item.duration_ms)
                    rtf_dev = (
                        server_ms / duration if server_ms is not None and duration > 0 else None
                    )
                    rtf_e2e = (
                        outcome.wall_ms / duration if outcome.wall_ms and duration > 0 else None
                    )
                    hypothesis_id: uuid.UUID | None = None
                    if cached is not None:
                        hypothesis_id = cached.id
                    elif outcome.ok:
                        hypothesis = Hypothesis(
                            cache_key=sample["cache_key"],
                            deployment_id=deployment.id,
                            item_id=item.id,
                            params_hash=params_hash[:16],
                            mode=execution_mode,
                            chunk_ms=chunk_ms,
                            input_rate=input_rate,
                            concurrency=target.lanes,
                            text=outcome.text,
                            segments={"items": outcome.segments} if outcome.segments else None,
                            usage=dict(outcome.usage) if isinstance(outcome.usage, dict) else {},
                            speaker_match=outcome.speaker_match or None,
                        )
                        db.add(hypothesis)
                        db.flush()
                        hypothesis_id = hypothesis.id
                    previous_case = db.scalar(
                        select(CaseResult).where(
                            CaseResult.run_target_id == target.id,
                            CaseResult.item_id == item.id,
                        )
                    )
                    if previous_case is not None:
                        db.delete(previous_case)
                        db.flush()
                    db.add(
                        CaseResult(
                            run_target_id=target.id,
                            item_id=item.id,
                            status="cached"
                            if cached is not None
                            else ("ok" if outcome.ok else "failed"),
                            text=(cached.text if cached is not None else outcome.text) or None,
                            wall_ms=None if cached is not None else outcome.wall_ms,
                            server_ms=server_ms,
                            rtf_dev=rtf_dev,
                            hypothesis_id=hypothesis_id,
                            dev_ms=server_ms,
                            ttft_ms=outcome.ttft_ms,
                            finalize_ms=outcome.finalize_ms,
                            rtf_e2e=None if cached is not None else rtf_e2e,
                            resource=dict(outcome.usage)
                            if isinstance(outcome.usage, dict)
                            else None,
                            error=outcome.error,
                            created_at=datetime.now(UTC),
                        )
                    )
                    done_total += 1
                    if outcome.ok:
                        target_ok += 1
                        consecutive_failed = 0
                        if outcome.wall_ms and duration > 0:
                            observed_ratios.append(
                                (duration_bucket(duration), outcome.wall_ms / duration)
                            )
                    else:
                        target_failed += 1
                        consecutive_failed += 1
                        if consecutive_failed >= BREAKER_THRESHOLD:
                            aborted_reason = "consecutive_failures"
                            should_stop = True
                    index = int(sample["index"])
                    if index % 10 == 0 and observed_ratios:
                        current_window = statistics.median(
                            ratio for _bucket, ratio in observed_ratios[-10:]
                        )
                        if previous_window_rtf is not None and speed_degraded(
                            current_window, previous_window_rtf
                        ):
                            consecutive_slow_windows += 1
                        else:
                            consecutive_slow_windows = 0
                        previous_window_rtf = current_window
                        if consecutive_slow_windows >= 2:
                            speed_anomaly = True
                    should_update_eta = (
                        index == calibration_count
                        or index % 10 == 0
                        or index == len(rows)
                        or time.monotonic() - last_eta_update >= 5
                    )
                    if should_update_eta:
                        target.stats = {
                            **(target.stats or {}),
                            "done": target_ok + target_failed,
                            "total": planned_count,
                            "ok": target_ok,
                            "failed": target_failed,
                            "calibration": {
                                "planned": calibration_count,
                                "complete": index >= calibration_count,
                            },
                            "speed_anomaly": speed_anomaly,
                        }
                        done_total = int(
                            db.scalar(
                                select(func.count(CaseResult.id))
                                .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
                                .where(RunTarget.run_id == run.id)
                            )
                            or 0
                        )
                        db.refresh(run)
                        run.progress = {
                            **(run.progress or {}),
                            "done": done_total,
                            "total": total_all,
                            **(
                                {
                                    "speed_anomaly": True,
                                    "warning": "速度异常：连续两个窗口下降超过 30%",
                                }
                                if speed_anomaly
                                else {}
                            ),
                        }
                        _apply_running_eta(
                            db,
                            run,
                            target=target,
                            elapsed_ms=int((time.monotonic() - target_started) * 1000),
                            observed=observed_ratios[WARMUP_SKIP:],
                            remaining=pending_durations[index:],
                            kind=device.kind,
                            lanes=target.lanes if run.kind == "perf" else 1,
                            mode=execution_mode,
                            input_rate=input_rate,
                            force_p90=speed_anomaly,
                        )
                        db.commit()
                        last_eta_update = time.monotonic()
                if should_stop:
                    break
                batch_start = batch_end

            previous_stats = target.stats if isinstance(target.stats, dict) else {}
            _clear_live()
            previous_eta = previous_stats.get("eta")
            eta_fields: dict[str, Any] = previous_eta if isinstance(previous_eta, dict) else {}
            target.stats = {
                **(target.stats or {}),
                "done": target_ok + target_failed,
                "total": planned_count,
                "ok": target_ok,
                "failed": target_failed,
                "calibration": {
                    "planned": calibration_count,
                    "complete": len(rows) >= calibration_count,
                },
                "speed_anomaly": speed_anomaly,
                "eta": {
                    **eta_fields,
                    "total_ms": 0,
                    "p90_total_ms": 0,
                    "queue_wait_ms": 0,
                    "execution_ms": 0,
                },
                **({"aborted": aborted_reason} if aborted_reason else {}),
                **({"condition_change": identity_change} if identity_change else {}),
            }
            target.finished_at = datetime.now(UTC)
            target.abort_reason = aborted_reason
            target.status = (
                "aborted_condition_changed"
                if aborted_reason == "condition_changed"
                else (
                    "cancelled"
                    if aborted_reason == "cancelled"
                    else (
                        "failed"
                        if aborted_reason
                        else ("completed" if target_failed == 0 else "partial")
                    )
                )
            )
            ok_total += target_ok
            failed_total += target_failed
            done_total = int(
                db.scalar(
                    select(func.count(CaseResult.id))
                    .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
                    .where(RunTarget.run_id == run.id)
                )
                or 0
            )
            db.refresh(run)
            run.progress = {
                **{key: value for key, value in (run.progress or {}).items() if key != "live"},
                "done": done_total,
                "total": total_all,
                **(
                    {"speed_anomaly": True, "warning": "速度异常：连续两个窗口下降超过 30%"}
                    if speed_anomaly
                    else {}
                ),
            }
            _refresh_run_eta_from_targets(db, run)
            if identity_change is not None:
                run.progress = {**run.progress, "condition_change": identity_change}
            db.commit()
        finally:
            resource_stop.set()
            await stop_sampler(resource_task)
            if watchdog_task is not None:
                watchdog_task.cancel()
                await asyncio.gather(watchdog_task, return_exceptions=True)
            _release_lease(db, device.id, str(run.id))
        if aborted_reason == "condition_changed":
            condition_changed = True
            break

    if _target_id is not None:
        return run

    if condition_changed:
        run.status = "aborted_condition_changed"
    elif cancelled:
        run.status = "cancelled"
    elif pending_targets:
        # 有 target 正被其他任务占用；保留已完成结果，队列稍后只补剩余 target。
        run.status = "queued"
        run.finished_at = None
    elif ok_total + failed_total == 0:
        run.status = "failed"
    elif failed_total == 0 and pending_targets == 0:
        run.status = "completed"
    elif ok_total / (ok_total + failed_total) >= 0.9:
        run.status = "partial"
    else:
        run.status = "failed"
    run.finished_at = datetime.now(UTC)
    db.commit()

    # 只对完成执行的 Run 自动评分；failed/被取消不评，auto_score 可关（M4 §5.10）
    if run.status in {"completed", "partial"} and run.auto_score:
        evaluation = create_evaluation_record(
            db, run.id, benchmark.metric_profile_id, trigger="auto"
        )
        from app.domain.evaluation_worker import get_evaluation_worker

        get_evaluation_worker().enqueue(
            evaluation.id,
            sessionmaker(bind=db.get_bind(), expire_on_commit=False),
        )
    db.refresh(run)
    return run


async def _transcribe_with_retry(
    device: Device,
    deployment: Deployment,
    audio_uri: str,
    *,
    diarization: bool = False,
    abort_event: asyncio.Event | None = None,
    mode: str = "burst",
    chunk_ms: int = 1000,
    input_rate: float = 1.0,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> TranscribeOutcome:
    """单条转写：连接错误/超时重试 2 次（1s/3s 退避），其余失败不重试（M4 §5.6）。"""
    model_key = deployment.model_key or ""
    asr_host, asr_port = device_endpoint(device)
    outcome = await transcribe_file(
        asr_host,
        asr_port,
        model_key,
        audio_uri,
        diarization=diarization,
        abort_event=abort_event,
        mode=mode,
        chunk_ms=chunk_ms,
        input_rate=input_rate,
        on_progress=on_progress,
    )
    attempt = 0
    while (
        not outcome.ok
        and attempt < len(RETRY_DELAYS_S)
        and (outcome.error or "").startswith(_RETRYABLE_PREFIXES)
    ):
        await asyncio.sleep(RETRY_DELAYS_S[attempt])
        if abort_event is not None and abort_event.is_set():
            return TranscribeOutcome(ok=False, error="aborted_condition_changed")
        attempt += 1
        outcome = await transcribe_file(
            asr_host,
            asr_port,
            model_key,
            audio_uri,
            diarization=diarization,
            abort_event=abort_event,
            mode=mode,
            chunk_ms=chunk_ms,
            input_rate=input_rate,
            on_progress=on_progress,
        )
    return outcome


def _run_target_snapshot(run: Run, target: RunTarget, device: Device) -> dict[str, Any]:
    """按设备与 subset 取创建 Run 时冻结的目标身份。"""
    snapshot = run.snapshot if isinstance(run.snapshot, dict) else {}
    targets = snapshot.get("targets")
    if not isinstance(targets, list):
        return {"sn": device.sn}
    for entry in targets:
        if (
            isinstance(entry, dict)
            and entry.get("device_id") == str(device.id)
            and entry.get("subset") == target.subset
        ):
            return entry
    return {"sn": device.sn}


def _identity_change_detail(
    run: Run,
    target: RunTarget,
    device: Device,
    *,
    expected_uid: str,
    expected_sn: Any,
    new_uid: str,
    new_sn: Any,
    new_model_key: str | None,
) -> dict[str, Any]:
    """生成可供事件和界面展示的新旧设备身份。"""
    snapshot_target = _run_target_snapshot(run, target, device)
    return {
        "reason": "condition_changed",
        "old": {
            "sn": expected_sn,
            "deployment_uid": expected_uid,
            "model_key": snapshot_target.get("model_key"),
        },
        "new": {"sn": new_sn, "deployment_uid": new_uid, "model_key": new_model_key},
    }


def _abort_target_for_condition_change(
    db: Session, run: Run, target: RunTarget, detail: dict[str, Any]
) -> None:
    """在处理首条样本前发现条件变化时立即终止 target。"""
    target.status = "aborted_condition_changed"
    target.abort_reason = "condition_changed"
    target.finished_at = datetime.now(UTC)
    target.stats = {"done": 0, "total": 0, "condition_change": detail}
    run.progress = {**(run.progress or {}), "condition_change": detail}
    db.commit()


def _publish_condition_change(
    run: Run, target: RunTarget, device: Device, detail: dict[str, Any]
) -> None:
    """发布 M4 定义的 target 条件变化事件，保留新旧指纹供 SSE 重放。"""
    sse_event_buffer.publish(
        f"run:{run.id}",
        "target.aborted_condition_changed",
        {
            "run_id": str(run.id),
            "target_id": str(target.id),
            "device_id": str(device.id),
            "device_name": device.name,
            "detail": detail,
        },
    )


async def _watch_target_identity(
    factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    target_id: uuid.UUID,
    device_id: uuid.UUID,
    expected_uid: str,
    expected_sn: Any,
    abort_event: asyncio.Event,
    change_holder: dict[str, Any],
    stop_state: dict[str, Any] | None = None,
) -> None:
    """每隔 5 分钟采集一次无会话身份，变化时触发当前转写优雅关闭。"""
    while True:
        await asyncio.sleep(IDENTITY_CHECK_INTERVAL_S)
        db = factory()
        try:
            run = db.get(Run, run_id)
            target = db.get(RunTarget, target_id)
            device = db.get(Device, device_id)
            if (
                run is None
                or target is None
                or device is None
                or run.status != "running"
                or target.status != "running"
            ):
                return
            try:
                probe, deployment = await run_probe(db, device, mode="identity")
            except ApiError as exc:
                db.rollback()
                logger.warning("Run 身份巡检失败，继续等待下一周期：%s", exc.message)
                continue
            result = probe.result if isinstance(probe.result, dict) else {}
            if (
                probe.status != "ok"
                or deployment is None
                or result.get("version_collection_ok") is not True
            ):
                continue
            current_sn = result.get("sn")
            if deployment.uid == expected_uid and current_sn == expected_sn:
                continue
            detail = _identity_change_detail(
                run,
                target,
                device,
                expected_uid=expected_uid,
                expected_sn=expected_sn,
                new_uid=deployment.uid,
                new_sn=current_sn,
                new_model_key=deployment.model_key,
            )
            change_holder["detail"] = detail
            if stop_state is not None:
                stop_state["reason"] = "condition_changed"
                stop_state["detail"] = detail
            abort_event.set()
            _publish_condition_change(run, target, device, detail)
            return
        except Exception:
            db.rollback()
            logger.exception("Run 身份巡检异常，继续等待下一周期")
        finally:
            db.close()


async def _watch_run_cancellation(
    factory: sessionmaker[Session],
    run_id: uuid.UUID,
    abort_event: asyncio.Event,
    stop_state: dict[str, Any],
) -> None:
    """轮询取消状态，并通知所有在途会话通过 close 帧收尾。"""
    while not abort_event.is_set():
        db = factory()
        try:
            status = db.scalar(select(Run.status).where(Run.id == run_id))
            if status in {"cancelling", "cancelled"}:
                stop_state["reason"] = "cancelled"
                abort_event.set()
                return
        except Exception:
            db.rollback()
            logger.exception("Run 取消状态巡检失败：%s", run_id)
        finally:
            db.close()
        await asyncio.sleep(0.5)


def _mark_target_failed(db: Session, target_id: uuid.UUID, reason: str) -> None:
    """记录 target 级未捕获错误，避免 Run 留在 running 状态。"""
    db.rollback()
    target = db.get(RunTarget, target_id)
    if target is None:
        return
    target.status = "failed"
    target.abort_reason = reason[:128]
    target.stats = {**(target.stats or {}), "error": reason[:128]}
    target.finished_at = datetime.now(UTC)
    db.commit()


def _apply_running_eta(
    db: Session,
    run: Run,
    *,
    target: RunTarget,
    elapsed_ms: int,
    observed: list[tuple[str, float]],
    remaining: list[int],
    kind: str,
    lanes: int = 1,
    mode: str = "burst",
    input_rate: float = 1.0,
    force_p90: bool = False,
) -> None:
    """更新 target 滚动 ETA 并汇总 Run 的最慢目标（M4 §5.7/§6.2）。"""
    estimate = rolling_update(
        elapsed_ms=elapsed_ms,
        observed=observed,
        remaining_durations_ms=remaining,
        kind=kind,
        lanes=lanes,
        mode=mode,
        input_rate=input_rate,
    )
    previous_eta = target.stats.get("eta", {}) if isinstance(target.stats, dict) else {}
    target.stats = {
        **(target.stats or {}),
        "eta": {
            **(previous_eta if isinstance(previous_eta, dict) else {}),
            **estimate,
            "execution_ms": estimate["eta_ms"],
            "queue_wait_ms": 0,
            "total_ms": estimate["eta_p90_ms"] if force_p90 else estimate["eta_ms"],
            "p90_total_ms": estimate["eta_p90_ms"],
        },
    }
    _refresh_run_eta_from_targets(db, run)


def _refresh_run_eta_from_targets(db: Session, run: Run) -> None:
    """以设备并行的最大剩余时间更新 Run ETA；目标顺序不影响结果。"""
    db.flush()
    db.scalar(select(Run.id).where(Run.id == run.id).with_for_update())
    targets = db.scalars(select(RunTarget).where(RunTarget.run_id == run.id)).all()
    active = [target for target in targets if target.status in {"pending", "running"}]
    estimates: list[tuple[RunTarget, dict[str, Any]]] = []
    for target in active:
        stats = target.stats if isinstance(target.stats, dict) else {}
        eta = stats.get("eta")
        if isinstance(eta, dict):
            estimates.append((target, eta))
    if not estimates:
        run.eta_ms = 0
        run.eta_p90_ms = 0
        return
    _, slowest_eta = max(estimates, key=lambda item: int(item[1].get("total_ms", 0)))
    run.eta_ms = int(slowest_eta.get("total_ms", 0))
    run.eta_p90_ms = max(int(eta.get("p90_total_ms", 0)) for _target, eta in estimates)
    run.eta_source = str(slowest_eta.get("source", "fallback"))
    run.eta_confidence = str(slowest_eta.get("confidence", "low"))


def ensure_run_owner(run: Run, *, actor: str, role: str) -> None:
    """执行 Run 取消/续跑的对象级授权（M7 D-M7-14）。"""
    if role in {"engineer", "admin"} or run.created_by == actor:
        return
    raise ApiError("E_NOT_OWNER", "只能操作自己创建的 Run", status_code=403)


def delete_run(db: Session, run_id: uuid.UUID, *, actor: str) -> None:
    """删除终态 Run 及其评分明细；活动评分或报告快照引用期间禁止删除（M7 D-M7-21）。"""
    run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    if run.status in {"queued", "running", "cancelling"}:
        raise ApiError(
            "E_RUN_ACTIVE", "排队或运行中的评测任务不能删除，请先取消并等待结束", status_code=409
        )
    if run.status not in {
        "completed",
        "partial",
        "failed",
        "cancelled",
        "aborted_condition_changed",
    }:
        raise ApiError("E_RUN_STATUS", f"Run 当前状态为 {run.status}，不能删除", status_code=409)

    evaluations = db.scalars(
        select(Evaluation).where(Evaluation.run_id == run.id).with_for_update()
    ).all()
    active_evaluations = [
        evaluation
        for evaluation in evaluations
        if evaluation.status in {"queued", "scoring", "cancelling"}
    ]
    if active_evaluations:
        # 评分仍在排队/执行时删除 Run 会让 worker 丢失目标，先要求取消或等待终态。
        raise ApiError(
            "E_EVALUATION_ACTIVE",
            "Run 的评分任务仍在执行，请等待结束或取消后再删除",
            status_code=409,
            detail={"evaluation_ids": [str(item.id) for item in active_evaluations]},
        )
    evaluation_ids = [str(evaluation.id) for evaluation in evaluations]
    if evaluation_ids:
        references = [
            Report.evaluation_ids.contains([evaluation_id]) for evaluation_id in evaluation_ids
        ]
        active_report_jobs = [
            Job.progress.contains({"evaluation_ids": [evaluation_id]})
            for evaluation_id in evaluation_ids
        ]
        report_id = db.scalar(select(Report.id).where(or_(*references)).limit(1))
        report_job_id = db.scalar(
            select(Job.id)
            .where(
                Job.kind == "report",
                Job.status.in_(("queued", "running")),
                or_(Job.ref_id.in_(evaluation_ids), *active_report_jobs),
            )
            .limit(1)
        )
        if report_id is not None or report_job_id is not None:
            raise ApiError(
                "E_RUN_REFERENCED",
                "Run 的评分已被报告引用或正在生成报告，请先删除或等待相关报告后再删除",
                status_code=409,
            )

    run_jobs = db.scalars(
        select(Job).where(Job.kind == "run", Job.ref_type == "run", Job.ref_id == str(run.id))
    ).all()
    for evaluation in evaluations:
        db.delete(evaluation)
    for job in run_jobs:
        db.delete(job)
    audit.record(
        db,
        "run.delete",
        actor=actor,
        resource=f"run:{run.id}",
        detail={"name": run.name, "status": run.status, "evaluation_count": len(evaluations)},
    )
    db.delete(run)
    db.commit()


def cancel_run(db: Session, run_id: uuid.UUID) -> Run:
    """请求取消 Run：置 "cancelling"，由执行循环在 30s 内停止派发（M4 §5.9）。

    终态/completed 的 Run 不可取消；queued 可直接取消。
    """
    run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    if run.status in {
        "completed",
        "partial",
        "failed",
        "cancelled",
        "cancelling",
        "aborted_condition_changed",
    }:
        raise ApiError("E_CONFLICT", f"Run 当前状态为 {run.status}，不能取消", status_code=409)
    if run.status == "queued":
        run.status = "cancelled"
        run.finished_at = datetime.now(UTC)
        pending = db.scalars(
            select(RunTarget).where(RunTarget.run_id == run.id, RunTarget.status == "pending")
        ).all()
        for target in pending:
            target.status = "cancelled"
            target.finished_at = run.finished_at
        for job in db.scalars(
            select(Job)
            .where(
                Job.kind == "run",
                Job.ref_type == "run",
                Job.ref_id == str(run.id),
                Job.status == "queued",
            )
            .with_for_update()
        ).all():
            job.status = "cancelled"
            job.error_code = "E_JOB_CANCELLED"
            job.finished_at = run.finished_at
    else:
        run.status = "cancelling"
        for job in db.scalars(
            select(Job)
            .where(
                Job.kind == "run",
                Job.ref_type == "run",
                Job.ref_id == str(run.id),
                Job.status == "running",
            )
            .with_for_update()
        ).all():
            job.cancel_requested_at = job.cancel_requested_at or datetime.now(UTC)
    db.commit()
    db.refresh(run)
    return run


def resume_run(db: Session, run_id: uuid.UUID) -> Run:
    """续跑 failed/cancelled 的 Run：重新入队，execute_run 只补缺失样本（M4 §5.9）。"""
    run = db.get(Run, run_id)
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    if run.status not in {"failed", "cancelled"}:
        raise ApiError(
            "E_CONFLICT", f"Run 当前状态为 {run.status}，只能续跑 failed/cancelled", status_code=409
        )
    run.status = "queued"
    run.finished_at = None
    db.commit()
    db.refresh(run)
    return run


def _acquire_lease(db: Session, device_id: uuid.UUID, target_id: uuid.UUID, holder: str) -> None:
    """原子抢占设备；活动探测/声纹检查或已有租约都会返回 409。"""
    device = db.scalar(select(Device.id).where(Device.id == device_id).with_for_update())
    if device is None:
        db.rollback()
        raise ApiError("E_NOT_FOUND", "设备不存在", status_code=404)
    active_probe = db.scalar(
        select(Job.id).where(
            Job.kind.in_(("probe", "voiceprint_check", "speaker_extract")),
            Job.ref_type == "device",
            Job.ref_id == str(device_id),
            Job.status.in_(("queued", "running")),
        )
    )
    if active_probe is not None:
        db.rollback()
        raise ApiError("E_DEVICE_BUSY", "设备正在探测或进行声纹自检", status_code=409)
    active_ops = db.scalar(
        select(OpsAction.id).where(
            OpsAction.device_id == device_id,
            OpsAction.status.in_(("queued", "running")),
        )
    )
    if active_ops is not None:
        db.rollback()
        raise ApiError("E_DEVICE_BUSY", "设备正在执行运维操作", status_code=409)
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=120)
    statement = (
        insert(DeviceLease)
        .values(
            device_id=device_id,
            run_target_id=target_id,
            holder=holder,
            expires_at=expires,
        )
        .on_conflict_do_update(
            index_elements=[DeviceLease.device_id],
            set_={
                "run_target_id": target_id,
                "holder": holder,
                "acquired_at": now,
                "expires_at": expires,
            },
            where=DeviceLease.expires_at < now,
        )
        .returning(DeviceLease.device_id)
    )
    acquired = db.execute(statement).first()
    if acquired is None:
        db.rollback()
        raise ApiError("E_DEVICE_BUSY", "设备正在被其他任务占用", status_code=409)
    db.commit()


def _renew_lease(db: Session, device_id: uuid.UUID, holder: str) -> None:
    """心跳续期；持有者不匹配时不动。"""
    db.execute(
        text(
            "update device_lease set expires_at = now() + interval '120 seconds' "
            "where device_id = :device_id and holder = :holder"
        ),
        {"device_id": device_id, "holder": holder},
    )
    db.commit()


def _release_lease(db: Session, device_id: uuid.UUID, holder: str) -> None:
    """结束或异常时释放；只删自己持有的那条。"""
    db.execute(
        delete(DeviceLease).where(DeviceLease.device_id == device_id, DeviceLease.holder == holder)
    )
    db.commit()


def score_run(
    db: Session, run_id: uuid.UUID, profile_id: uuid.UUID, *, trigger: str = "manual"
) -> Evaluation:
    """在既有 Run 结果上按指定口径重新评分，不重新调用设备。"""
    run = db.get(Run, run_id)
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    if run.status not in {"completed", "partial"}:
        raise ApiError("E_CONFLICT", "Run 尚无可评分的终态结果", status_code=409)
    benchmark = db.get(Benchmark, run.benchmark_id)
    if benchmark is None:
        raise ApiError("E_SEMANTIC", "Run 缺少 benchmark", status_code=422)
    return _score_run(db, run, benchmark, profile_id=profile_id, trigger=trigger)


def create_evaluation_record(
    db: Session, run_id: uuid.UUID, profile_id: uuid.UUID, *, trigger: str = "manual"
) -> Evaluation:
    """创建已持久化的 queued Evaluation，由评分 worker 后台处理。"""
    run = db.get(Run, run_id)
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    if run.status not in {"completed", "partial"}:
        raise ApiError("E_CONFLICT", "Run 尚无可评分的终态结果", status_code=409)
    if db.get(Benchmark, run.benchmark_id) is None:
        raise ApiError("E_SEMANTIC", "Run 缺少 benchmark", status_code=422)
    profile = db.get(MetricProfile, profile_id)
    if profile is None:
        raise ApiError("E_SEMANTIC", "口径不存在", status_code=422)

    total = (
        db.scalar(
            select(func.count())
            .select_from(CaseResult)
            .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
            .where(
                RunTarget.run_id == run.id,
                CaseResult.status.in_(["ok", "cached"]),
            )
        )
        or 0
    )
    evaluation = Evaluation(
        run_id=run.id,
        metric_profile_id=profile.id,
        profile_hash=profile.profile_hash,
        scorer_version="cer-4",
        trigger=trigger,
        status="queued",
        progress={"phase": "queued", "done": 0, "total": int(total), "last_case_result_id": 0},
    )
    db.add(evaluation)
    db.commit()
    db.refresh(evaluation)
    return evaluation


def process_evaluation(db: Session, evaluation_id: uuid.UUID) -> Evaluation:
    """处理指定 Evaluation；依照已提交游标恢复未完成的评分页。"""
    evaluation = db.get(Evaluation, evaluation_id)
    if evaluation is None:
        raise ApiError("E_NOT_FOUND", "Evaluation 不存在", status_code=404)
    if evaluation.status in {"completed", "cancelled"}:
        return evaluation
    if evaluation.status == "cancelling":
        evaluation.status = "cancelled"
        evaluation.finished_at = datetime.now(UTC)
        db.commit()
        return evaluation
    run = db.get(Run, evaluation.run_id)
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    benchmark = db.get(Benchmark, run.benchmark_id)
    if benchmark is None:
        raise ApiError("E_SEMANTIC", "Run 缺少 benchmark", status_code=422)
    return _score_run(
        db,
        run,
        benchmark,
        profile_id=evaluation.metric_profile_id,
        trigger=evaluation.trigger,
        evaluation=evaluation,
    )


def _score_run(
    db: Session,
    run: Run,
    benchmark: Benchmark,
    *,
    profile_id: uuid.UUID,
    trigger: str,
    evaluation: Evaluation | None = None,
) -> Evaluation:
    """对成功 case 按 profile 计算指标并聚合（M5 §5.1/§5.2/§5.9）。

    - 逐样本结构按 §2.1：`{"cer": {status, value, s, d, i, n}}`；
    - 空参考不静默跳过，落 `na(empty_reference)` 记录（§7 异常降级）；
    - 覆盖率分母是计划 case 数（本次 Run 的全部转写结果），≥0.95 ok，(0,0.95) partial。
    """
    profile = db.get(MetricProfile, profile_id)
    if profile is None:
        raise ApiError("E_SEMANTIC", "口径不存在", status_code=422)
    cer_config = profile.params.get("cer")
    cer_config = cer_config if isinstance(cer_config, dict) else {}
    text_config = profile.params.get("text")
    text_config = text_config if isinstance(text_config, dict) else {}
    raw_drop_tags = text_config.get("drop_tags", [])
    drop_tags = tuple(tag for tag in raw_drop_tags if isinstance(tag, str))
    raw_drop_fillers = text_config.get("drop_fillers", [])
    drop_fillers = tuple(value for value in raw_drop_fillers if isinstance(value, str))
    traditional = str(text_config.get("traditional", "keep"))
    cer_enabled = cer_config.get("enabled", True) is not False
    # AliMeeting 的 <sc> 是会话分隔符，恒剥离且不计入编辑距离（M5 §5.1）。
    strip_sc = True
    strip_punct = text_config.get("strip_punct", True) is not False
    strip_space = text_config.get("strip_space", True) is not False
    lowercase = text_config.get("lowercase", True) is not False
    perf_config = profile.params.get("perf")
    perf_config = perf_config if isinstance(perf_config, dict) else {}
    perf_enabled = perf_config.get("enabled", True) is not False

    if evaluation is None:
        evaluation = Evaluation(
            run_id=run.id,
            metric_profile_id=profile.id,
            profile_hash=profile.profile_hash,
            scorer_version="cer-4",
            trigger=trigger,
            status="scoring",
            progress={},
        )
        db.add(evaluation)
    else:
        evaluation.status = "scoring"
        evaluation.finished_at = None
    db.flush()

    # 计划 case 数 = 本次 Run 实际产生的转写结果数（含失败；失败由 Run 状态表达）
    planned = (
        db.scalar(
            select(func.count())
            .select_from(CaseResult)
            .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
            .where(RunTarget.run_id == run.id)
        )
        or 0
    )

    row_statement = (
        select(CaseResult, RefText.text)
        .join(Item, Item.id == CaseResult.item_id)
        .outerjoin(
            RefText,
            (RefText.item_id == Item.id) & (RefText.kind == "verbatim"),
        )
        .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
        .where(
            RunTarget.run_id == run.id,
            CaseResult.status.in_(["ok", "cached"]),
        )
    )
    rows: list[tuple[CaseResult, str | None]] = []
    target_subsets = dict(
        db.execute(select(RunTarget.id, RunTarget.subset).where(RunTarget.run_id == run.id)).all()
    )
    target_planned_cases = dict(
        db.execute(
            select(CaseResult.run_target_id, func.count())
            .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
            .where(RunTarget.run_id == run.id)
            .group_by(CaseResult.run_target_id)
        ).all()
    )
    planned_bucket_rows = db.execute(
        select(CaseResult.id, CaseResult.run_target_id, Item.duration_ms, RefText.text)
        .join(Item, Item.id == CaseResult.item_id)
        .outerjoin(
            RefText,
            (RefText.item_id == Item.id) & (RefText.kind == "verbatim"),
        )
        .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
        .where(RunTarget.run_id == run.id)
    ).all()
    case_duration_buckets: dict[int, str] = {}
    planned_cases_by_bucket: dict[str, int] = {}
    target_planned_cases_by_bucket: dict[tuple[uuid.UUID, str], int] = {}
    planned_numeric_cases_by_target: dict[uuid.UUID, int] = {}
    planned_numeric_cases_by_bucket: dict[str, int] = {}
    target_planned_numeric_cases_by_bucket: dict[tuple[uuid.UUID, str], int] = {}
    for case_id, target_id, duration_ms, ref_text in planned_bucket_rows:
        bucket_name = duration_bucket(int(duration_ms))
        case_duration_buckets[int(case_id)] = bucket_name
        planned_cases_by_bucket[bucket_name] = planned_cases_by_bucket.get(bucket_name, 0) + 1
        target_bucket_key = (target_id, bucket_name)
        target_planned_cases_by_bucket[target_bucket_key] = (
            target_planned_cases_by_bucket.get(target_bucket_key, 0) + 1
        )
        if isinstance(ref_text, str) and numeric_reference_length(
            ref_text,
            strip_sc=strip_sc,
            strip_punct=strip_punct,
            strip_space=strip_space,
            lowercase=lowercase,
            drop_tags=drop_tags,
            drop_fillers=drop_fillers,
            traditional=traditional,
        ):
            planned_numeric_cases_by_target[target_id] = (
                planned_numeric_cases_by_target.get(target_id, 0) + 1
            )
            planned_numeric_cases_by_bucket[bucket_name] = (
                planned_numeric_cases_by_bucket.get(bucket_name, 0) + 1
            )
            target_planned_numeric_cases_by_bucket[target_bucket_key] = (
                target_planned_numeric_cases_by_bucket.get(target_bucket_key, 0) + 1
            )

    total_s = 0
    total_d = 0
    total_i = 0
    total_ref = 0
    scored = 0
    case_scores: dict[int, CaseScore] = {}
    cer_samples: list[tuple[int, int]] = []
    cer_case_values: list[float] = []
    cer_samples_by_target: dict[uuid.UUID, list[tuple[int, int]]] = {}
    cer_case_values_by_target: dict[uuid.UUID, list[float]] = {}
    cer_samples_by_bucket: dict[str, list[tuple[int, int]]] = {}
    cer_case_values_by_bucket: dict[str, list[float]] = {}
    cer_samples_by_target_bucket: dict[tuple[uuid.UUID, str], list[tuple[int, int]]] = {}
    cer_case_values_by_target_bucket: dict[tuple[uuid.UUID, str], list[float]] = {}
    existing_scores = {
        score.case_result_id: score
        for score in db.scalars(
            select(CaseScore).where(CaseScore.evaluation_id == evaluation.id)
        ).all()
    }
    saved_progress = evaluation.progress if isinstance(evaluation.progress, dict) else {}

    def progress_int(key: str) -> int:
        value = saved_progress.get(key, 0)
        return int(value) if isinstance(value, int | float | str) else 0

    resume_cursor = progress_int("last_case_result_id")
    progress_done = progress_int("done")
    progress_total = int(
        db.scalar(
            select(func.count())
            .select_from(CaseResult)
            .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
            .where(
                RunTarget.run_id == run.id,
                CaseResult.status.in_(["ok", "cached"]),
            )
        )
        or 0
    )
    from app.core.config import get_settings

    page_size = max(1, get_settings().scoring_page_size)

    def accumulate_cer(case: CaseResult, metrics: dict[str, object]) -> None:
        """从新算或已提交的 case_score 统一恢复 CER 聚合状态。"""
        nonlocal total_s, total_d, total_i, total_ref, scored
        raw_cer = metrics.get("cer")
        if not isinstance(raw_cer, dict) or raw_cer.get("status") != "ok":
            return
        edits = int(raw_cer.get("s", 0)) + int(raw_cer.get("d", 0)) + int(raw_cer.get("i", 0))
        ref_len = int(raw_cer.get("n", 0))
        value = float(raw_cer["value"])
        total_s += int(raw_cer.get("s", 0))
        total_d += int(raw_cer.get("d", 0))
        total_i += int(raw_cer.get("i", 0))
        total_ref += ref_len
        scored += 1
        cer_sample = (edits, ref_len)
        cer_samples.append(cer_sample)
        cer_case_values.append(value)
        cer_samples_by_target.setdefault(case.run_target_id, []).append(cer_sample)
        cer_case_values_by_target.setdefault(case.run_target_id, []).append(value)
        duration_bucket_name = case_duration_buckets.get(case.id)
        if duration_bucket_name is not None:
            cer_samples_by_bucket.setdefault(duration_bucket_name, []).append(cer_sample)
            cer_case_values_by_bucket.setdefault(duration_bucket_name, []).append(value)
            cer_samples_by_target_bucket.setdefault(
                (case.run_target_id, duration_bucket_name), []
            ).append(cer_sample)
            cer_case_values_by_target_bucket.setdefault(
                (case.run_target_id, duration_bucket_name), []
            ).append(value)

    def set_case_score(case: CaseResult, metrics: dict[str, object]) -> CaseScore:
        score = existing_scores.get(case.id)
        if score is None:
            score = CaseScore(
                evaluation_id=evaluation.id,
                case_result_id=case.id,
                metrics=metrics,
            )
            db.add(score)
            existing_scores[case.id] = score
        else:
            score.metrics = metrics
        case_scores[case.id] = score
        return score

    def finish_cancelled() -> Evaluation:
        """丢弃当前未提交的聚合写入，保留最近完成页及其游标。"""
        db.rollback()
        current = db.get(Evaluation, evaluation.id)
        if current is None:
            raise ApiError("E_NOT_FOUND", "Evaluation 不存在", status_code=404)
        current.status = "cancelled"
        current.finished_at = datetime.now(UTC)
        current.progress = {
            **(current.progress if isinstance(current.progress, dict) else {}),
            "phase": "cancelled",
        }
        db.commit()
        db.refresh(current)
        return current

    def cancellation_requested() -> bool:
        return (
            db.scalar(select(Evaluation.status).where(Evaluation.id == evaluation.id))
            == "cancelling"
        )

    page_cursor = 0
    while True:
        page = db.execute(
            row_statement.where(CaseResult.id > page_cursor)
            .order_by(CaseResult.id)
            .limit(page_size)
        ).all()
        if not page:
            break
        rows.extend(page)
        page_new_count = 0
        for case, ref_text in page:
            previous_score = existing_scores.get(case.id)
            if case.id <= resume_cursor and previous_score is not None:
                case_scores[case.id] = previous_score
                accumulate_cer(case, previous_score.metrics)
                continue

            if case.id > resume_cursor:
                page_new_count += 1
            if not cer_enabled:
                # 口径关闭 CER：逐条落 na(metric_disabled)，不做无意义的编辑距离计算。
                score = set_case_score(case, {"cer": {"status": "na", "reason": "metric_disabled"}})
                accumulate_cer(case, score.metrics)
                continue
            if ref_text is None:
                score = set_case_score(case, {"cer": {"status": "na", "reason": "no_reference"}})
                accumulate_cer(case, score.metrics)
                continue
            try:
                result = compute_cer(
                    ref_text,
                    case.text or "",
                    strip_sc=strip_sc,
                    strip_punct=strip_punct,
                    strip_space=strip_space,
                    lowercase=lowercase,
                    drop_tags=drop_tags,
                    drop_fillers=drop_fillers,
                    traditional=traditional,
                )
            except ValueError:
                # 空参考：单条不可评要可追溯，落 na 记录（M5 §7）
                score = set_case_score(case, {"cer": {"status": "na", "reason": "empty_reference"}})
                accumulate_cer(case, score.metrics)
                continue
            score = set_case_score(
                case,
                {
                    "cer": {
                        "status": "ok",
                        "value": result.value,
                        "s": result.s,
                        "d": result.d,
                        "i": result.i,
                        "n": result.n,
                    }
                },
            )
            accumulate_cer(case, score.metrics)

        page_cursor = int(page[-1][0].id)
        if page_cursor > resume_cursor:
            progress_done += page_new_count
        evaluation.progress = {
            "phase": "scoring",
            "done": min(progress_done, progress_total),
            "total": progress_total,
            "last_case_result_id": page_cursor,
        }
        db.commit()
        resume_cursor = max(resume_cursor, page_cursor)
        if cancellation_requested():
            return finish_cancelled()

    evaluation.progress = {
        "phase": "aggregating",
        "done": progress_total,
        "total": progress_total,
        "last_case_result_id": resume_cursor,
    }
    db.commit()
    if cancellation_requested():
        return finish_cancelled()

    # 覆盖率状态直接落 metric_value.status（M5 §5.9）
    if cer_enabled:
        coverage_status, coverage_reason = coverage_state(scored, planned)
    else:
        coverage_status, coverage_reason = "na", "metric_disabled"

    micro = (total_s + total_d + total_i) / total_ref if total_ref else None
    evaluation.coverage = {"scored": scored, "planned": planned}
    evaluation.metrics = {
        "cer": micro,
        "s": total_s,
        "d": total_d,
        "i": total_i,
        "ref_len": total_ref,
        "n": scored,
    }

    def add_metric_value(
        *,
        run_target_id: uuid.UUID | None,
        subset: str,
        value: float | None,
        n: int,
        status: str,
        reason: str | None,
        metric: str = "cer",
        scope: str = "micro",
        detail: dict[str, object] | None = None,
        ci: tuple[float | None, float | None] | None = None,
        bucket: dict[str, str] | None = None,
    ) -> None:
        """聚合结果落 metric_value（M5 §4/§5.9）：报告只读这里，不扫明细。"""
        db.add(
            MetricValue(
                evaluation_id=evaluation.id,
                metric=metric,
                run_target_id=run_target_id,
                subset=subset,
                scope=scope,
                bucket=bucket or {},
                value=value,
                n=n,
                ci_low=ci[0] if ci else None,
                ci_high=ci[1] if ci else None,
                status=status,
                reason=reason,
                detail=detail or {},
            )
        )

    cer_aggregate = cer_config.get("aggregate", "micro")
    if not cer_enabled:
        # 关闭 CER 时不落任何数值，只写一条可追溯的 na；避免报告把 0 当成绩。
        cer_aggregate = "disabled"
    elif cer_aggregate not in {"micro", "macro", "both"}:
        cer_aggregate = "micro"

    if not cer_enabled:
        add_metric_value(
            run_target_id=None,
            subset="all",
            value=None,
            n=0,
            status="na",
            reason="metric_disabled",
            metric="cer",
            detail={"planned_cases": planned, "scoreable_cases": 0},
        )

    def add_cer_macro(
        *,
        run_target_id: uuid.UUID | None,
        subset: str,
        values: list[float],
        planned_count: int,
        bucket: dict[str, str] | None = None,
    ) -> None:
        """按样本 CER 算术平均，覆盖率与 CI 均按 utterance 计算。"""
        status, reason = coverage_state(len(values), planned_count)
        if not values and planned_count:
            status, reason = "na", "empty_reference"
        add_metric_value(
            run_target_id=run_target_id,
            subset=subset,
            value=sum(values) / len(values) if values else None,
            n=len(values),
            status=status,
            reason=reason,
            scope="macro",
            ci=mean_ci(values),
            bucket=bucket,
            detail={"planned_cases": planned_count, "scoreable_cases": len(values)},
        )

    # 按 profile 选择 CER micro、macro 或同时写入（M5 §5.8）。
    for target_id, target_subset in target_subsets.items():
        target_samples = cer_samples_by_target.get(target_id, [])
        target_case_values = cer_case_values_by_target.get(target_id, [])
        target_edits = sum(edits for edits, _ref_len in target_samples)
        target_ref_len = sum(ref_len for _edits, ref_len in target_samples)
        target_value = target_edits / target_ref_len if target_ref_len else None
        target_status, target_reason = coverage_state(
            len(target_samples), int(target_planned_cases.get(target_id, 0))
        )
        if target_value is None and target_samples:
            target_status, target_reason = "na", "empty_reference"
        if cer_aggregate in {"micro", "both"}:
            add_metric_value(
                run_target_id=target_id,
                subset=str(target_subset),
                value=target_value,
                n=len(target_samples),
                status=target_status,
                reason=target_reason,
                ci=ratio_ci(target_samples),
                scope="micro",
            )
        if cer_aggregate in {"macro", "both"}:
            add_cer_macro(
                run_target_id=target_id,
                subset=str(target_subset),
                values=target_case_values,
                planned_count=int(target_planned_cases.get(target_id, 0)),
            )

    if cer_aggregate in {"micro", "both"}:
        add_metric_value(
            run_target_id=None,
            subset="all",
            value=micro,
            n=scored,
            status=coverage_status if micro is not None else "na",
            reason=coverage_reason if micro is not None else "empty_reference",
            ci=ratio_ci(cer_samples),
            scope="micro",
        )
    if cer_aggregate in {"macro", "both"}:
        add_cer_macro(
            run_target_id=None,
            subset="all",
            values=cer_case_values,
            planned_count=planned,
        )

    for bucket_name, planned_count in planned_cases_by_bucket.items():
        bucket_samples = cer_samples_by_bucket.get(bucket_name, [])
        bucket_case_values = cer_case_values_by_bucket.get(bucket_name, [])
        bucket_edits = sum(edits for edits, _ref_len in bucket_samples)
        bucket_ref_len = sum(ref_len for _edits, ref_len in bucket_samples)
        bucket_value = bucket_edits / bucket_ref_len if bucket_ref_len else None
        bucket_status, bucket_reason = coverage_state(len(bucket_samples), planned_count)
        if bucket_value is None and bucket_samples:
            bucket_status, bucket_reason = "na", "empty_reference"
        if cer_aggregate in {"micro", "both"}:
            add_metric_value(
                run_target_id=None,
                subset="all",
                value=bucket_value,
                n=len(bucket_samples),
                status=bucket_status,
                reason=bucket_reason,
                ci=ratio_ci(bucket_samples),
                scope="micro",
                bucket={"duration_bucket": bucket_name},
                detail={"planned_cases": planned_count, "scoreable_cases": len(bucket_samples)},
            )
        if cer_aggregate in {"macro", "both"}:
            add_cer_macro(
                run_target_id=None,
                subset="all",
                values=bucket_case_values,
                planned_count=planned_count,
                bucket={"duration_bucket": bucket_name},
            )
        for target_id, target_subset in target_subsets.items():
            target_samples = cer_samples_by_target_bucket.get((target_id, bucket_name), [])
            target_case_values = cer_case_values_by_target_bucket.get((target_id, bucket_name), [])
            target_planned = target_planned_cases_by_bucket.get((target_id, bucket_name), 0)
            target_edits = sum(edits for edits, _ref_len in target_samples)
            target_ref_len = sum(ref_len for _edits, ref_len in target_samples)
            target_value = target_edits / target_ref_len if target_ref_len else None
            target_status, target_reason = coverage_state(len(target_samples), target_planned)
            if target_value is None and target_samples:
                target_status, target_reason = "na", "empty_reference"
            if cer_aggregate in {"micro", "both"}:
                add_metric_value(
                    run_target_id=target_id,
                    subset=str(target_subset),
                    value=target_value,
                    n=len(target_samples),
                    status=target_status,
                    reason=target_reason,
                    ci=ratio_ci(target_samples),
                    scope="micro",
                    bucket={"duration_bucket": bucket_name},
                    detail={
                        "planned_cases": target_planned,
                        "scoreable_cases": len(target_samples),
                    },
                )
            if cer_aggregate in {"macro", "both"}:
                add_cer_macro(
                    run_target_id=target_id,
                    subset=str(target_subset),
                    values=target_case_values,
                    planned_count=target_planned,
                    bucket={"duration_bucket": bucket_name},
                )

    # DER 按 session 重采样，避免同一会话内的样本相关性把区间压窄。
    from app.scoring.speaker import der as speaker_der
    from app.scoring.speaker import eer as speaker_eer
    from app.scoring.speaker import naming as speaker_naming

    der_totals = {"miss": 0, "fa": 0, "conf": 0, "ref": 0}
    der_cases_by_target: dict[uuid.UUID, int] = {}
    der_totals_by_target: dict[uuid.UUID, dict[str, int]] = {}
    der_sessions: dict[str, list[int]] = {}
    der_sessions_by_target: dict[uuid.UUID, dict[str, list[int]]] = {}
    no_hyp_by_target: dict[uuid.UUID, int] = {}
    no_diarization_by_target: dict[uuid.UUID, int] = {}
    no_ref_by_target: dict[uuid.UUID, int] = {}
    sa_totals = {"miss": 0, "fa": 0, "conf": 0, "ref": 0}
    sa_totals_by_target: dict[uuid.UUID, dict[str, int]] = {}
    sa_cases_by_target: dict[uuid.UUID, int] = {}
    sa_sessions: dict[str, list[int]] = {}
    sa_sessions_by_target: dict[uuid.UUID, dict[str, list[int]]] = {}
    naming_totals = {"correct": 0, "fa": 0, "fr": 0, "total": 0}
    naming_totals_by_target: dict[uuid.UUID, dict[str, int]] = {}
    naming_cases_by_target: dict[uuid.UUID, int] = {}
    naming_sessions: dict[str, list[int]] = {}
    naming_sessions_by_target: dict[uuid.UUID, dict[str, list[int]]] = {}
    naming_no_match_by_target: dict[uuid.UUID, int] = {}
    eer_trials: list[tuple[float, bool]] = []
    eer_trials_by_target: dict[uuid.UUID, list[tuple[float, bool]]] = {}
    der_config = profile.params.get("der")
    der_enabled = not isinstance(der_config, dict) or der_config.get("enabled") is not False
    der_collar_ms = int(der_config.get("collar_ms", 250)) if isinstance(der_config, dict) else 250
    der_skip_overlap = (
        bool(der_config.get("skip_overlap", False)) if isinstance(der_config, dict) else False
    )
    naming_config = profile.params.get("naming")
    naming_enabled = (
        not isinstance(naming_config, dict) or naming_config.get("enabled") is not False
    )
    naming_threshold = (
        float(naming_config.get("threshold", 0.6)) if isinstance(naming_config, dict) else 0.6
    )
    naming_unknown_policy = (
        str(naming_config.get("unknown_policy", "fr")) if isinstance(naming_config, dict) else "fr"
    )
    item_ids = {case.item_id for case, _ref_text in rows}
    items_by_id = {
        item.id: item for item in db.scalars(select(Item).where(Item.id.in_(item_ids))).all()
    }
    ref_segments_by_item: dict[int, list[RefSegment]] = {}
    for segment in db.scalars(
        select(RefSegment)
        .where(RefSegment.item_id.in_(item_ids), RefSegment.skip.is_(False))
        .order_by(RefSegment.item_id, RefSegment.start_ms, RefSegment.seq)
    ).all():
        ref_segments_by_item.setdefault(segment.item_id, []).append(segment)
    hypothesis_ids = {
        case.hypothesis_id for case, _ref_text in rows if case.hypothesis_id is not None
    }
    hypotheses_by_id = {
        hypothesis.id: hypothesis
        for hypothesis in db.scalars(
            select(Hypothesis).where(Hypothesis.id.in_(hypothesis_ids))
        ).all()
    }
    for case_index, (case, _ref_text) in enumerate(rows, start=1):
        if (case_index - 1) % page_size == 0 and cancellation_requested():
            return finish_cancelled()
        item = items_by_id.get(case.item_id)
        if item is None or not item.duration_ms:
            continue
        stored_score = case_scores.get(case.id)
        if stored_score is None:
            continue
        ref_segments = [
            (segment.speaker, segment.start_ms, segment.end_ms)
            for segment in ref_segments_by_item.get(item.id, [])
        ]
        if not ref_segments:
            no_ref_by_target[case.run_target_id] = no_ref_by_target.get(case.run_target_id, 0) + 1
            stored_score.metrics = {
                **stored_score.metrics,
                "der": {
                    "status": "na",
                    "reason": "no_segment_timeline" if der_enabled else "metric_disabled",
                },
                "sa_der": {
                    "status": "na",
                    "reason": "no_segment_timeline" if der_enabled else "metric_disabled",
                },
                "naming_acc": {
                    "status": "na",
                    "reason": "no_speaker_reference" if naming_enabled else "metric_disabled",
                },
            }
            continue
        hypothesis = (
            hypotheses_by_id.get(case.hypothesis_id) if case.hypothesis_id is not None else None
        )
        raw: object = (
            (hypothesis.segments or {}).get("items") if hypothesis and hypothesis.segments else []
        )
        raw_segments: list[Any] = list(raw) if isinstance(raw, list) else []
        hyp_segments: list[tuple[str, int, int]] = []
        for segment in raw_segments:
            try:
                hyp_segments.append(
                    (
                        str(segment.get("speaker") or "spk"),
                        int(segment.get("start_ms") or 0),
                        int(segment.get("end_ms") or 0),
                    )
                )
            except (TypeError, ValueError):
                continue
        if not hyp_segments:
            no_hyp_by_target[case.run_target_id] = no_hyp_by_target.get(case.run_target_id, 0) + 1
        reference_speakers = {speaker for speaker, _start, _end in ref_segments}
        hypothesis_speakers = {speaker for speaker, _start, _end in hyp_segments}
        diarization_missing = (
            bool(hyp_segments)
            and hypothesis_speakers <= {"spk", "-1", "unknown"}
            and len(reference_speakers) > 1
        )
        if diarization_missing:
            no_diarization_by_target[case.run_target_id] = (
                no_diarization_by_target.get(case.run_target_id, 0) + 1
            )
        session_key = item.pair_key or item.item_key
        raw_match_data = hypothesis.speaker_match if hypothesis and hypothesis.speaker_match else {}
        matched_ids: dict[str, str | None] = {}
        raw_matched_ids: dict[str, str | None] = {}
        confidences: dict[str, float] = {}
        if isinstance(raw_match_data, dict):
            for raw_label, raw_entry in raw_match_data.items():
                if not isinstance(raw_entry, dict):
                    continue
                label = str(raw_label)
                match_info = raw_entry.get("match_info")
                if match_info is None:
                    matched_ids[label] = None
                    raw_matched_ids[label] = None
                    continue
                if not isinstance(match_info, dict):
                    continue
                raw_id = match_info.get("matched_id")
                matched_id = str(raw_id) if isinstance(raw_id, str) else None
                raw_matched_ids[label] = matched_id
                matched_ids[label] = matched_id
                raw_confidence = match_info.get("confidence")
                if isinstance(raw_confidence, int | float) and not isinstance(raw_confidence, bool):
                    confidences[label] = float(raw_confidence)
                    if raw_confidence < naming_threshold:
                        matched_ids[label] = None

        has_voiceprint_match = any(value is not None for value in raw_matched_ids.values())

        der_metric: dict[str, object]
        if not der_enabled:
            der_metric = {"status": "na", "reason": "metric_disabled"}
        elif diarization_missing:
            der_metric = {"status": "na", "reason": "no_diarization_output"}
        else:
            der_result = speaker_der(
                ref_segments,
                hyp_segments,
                int(item.duration_ms),
                collar_ms=der_collar_ms,
                skip_overlap=der_skip_overlap,
            )
            if der_result["value"] is None or not der_result.get("ref_frames"):
                der_metric = {
                    "status": "na",
                    "reason": str(der_result.get("reason") or "no_segment_timeline"),
                }
            else:
                miss = int(der_result.get("miss_frames") or der_result.get("miss") or 0)
                false_alarm = int(der_result.get("fa_frames") or der_result.get("fa") or 0)
                confusion = int(der_result.get("conf_frames") or der_result.get("conf") or 0)
                reference_frames = int(der_result["ref_frames"])
                for key, value in (
                    ("miss", miss),
                    ("fa", false_alarm),
                    ("conf", confusion),
                    ("ref", reference_frames),
                ):
                    der_totals[key] += value
                target_totals = der_totals_by_target.setdefault(
                    case.run_target_id, {"miss": 0, "fa": 0, "conf": 0, "ref": 0}
                )
                target_totals["miss"] += miss
                target_totals["fa"] += false_alarm
                target_totals["conf"] += confusion
                target_totals["ref"] += reference_frames
                der_cases_by_target[case.run_target_id] = (
                    der_cases_by_target.get(case.run_target_id, 0) + 1
                )
                session_totals = der_sessions.setdefault(session_key, [0, 0])
                session_totals[0] += miss + false_alarm + confusion
                session_totals[1] += reference_frames
                target_sessions = der_sessions_by_target.setdefault(case.run_target_id, {})
                target_session_totals = target_sessions.setdefault(session_key, [0, 0])
                target_session_totals[0] += miss + false_alarm + confusion
                target_session_totals[1] += reference_frames
                der_metric = {
                    "status": "ok",
                    "value": (miss + false_alarm + confusion) / reference_frames,
                    "miss_frames": miss,
                    "fa_frames": false_alarm,
                    "conf_frames": confusion,
                    "ref_frames": reference_frames,
                }

        if not der_enabled:
            sa_metric: dict[str, object] = {"status": "na", "reason": "metric_disabled"}
        elif diarization_missing:
            sa_metric = {"status": "na", "reason": "no_diarization_output"}
        elif not has_voiceprint_match:
            sa_metric = {"status": "na", "reason": "no_voiceprint_match"}
        else:
            sa_result = speaker_der(
                ref_segments,
                hyp_segments,
                int(item.duration_ms),
                collar_ms=der_collar_ms,
                skip_overlap=der_skip_overlap,
                fixed_mapping=raw_matched_ids,
            )
            if sa_result["value"] is None or not sa_result.get("ref_frames"):
                sa_metric = {
                    "status": "na",
                    "reason": str(sa_result.get("reason") or "no_segment_timeline"),
                }
            else:
                sa_miss = int(sa_result.get("miss_frames") or sa_result.get("miss") or 0)
                sa_fa = int(sa_result.get("fa_frames") or sa_result.get("fa") or 0)
                sa_conf = int(sa_result.get("conf_frames") or sa_result.get("conf") or 0)
                sa_ref = int(sa_result["ref_frames"])
                for key, value in (
                    ("miss", sa_miss),
                    ("fa", sa_fa),
                    ("conf", sa_conf),
                    ("ref", sa_ref),
                ):
                    sa_totals[key] += value
                target_sa_totals = sa_totals_by_target.setdefault(
                    case.run_target_id, {"miss": 0, "fa": 0, "conf": 0, "ref": 0}
                )
                target_sa_totals["miss"] += sa_miss
                target_sa_totals["fa"] += sa_fa
                target_sa_totals["conf"] += sa_conf
                target_sa_totals["ref"] += sa_ref
                sa_cases_by_target[case.run_target_id] = (
                    sa_cases_by_target.get(case.run_target_id, 0) + 1
                )
                sa_session = sa_sessions.setdefault(session_key, [0, 0])
                sa_session[0] += sa_miss + sa_fa + sa_conf
                sa_session[1] += sa_ref
                target_sa_sessions = sa_sessions_by_target.setdefault(case.run_target_id, {})
                target_sa_session = target_sa_sessions.setdefault(session_key, [0, 0])
                target_sa_session[0] += sa_miss + sa_fa + sa_conf
                target_sa_session[1] += sa_ref
                sa_metric = {
                    "status": "ok",
                    "value": (sa_miss + sa_fa + sa_conf) / sa_ref,
                    "miss_frames": sa_miss,
                    "fa_frames": sa_fa,
                    "conf_frames": sa_conf,
                    "ref_frames": sa_ref,
                }

        if not naming_enabled:
            naming_metric: dict[str, object] = {"status": "na", "reason": "metric_disabled"}
        elif diarization_missing:
            naming_metric = {"status": "na", "reason": "no_diarization_output"}
        elif not has_voiceprint_match:
            naming_metric = {"status": "na", "reason": "no_voiceprint_match"}
            naming_no_match_by_target[case.run_target_id] = (
                naming_no_match_by_target.get(case.run_target_id, 0) + 1
            )
        else:
            naming_result = speaker_naming(
                ref_segments,
                hyp_segments,
                matched_ids,
                unknown_policy=naming_unknown_policy,
            )
            naming_metric = dict(naming_result)
            if naming_result.get("status") == "ok":
                correct_count = int(naming_result.get("correct_count") or 0)
                fa_count = int(naming_result.get("fa_count") or 0)
                fr_count = int(naming_result.get("fr_count") or 0)
                segment_count = int(naming_result.get("n") or 0)
                for key, value in (
                    ("correct", correct_count),
                    ("fa", fa_count),
                    ("fr", fr_count),
                    ("total", segment_count),
                ):
                    naming_totals[key] += value
                target_naming = naming_totals_by_target.setdefault(
                    case.run_target_id, {"correct": 0, "fa": 0, "fr": 0, "total": 0}
                )
                target_naming["correct"] += correct_count
                target_naming["fa"] += fa_count
                target_naming["fr"] += fr_count
                target_naming["total"] += segment_count
                naming_cases_by_target[case.run_target_id] = (
                    naming_cases_by_target.get(case.run_target_id, 0) + 1
                )
                naming_session = naming_sessions.setdefault(session_key, [0, 0])
                naming_session[0] += correct_count
                naming_session[1] += segment_count
                target_naming_sessions = naming_sessions_by_target.setdefault(
                    case.run_target_id, {}
                )
                target_naming_session = target_naming_sessions.setdefault(session_key, [0, 0])
                target_naming_session[0] += correct_count
                target_naming_session[1] += segment_count
            elif naming_result.get("reason") == "no_voiceprint_match":
                naming_no_match_by_target[case.run_target_id] = (
                    naming_no_match_by_target.get(case.run_target_id, 0) + 1
                )

        hyp_labels = (
            set() if diarization_missing else {speaker for speaker, _start, _end in hyp_segments}
        )
        for label in hyp_labels & confidences.keys():
            overlaps: dict[str, int] = {}
            for ref_speaker, ref_start, ref_end in ref_segments:
                for hyp_speaker, hyp_start, hyp_end in hyp_segments:
                    if hyp_speaker != label:
                        continue
                    overlap_ms = min(ref_end, hyp_end) - max(ref_start, hyp_start)
                    if overlap_ms > 0:
                        overlaps[ref_speaker] = overlaps.get(ref_speaker, 0) + overlap_ms
            expected_speaker = (
                max(overlaps, key=lambda speaker: overlaps[speaker]) if overlaps else None
            )
            correct_match = (
                expected_speaker is not None and raw_matched_ids.get(label) == expected_speaker
            )
            trial = (confidences[label], correct_match)
            eer_trials.append(trial)
            eer_trials_by_target.setdefault(case.run_target_id, []).append(trial)

        stored_score.metrics = {
            **stored_score.metrics,
            "der": der_metric,
            "sa_der": sa_metric,
            "naming_acc": naming_metric,
        }

    for der_target_id, subset in [(None, "all"), *target_subsets.items()]:
        der_case_count = (
            sum(der_cases_by_target.values())
            if der_target_id is None
            else der_cases_by_target.get(der_target_id, 0)
        )
        target_totals = (
            der_totals
            if der_target_id is None
            else der_totals_by_target.get(der_target_id, {"miss": 0, "fa": 0, "conf": 0, "ref": 0})
        )
        planned_count = (
            planned if der_target_id is None else int(target_planned_cases.get(der_target_id, 0))
        )
        target_sessions = (
            der_sessions if der_target_id is None else der_sessions_by_target.get(der_target_id, {})
        )
        session_samples = [(counts[0], counts[1]) for counts in target_sessions.values()]
        denominator = target_totals["ref"]
        der_edits = target_totals["miss"] + target_totals["fa"] + target_totals["conf"]
        der_value = der_edits / denominator if denominator else None
        der_status: str
        der_reason: str | None
        if not der_enabled:
            der_value = None
            der_reason = "metric_disabled"
            der_status = "na"
        elif der_value is None:
            missing_hyp = (
                sum(no_hyp_by_target.values())
                if der_target_id is None
                else no_hyp_by_target.get(der_target_id, 0)
            )
            missing_ref = (
                sum(no_ref_by_target.values())
                if der_target_id is None
                else no_ref_by_target.get(der_target_id, 0)
            )
            der_reason = (
                "no_diarization_output"
                if missing_hyp and not missing_ref
                else "no_segment_timeline"
            )
            der_status = "na"
        else:
            der_status, der_reason = coverage_state(der_case_count, planned_count)
        add_metric_value(
            run_target_id=der_target_id,
            subset=str(subset),
            value=der_value,
            n=der_case_count,
            status=der_status,
            reason=der_reason,
            metric="der",
            detail={
                "miss_frames": target_totals["miss"],
                "fa_frames": target_totals["fa"],
                "conf_frames": target_totals["conf"],
                "ref_frames": denominator,
            },
            ci=ratio_ci(session_samples),
        )
    for speaker_target_id, subset in [(None, "all"), *target_subsets.items()]:
        speaker_planned = (
            planned
            if speaker_target_id is None
            else int(target_planned_cases.get(speaker_target_id, 0))
        )
        speaker_sa_totals = (
            sa_totals
            if speaker_target_id is None
            else sa_totals_by_target.get(
                speaker_target_id, {"miss": 0, "fa": 0, "conf": 0, "ref": 0}
            )
        )
        speaker_sa_cases = (
            sum(sa_cases_by_target.values())
            if speaker_target_id is None
            else sa_cases_by_target.get(speaker_target_id, 0)
        )
        speaker_sa_sessions = (
            sa_sessions
            if speaker_target_id is None
            else sa_sessions_by_target.get(speaker_target_id, {})
        )
        sa_denominator = speaker_sa_totals["ref"]
        sa_errors = speaker_sa_totals["miss"] + speaker_sa_totals["fa"] + speaker_sa_totals["conf"]
        sa_value = sa_errors / sa_denominator if sa_denominator else None
        sa_status: str
        sa_reason: str | None
        if not der_enabled:
            sa_status, sa_reason = "na", "metric_disabled"
        elif sa_value is None:
            missing_diarization = (
                sum(no_diarization_by_target.values())
                if speaker_target_id is None
                else no_diarization_by_target.get(speaker_target_id, 0)
            )
            sa_status = "na"
            sa_reason = (
                "no_diarization_output"
                if missing_diarization
                else ("no_voiceprint_match" if der_cases_by_target else "no_segment_timeline")
            )
        else:
            sa_status, sa_reason = coverage_state(speaker_sa_cases, speaker_planned)
        add_metric_value(
            run_target_id=speaker_target_id,
            subset=str(subset),
            value=sa_value if der_enabled else None,
            n=speaker_sa_cases,
            status=sa_status,
            reason=sa_reason,
            metric="sa_der",
            detail={
                "miss_frames": speaker_sa_totals["miss"],
                "fa_frames": speaker_sa_totals["fa"],
                "conf_frames": speaker_sa_totals["conf"],
                "ref_frames": sa_denominator,
            },
            ci=ratio_ci([(counts[0], counts[1]) for counts in speaker_sa_sessions.values()]),
        )

        speaker_naming_totals = (
            naming_totals
            if speaker_target_id is None
            else naming_totals_by_target.get(
                speaker_target_id, {"correct": 0, "fa": 0, "fr": 0, "total": 0}
            )
        )
        speaker_naming_cases = (
            sum(naming_cases_by_target.values())
            if speaker_target_id is None
            else naming_cases_by_target.get(speaker_target_id, 0)
        )
        speaker_naming_sessions = (
            naming_sessions
            if speaker_target_id is None
            else naming_sessions_by_target.get(speaker_target_id, {})
        )
        naming_denominator = speaker_naming_totals["total"]
        naming_value = (
            speaker_naming_totals["correct"] / naming_denominator if naming_denominator else None
        )
        naming_status: str
        naming_reason: str | None
        if not naming_enabled:
            naming_status, naming_reason = "na", "metric_disabled"
        elif naming_value is None:
            missing_match = (
                sum(naming_no_match_by_target.values())
                if speaker_target_id is None
                else naming_no_match_by_target.get(speaker_target_id, 0)
            )
            missing_diarization = (
                sum(no_diarization_by_target.values())
                if speaker_target_id is None
                else no_diarization_by_target.get(speaker_target_id, 0)
            )
            naming_status = "na"
            naming_reason = (
                "no_diarization_output"
                if missing_diarization
                else ("no_voiceprint_match" if missing_match else "no_speaker_reference")
            )
        else:
            naming_status, naming_reason = coverage_state(speaker_naming_cases, speaker_planned)
        add_metric_value(
            run_target_id=speaker_target_id,
            subset=str(subset),
            value=naming_value if naming_enabled else None,
            n=naming_denominator,
            status=naming_status,
            reason=naming_reason,
            metric="naming_acc",
            detail={
                "correct": speaker_naming_totals["correct"],
                "fa": speaker_naming_totals["fa"],
                "fr": speaker_naming_totals["fr"],
                "total": naming_denominator,
                "scoreable_cases": speaker_naming_cases,
                "planned_cases": speaker_planned,
            },
            ci=ratio_ci([(counts[0], counts[1]) for counts in speaker_naming_sessions.values()]),
        )

        target_eer_trials = (
            eer_trials
            if speaker_target_id is None
            else eer_trials_by_target.get(speaker_target_id, [])
        )
        if not naming_enabled:
            eer_result: dict[str, Any] = {"status": "na", "reason": "metric_disabled", "n": 0}
        else:
            eer_result = speaker_eer(target_eer_trials)
        add_metric_value(
            run_target_id=speaker_target_id,
            subset=str(subset),
            value=eer_result.get("value"),
            n=int(eer_result.get("n") or 0),
            status=str(eer_result["status"]),
            reason=str(eer_result["reason"]) if eer_result.get("reason") else None,
            metric="speaker_eer",
            detail={"far": eer_result.get("far"), "frr": eer_result.get("frr")},
        )

    # 标点 F1（M5 §5.3）：dataset 必须 punct_supported；设备不出标点 → na
    from app.scoring.itn import ItnResult, score_itn
    from app.scoring.itn import aggregate as itn_aggregate
    from app.scoring.punct import aggregate as punct_aggregate
    from app.scoring.punct import score_punctuation

    version = db.get(DatasetVersion, benchmark.dataset_version_id)
    caps = (version.capabilities if version else None) or {}
    punct_supported = caps.get("punct_supported")
    if punct_supported is None:
        punct_supported = bool(caps.get("punctuation", False))

    punct_rows = [
        score_punctuation(
            ref_text or "",
            case.text or "",
            drop_tags=drop_tags,
            drop_fillers=drop_fillers,
            lowercase=lowercase,
            traditional=traditional,
        )
        for case, ref_text in rows
    ]
    punct_by_target: dict[uuid.UUID, list[tuple[CaseResult, Any]]] = {}
    for (case, _ref_text), punct_result in zip(rows, punct_rows, strict=True):
        punct_by_target.setdefault(case.run_target_id, []).append((case, punct_result))
    hyp_punct_total = sum(row.hyp_punct for row in punct_rows)
    punct_config = profile.params.get("punct")
    punct_enabled = not isinstance(punct_config, dict) or punct_config.get("enabled") is not False
    punct_global_reason = (
        "metric_disabled"
        if not punct_enabled
        else "no_sentence_punctuation"
        if not punct_supported
        else "device_no_punctuation_output"
        if hyp_punct_total == 0
        else None
    )

    for (case, _ref_text), punct_result in zip(rows, punct_rows, strict=True):
        stored_score = case_scores.get(case.id)
        if stored_score is None:
            continue
        target_results = punct_by_target.get(case.run_target_id, [])
        target_has_punctuation = any(result.hyp_punct > 0 for _, result in target_results)
        case_na_reason = punct_global_reason or (
            "device_no_punctuation_output" if not target_has_punctuation else None
        )
        if case_na_reason is not None:
            punct_metric: dict[str, object] = {"status": "na", "reason": case_na_reason}
        elif punct_result.ref_punct == 0 and punct_result.hyp_punct == 0:
            punct_metric = {"status": "na", "reason": "no_sentence_punctuation"}
        else:
            denominator = 2 * punct_result.tp + punct_result.fp + punct_result.fn
            punct_metric = {
                "status": "ok",
                "value": (2 * punct_result.tp / denominator) if denominator else 0.0,
                "tp": punct_result.tp,
                "fp": punct_result.fp,
                "fn": punct_result.fn,
                "sentence_end_acc": (
                    punct_result.end_correct / punct_result.end_total
                    if punct_result.end_total
                    else None
                ),
            }
        stored_score.metrics = {**stored_score.metrics, "punct_f1": punct_metric}

    def add_punct_metric(
        *,
        run_target_id: uuid.UUID | None,
        subset: str,
        entries: list[tuple[CaseResult, Any]],
        bucket: dict[str, str] | None = None,
        planned_count_override: int | None = None,
    ) -> None:
        """按全局或 target 聚合标点，并用对应计划样本计算覆盖率与 CI。"""
        planned_count = (
            planned_count_override
            if planned_count_override is not None
            else planned
            if run_target_id is None
            else int(target_planned_cases.get(run_target_id, 0))
        )
        all_target_entries = (
            [(case, result) for (case, _), result in zip(rows, punct_rows, strict=True)]
            if run_target_id is None
            else punct_by_target.get(run_target_id, [])
        )
        target_has_output = any(result.hyp_punct > 0 for _, result in all_target_entries)
        hard_reason = (
            punct_global_reason
            if punct_global_reason in {"metric_disabled", "no_sentence_punctuation"}
            else "device_no_punctuation_output"
            if not target_has_output
            else None
        )
        scoreable_rows = [
            result for _, result in entries if result.ref_punct > 0 or result.hyp_punct > 0
        ]
        if hard_reason is not None:
            add_metric_value(
                run_target_id=run_target_id,
                subset=subset,
                value=None,
                n=0,
                status="na",
                reason=hard_reason,
                metric="punct_f1",
                detail={"scoreable_cases": 0, "planned_cases": planned_count},
                bucket=bucket,
            )
            return
        if not scoreable_rows:
            add_metric_value(
                run_target_id=run_target_id,
                subset=subset,
                value=None,
                n=0,
                status="na",
                reason="no_sentence_punctuation",
                metric="punct_f1",
                detail={"scoreable_cases": 0, "planned_cases": planned_count},
                bucket=bucket,
            )
            return

        summary = punct_aggregate(scoreable_rows)
        status, reason = coverage_state(len(scoreable_rows), planned_count)
        add_metric_value(
            run_target_id=run_target_id,
            subset=subset,
            value=summary["f1"],
            n=len(scoreable_rows),
            status=status,
            reason=reason,
            metric="punct_f1",
            detail={
                "precision": summary["precision"],
                "recall": summary["recall"],
                "sentence_end_acc": summary["sentence_end_acc"],
                "tp": summary["tp"],
                "fp": summary["fp"],
                "fn": summary["fn"],
                "scoreable_cases": len(scoreable_rows),
                "planned_cases": planned_count,
                "coverage": len(scoreable_rows) / planned_count if planned_count else 0.0,
            },
            ci=f1_ci([(row.tp, row.fp, row.fn) for row in scoreable_rows]),
            bucket=bucket,
        )

    add_punct_metric(
        run_target_id=None,
        subset="all",
        entries=[(case, result) for (case, _), result in zip(rows, punct_rows, strict=True)],
    )
    for target_id, subset in target_subsets.items():
        add_punct_metric(
            run_target_id=target_id,
            subset=str(subset),
            entries=punct_by_target.get(target_id, []),
        )
    for bucket_name, planned_count in planned_cases_by_bucket.items():
        bucket_entries = [
            (case, result)
            for (case, _), result in zip(rows, punct_rows, strict=True)
            if case_duration_buckets.get(case.id) == bucket_name
        ]
        add_punct_metric(
            run_target_id=None,
            subset="all",
            entries=bucket_entries,
            bucket={"duration_bucket": bucket_name},
            planned_count_override=planned_count,
        )
        for target_id, subset in target_subsets.items():
            target_entries = [
                (case, result)
                for case, result in punct_by_target.get(target_id, [])
                if case_duration_buckets.get(case.id) == bucket_name
            ]
            add_punct_metric(
                run_target_id=target_id,
                subset=str(subset),
                entries=target_entries,
                bucket={"duration_bucket": bucket_name},
                planned_count_override=target_planned_cases_by_bucket.get(
                    (target_id, bucket_name), 0
                ),
            )

    # ITN 准确率（M5 §5.4）：需要 RefText(kind=itn) 金标，否则 na
    itn_rows = []
    itn_missing = 0
    itn_case_rows: dict[int, ItnResult] = {}
    itn_by_target: dict[uuid.UUID, list[ItnResult]] = {}
    itn_reference_cases_by_target: dict[uuid.UUID, int] = {}
    itn_by_bucket: dict[str, list[ItnResult]] = {}
    itn_by_target_bucket: dict[tuple[uuid.UUID, str], list[ItnResult]] = {}
    itn_reference_cases_by_bucket: dict[str, int] = {}
    itn_reference_cases_by_target_bucket: dict[tuple[uuid.UUID, str], int] = {}
    reference_rows = db.execute(
        select(RefText.item_id, RefText.text, Item.lang, Item.buckets)
        .join(Item, Item.id == RefText.item_id)
        .where(RefText.item_id.in_(item_ids), RefText.kind == "itn")
    ).all()
    itn_references: dict[int, tuple[str, str]] = {}
    for item_id, ref_content, item_language, buckets in reference_rows:
        bucket_language = (buckets or {}).get("language")
        language = item_language or bucket_language
        itn_references[int(item_id)] = (
            str(ref_content),
            str(language) if isinstance(language, str) else "auto",
        )
    for case, _ref_text in rows:
        reference = itn_references.get(case.item_id)
        if reference is None:
            itn_missing += 1
            continue
        itn_reference_cases_by_target[case.run_target_id] = (
            itn_reference_cases_by_target.get(case.run_target_id, 0) + 1
        )
        duration_bucket_name = case_duration_buckets.get(case.id)
        if duration_bucket_name is not None:
            itn_reference_cases_by_bucket[duration_bucket_name] = (
                itn_reference_cases_by_bucket.get(duration_bucket_name, 0) + 1
            )
            target_bucket_key = (case.run_target_id, duration_bucket_name)
            itn_reference_cases_by_target_bucket[target_bucket_key] = (
                itn_reference_cases_by_target_bucket.get(target_bucket_key, 0) + 1
            )
        reference_text, language = reference
        itn_result = score_itn(
            reference_text,
            case.text or "",
            language=language,
            drop_tags=drop_tags,
            drop_fillers=drop_fillers,
            traditional=traditional,
        )
        itn_rows.append(itn_result)
        itn_by_target.setdefault(case.run_target_id, []).append(itn_result)
        if duration_bucket_name is not None:
            itn_by_bucket.setdefault(duration_bucket_name, []).append(itn_result)
            itn_by_target_bucket.setdefault((case.run_target_id, duration_bucket_name), []).append(
                itn_result
            )
        itn_case_rows[case.id] = itn_result

    raw_itn_ratio = caps.get("itn_ratio", 0)
    itn_ratio = (
        float(raw_itn_ratio)
        if isinstance(raw_itn_ratio, int | float) and not isinstance(raw_itn_ratio, bool)
        else 0.0
    )
    itn_config = profile.params.get("itn")
    itn_enabled = not isinstance(itn_config, dict) or itn_config.get("enabled") is not False
    scoreable_itn_cases = sum(1 for result in itn_rows if result.total > 0)
    itn_global_reason = (
        "metric_disabled"
        if not itn_enabled
        else "no_itn_reference"
        if not itn_rows or itn_ratio < 0.95
        else "no_itn_entities"
        if scoreable_itn_cases == 0
        else None
    )
    for case, _ref_text in rows:
        stored_score = case_scores.get(case.id)
        if stored_score is None:
            continue
        if itn_global_reason in {"metric_disabled", "no_itn_reference"}:
            itn_metric: dict[str, object] = {
                "status": "na",
                "reason": itn_global_reason,
            }
        else:
            stored_itn_result = itn_case_rows.get(case.id)
            if stored_itn_result is None:
                itn_metric = {"status": "na", "reason": "no_itn_reference"}
            elif stored_itn_result.total == 0:
                itn_metric = {"status": "na", "reason": "no_itn_entities"}
            else:
                itn_metric = {
                    "status": "ok",
                    "value": stored_itn_result.correct / stored_itn_result.total,
                    "correct": stored_itn_result.correct,
                    "total": stored_itn_result.total,
                    "by_type": stored_itn_result.by_type or {},
                }
        stored_score.metrics = {**stored_score.metrics, "itn_acc": itn_metric}

    def add_itn_metric(
        *,
        run_target_id: uuid.UUID | None,
        subset: str,
        entries: list[ItnResult],
        bucket: dict[str, str] | None = None,
        planned_count_override: int | None = None,
        reference_count_override: int | None = None,
    ) -> None:
        """按全局或 target 聚合 ITN，并按计划 case 数标记覆盖率。"""
        planned_count = (
            planned_count_override
            if planned_count_override is not None
            else planned
            if run_target_id is None
            else int(target_planned_cases.get(run_target_id, 0))
        )
        reference_count = (
            reference_count_override
            if reference_count_override is not None
            else len(itn_rows)
            if run_target_id is None
            else itn_reference_cases_by_target.get(run_target_id, 0)
        )
        missing_count = max(planned_count - reference_count, 0)
        scoreable = [result for result in entries if result.total > 0]
        hard_reason = (
            "metric_disabled"
            if not itn_enabled
            else "no_itn_reference"
            if itn_ratio < 0.95 or reference_count == 0
            else "no_itn_entities"
            if not scoreable
            else None
        )
        if hard_reason is not None:
            add_metric_value(
                run_target_id=run_target_id,
                subset=subset,
                value=None,
                n=0,
                status="na",
                reason=hard_reason,
                metric="itn_acc",
                detail={
                    "missing_cases": missing_count,
                    "scoreable_cases": 0,
                    "planned_cases": planned_count,
                },
                bucket=bucket,
            )
            return

        summary = itn_aggregate(scoreable)
        status, reason = coverage_state(len(scoreable), planned_count)
        add_metric_value(
            run_target_id=run_target_id,
            subset=subset,
            value=summary["value"],
            n=summary["n"],
            status=status,
            reason=reason,
            metric="itn_acc",
            detail={
                "correct": summary["correct"],
                "total": summary["total"],
                "by_type": summary["by_type"],
                "missing_cases": missing_count,
                "scoreable_cases": len(scoreable),
                "planned_cases": planned_count,
                "coverage": len(scoreable) / planned_count if planned_count else 0.0,
            },
            ci=ratio_ci([(row.correct, row.total) for row in scoreable]),
            bucket=bucket,
        )

    add_itn_metric(run_target_id=None, subset="all", entries=itn_rows)
    for target_id, subset in target_subsets.items():
        add_itn_metric(
            run_target_id=target_id,
            subset=str(subset),
            entries=itn_by_target.get(target_id, []),
        )
    for bucket_name, planned_count in planned_cases_by_bucket.items():
        add_itn_metric(
            run_target_id=None,
            subset="all",
            entries=itn_by_bucket.get(bucket_name, []),
            bucket={"duration_bucket": bucket_name},
            planned_count_override=planned_count,
            reference_count_override=itn_reference_cases_by_bucket.get(bucket_name, 0),
        )
        for target_id, subset in target_subsets.items():
            target_bucket_key = (target_id, bucket_name)
            add_itn_metric(
                run_target_id=target_id,
                subset=str(subset),
                entries=itn_by_target_bucket.get(target_bucket_key, []),
                bucket={"duration_bucket": bucket_name},
                planned_count_override=target_planned_cases_by_bucket.get(target_bucket_key, 0),
                reference_count_override=itn_reference_cases_by_target_bucket.get(
                    target_bucket_key, 0
                ),
            )

    # 无独立 ITN 金标时，保留 CER 对齐中数字相关字符的误差作为近似信号（M5 §5.4）。
    numeric_by_target: dict[uuid.UUID, list[NumericCerResult]] = {}
    numeric_by_bucket: dict[str, list[NumericCerResult]] = {}
    numeric_by_target_bucket: dict[tuple[uuid.UUID, str], list[NumericCerResult]] = {}
    for case, numeric_reference in rows:
        case_score = case_scores.get(case.id)
        if case_score is None:
            continue
        case_metrics = dict(case_score.metrics)
        if numeric_reference is None:
            case_metrics["numeric_cer"] = {"status": "na", "reason": "no_reference"}
            case_score.metrics = case_metrics
            continue

        numeric_result = numeric_cer(
            numeric_reference,
            case.text or "",
            strip_sc=strip_sc,
            strip_punct=strip_punct,
            strip_space=strip_space,
            lowercase=lowercase,
            drop_tags=drop_tags,
            drop_fillers=drop_fillers,
            traditional=traditional,
        )
        if numeric_result.value is None:
            case_metrics["numeric_cer"] = {"status": "na", "reason": "no_numeric_reference"}
        else:
            case_metrics["numeric_cer"] = {
                "status": "ok",
                "value": numeric_result.value,
                "s": numeric_result.s,
                "d": numeric_result.d,
                "i": numeric_result.i,
                "edits": numeric_result.edits,
                "n": numeric_result.n,
                "approximate": True,
            }
            numeric_by_target.setdefault(case.run_target_id, []).append(numeric_result)
            duration_bucket_name = case_duration_buckets.get(case.id)
            if duration_bucket_name is not None:
                numeric_by_bucket.setdefault(duration_bucket_name, []).append(numeric_result)
                numeric_by_target_bucket.setdefault(
                    (case.run_target_id, duration_bucket_name), []
                ).append(numeric_result)
        case_score.metrics = case_metrics

    def add_numeric_metric(
        *,
        run_target_id: uuid.UUID | None,
        subset: str,
        entries: list[NumericCerResult],
        planned_count: int,
        bucket: dict[str, str] | None = None,
    ) -> float | None:
        """聚合数字相关 CER；计划分母只统计参考含数字字符的样本。"""
        scoreable = [entry for entry in entries if entry.n > 0]
        numeric_chars = sum(entry.n for entry in scoreable)
        edits = sum(entry.edits for entry in scoreable)
        value = edits / numeric_chars if numeric_chars else None
        status: str
        reason: str | None
        if planned_count == 0:
            status, reason = "na", "no_numeric_reference"
        else:
            status, reason = coverage_state(len(scoreable), planned_count)
        add_metric_value(
            run_target_id=run_target_id,
            subset=subset,
            value=value,
            n=len(scoreable),
            status=status if value is not None else "na",
            reason=reason if value is not None else (reason or "no_numeric_reference"),
            metric="numeric_cer",
            detail={
                "edits": edits,
                "numeric_characters": numeric_chars,
                "scoreable_cases": len(scoreable),
                "planned_cases": planned_count,
                "missing_cases": max(planned_count - len(scoreable), 0),
                "coverage": len(scoreable) / planned_count if planned_count else 0.0,
                "approximate": True,
            },
            ci=ratio_ci([(entry.edits, entry.n) for entry in scoreable]),
            bucket=bucket,
        )
        return value

    numeric_value = add_numeric_metric(
        run_target_id=None,
        subset="all",
        entries=[entry for values in numeric_by_target.values() for entry in values],
        planned_count=sum(planned_numeric_cases_by_target.values()),
    )
    evaluation.metrics = {**evaluation.metrics, "numeric_cer": numeric_value}
    for target_id, subset in target_subsets.items():
        add_numeric_metric(
            run_target_id=target_id,
            subset=str(subset),
            entries=numeric_by_target.get(target_id, []),
            planned_count=planned_numeric_cases_by_target.get(target_id, 0),
        )
    for bucket_name, planned_count in planned_numeric_cases_by_bucket.items():
        add_numeric_metric(
            run_target_id=None,
            subset="all",
            entries=numeric_by_bucket.get(bucket_name, []),
            planned_count=planned_count,
            bucket={"duration_bucket": bucket_name},
        )
        for target_id, subset in target_subsets.items():
            target_bucket_key = (target_id, bucket_name)
            add_numeric_metric(
                run_target_id=target_id,
                subset=str(subset),
                entries=numeric_by_target_bucket.get(target_bucket_key, []),
                planned_count=target_planned_numeric_cases_by_bucket.get(target_bucket_key, 0),
                bucket={"duration_bucket": bucket_name},
            )

    # 性能统计排除缓存样本；计划分母也排除缓存命中，失败转写仍保留在分母中。
    perf_rows = db.execute(
        select(CaseResult, Item.duration_ms, Hypothesis.mode, Hypothesis.usage)
        .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
        .join(Item, Item.id == CaseResult.item_id)
        .outerjoin(Hypothesis, Hypothesis.id == CaseResult.hypothesis_id)
        .where(RunTarget.run_id == run.id)
    ).all()
    perf_cases: list[dict[str, Any]] = []
    for case, duration_ms, mode, usage in perf_rows:
        perf_cases.append(
            {
                "target_id": case.run_target_id,
                "subset": str(target_subsets.get(case.run_target_id, "all")),
                "bucket": duration_bucket(int(duration_ms)),
                "status": case.status,
                "mode": str(mode or ""),
                "duration_ms": int(duration_ms),
                "wall_ms": case.wall_ms,
                "server_ms": case.server_ms,
                "ttft_ms": case.ttft_ms,
                "finalize_ms": case.finalize_ms,
                "rtf_dev": case.rtf_dev,
                "rtf_e2e": case.rtf_e2e,
                "resource": case.resource
                if isinstance(case.resource, dict) and case.resource
                else (dict(usage) if isinstance(usage, dict) else {}),
            }
        )

    def add_perf_group(
        *,
        run_target_id: uuid.UUID | None,
        subset: str,
        bucket: dict[str, str],
        entries: list[dict[str, Any]],
        planned_count: int,
    ) -> None:
        """将当前范围的性能中位数、P95 与吞吐落入 metric_value。"""
        measured = [entry for entry in entries if entry["status"] == "ok"]
        metric_values: dict[str, list[float]] = {
            "ttft_ms": [],
            "finalize_ms": [],
            "rtf_dev": [],
            "rtf_e2e": [],
            "rtf_realtime": [],
            "overhead_ratio": [],
        }
        for entry in measured:
            mode = entry["mode"]
            for metric_name, field_name in (("rtf_dev", "rtf_dev"), ("rtf_e2e", "rtf_e2e")):
                value = entry[field_name]
                mode_ok = metric_name != "rtf_e2e" or mode in {"burst", "batch"}
                if isinstance(value, int | float) and mode_ok:
                    metric_values[metric_name].append(float(value))
            if mode == "stream" and isinstance(entry["ttft_ms"], int | float):
                metric_values["ttft_ms"].append(float(entry["ttft_ms"]))
            if mode in {"1x", "realtime"}:
                finalize_ms = entry["finalize_ms"]
                duration_ms = entry["duration_ms"]
                if isinstance(finalize_ms, int | float):
                    metric_values["finalize_ms"].append(float(finalize_ms))
                    if duration_ms > 0:
                        metric_values["rtf_realtime"].append(finalize_ms / duration_ms)
            perf_resource = entry["resource"]
            if isinstance(perf_resource, dict):
                total_cost = perf_resource.get("total_cost_ms")
                inference = perf_resource.get("inference_ms", entry["server_ms"])
                duration_ms = entry["duration_ms"]
                if (
                    isinstance(total_cost, int | float)
                    and isinstance(inference, int | float)
                    and duration_ms > 0
                ):
                    metric_values["overhead_ratio"].append(
                        (float(total_cost) - float(inference)) / duration_ms
                    )

        no_data_reasons = {
            "ttft_ms": "no_partial_timing",
            "finalize_ms": "no_realtime_timing",
            "rtf_dev": "no_inference_timing",
            "rtf_e2e": "no_batch_timing",
            "rtf_realtime": "no_realtime_results",
            "overhead_ratio": "cost_data_unavailable",
        }
        for metric_name, values in metric_values.items():
            if values:
                value, p95 = summarize_latency(values)
                status, reason = coverage_state(len(values), planned_count)
                detail: dict[str, object] = {
                    "median": value,
                    "p95": p95,
                    "unit": "ms" if metric_name.endswith("_ms") else "ratio",
                    "scoreable_cases": len(values),
                    "planned_cases": planned_count,
                }
                add_metric_value(
                    run_target_id=run_target_id,
                    subset=subset,
                    value=value,
                    n=len(values),
                    status=status,
                    reason=reason,
                    metric=metric_name,
                    detail=detail,
                    bucket=bucket,
                )
            else:
                add_metric_value(
                    run_target_id=run_target_id,
                    subset=subset,
                    value=None,
                    n=0,
                    status="na",
                    reason=no_data_reasons[metric_name],
                    metric=metric_name,
                    detail={"scoreable_cases": 0, "planned_cases": planned_count},
                    bucket=bucket,
                )

        throughput_rows = [
            entry
            for entry in measured
            if entry["duration_ms"] > 0
            and isinstance(entry["wall_ms"], int | float)
            and entry["wall_ms"] > 0
        ]
        throughput_value = throughput(
            [int(entry["duration_ms"]) for entry in throughput_rows],
            [int(entry["wall_ms"]) for entry in throughput_rows],
        )
        throughput_status, throughput_reason = coverage_state(len(throughput_rows), planned_count)
        add_metric_value(
            run_target_id=run_target_id,
            subset=subset,
            value=throughput_value,
            n=len(throughput_rows),
            status=throughput_status if throughput_value is not None else "na",
            reason=throughput_reason if throughput_value is not None else "no_wall_timing",
            metric="throughput",
            detail={
                "audio_ms": sum(int(entry["duration_ms"]) for entry in throughput_rows),
                "wall_ms": sum(int(entry["wall_ms"]) for entry in throughput_rows),
                "unit": "x_realtime",
                "planned_cases": planned_count,
            },
            bucket=bucket,
        )

    perf_targets: list[tuple[uuid.UUID | None, str]] = [
        (None, "all"),
        *[(target_id, str(subset)) for target_id, subset in target_subsets.items()],
    ]
    if not perf_enabled:
        # 口径关闭性能指标：不写 ttft/rtf/throughput，报告不会出现“有值但无意义”的读数。
        perf_targets = []
    for perf_target_id, subset in perf_targets:
        scoped = [
            entry
            for entry in perf_cases
            if perf_target_id is None or entry["target_id"] == perf_target_id
        ]
        planned_scoped = [entry for entry in scoped if entry["status"] != "cached"]
        add_perf_group(
            run_target_id=perf_target_id,
            subset=subset,
            bucket={},
            entries=planned_scoped,
            planned_count=len(planned_scoped),
        )
        perf_bucket_names = sorted({str(entry["bucket"]) for entry in planned_scoped})
        for bucket_name in perf_bucket_names:
            bucket_rows = [entry for entry in planned_scoped if entry["bucket"] == bucket_name]
            add_perf_group(
                run_target_id=perf_target_id,
                subset=subset,
                bucket={"duration_bucket": bucket_name},
                entries=bucket_rows,
                planned_count=len(bucket_rows),
            )

    if cancellation_requested():
        return finish_cancelled()
    evaluation.status = "completed"
    evaluation.finished_at = datetime.now(UTC)
    evaluation.progress = {
        "phase": "completed",
        "done": progress_total,
        "total": progress_total,
        "last_case_result_id": resume_cursor,
    }
    db.commit()
    db.refresh(evaluation)
    return evaluation


def _device_name(devices: dict[uuid.UUID, Device | None], target_id: uuid.UUID) -> str | None:
    device = devices.get(target_id)
    return device.name if device else None


def run_summary(
    db: Session,
    run: Run,
    *,
    evaluation_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """给 CLI/API 的统一摘要。"""
    targets = db.scalars(
        select(RunTarget).where(RunTarget.run_id == run.id).order_by(RunTarget.id)
    ).all()
    target_devices = {item.id: db.get(Device, item.device_id) for item in targets}
    evaluation_statement = select(Evaluation).where(Evaluation.run_id == run.id)
    if evaluation_id is not None:
        evaluation_statement = evaluation_statement.where(Evaluation.id == evaluation_id)
    else:
        evaluation_statement = evaluation_statement.order_by(
            Evaluation.created_at.desc(), Evaluation.id.desc()
        )
    evaluation = db.scalar(evaluation_statement)
    metrics = []
    if evaluation is not None:
        # 摘要只展示 evaluation 级聚合（run_target_id 为空）；目标级在矩阵端点（M5 §4）
        values = db.scalars(
            select(MetricValue).where(
                MetricValue.evaluation_id == evaluation.id,
                MetricValue.run_target_id.is_(None),
            )
        ).all()
        metrics = [
            {
                "metric": item.metric,
                "subset": item.subset,
                "scope": item.scope,
                "value": item.value,
                "n": item.n,
                "status": item.status,
                "reason": item.reason,
                "detail": item.detail,
                "ci_low": item.ci_low,
                "ci_high": item.ci_high,
            }
            for item in values
            if not item.bucket
        ]
    return {
        "run": {
            "id": str(run.id),
            "name": run.name,
            "status": run.status,
            "group_id": str(run.group_id) if run.group_id else None,
            "progress": run.progress,
            "snapshot": run.snapshot,
            # ETA 必须带出处与置信度（M4 §4/§8）
            "eta_ms": run.eta_ms,
            "eta_p90_ms": run.eta_p90_ms,
            "eta_source": run.eta_source,
            "eta_confidence": run.eta_confidence,
        },
        "targets": [
            {
                "id": str(item.id),
                "device_id": str(item.device_id),
                "device_name": _device_name(target_devices, item.id),
                "subset": item.subset,
                "status": item.status,
                "abort_reason": item.abort_reason,
                "stats": item.stats,
            }
            for item in targets
        ],
        "evaluation": {
            "id": str(evaluation.id),
            "status": evaluation.status,
            "scorer_version": evaluation.scorer_version,
            "trigger": evaluation.trigger,
            "created_at": evaluation.created_at.isoformat() if evaluation.created_at else None,
            "progress": evaluation.progress,
            "coverage": evaluation.coverage,
            "metrics": evaluation.metrics,
        }
        if evaluation
        else None,
        "metrics": metrics,
    }


def run_matrix(
    db: Session,
    run: Run,
    *,
    evaluation_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """按 target 聚合 CER；P1 只有 CER，其它指标占位 na。"""
    evaluation_statement = select(Evaluation).where(Evaluation.run_id == run.id)
    if evaluation_id is not None:
        evaluation_statement = evaluation_statement.where(Evaluation.id == evaluation_id)
    else:
        evaluation_statement = evaluation_statement.order_by(
            Evaluation.created_at.desc(), Evaluation.id.desc()
        )
    evaluation = db.scalar(evaluation_statement)
    rows = db.execute(
        select(RunTarget, Device)
        .join(Device, Device.id == RunTarget.device_id)
        .where(RunTarget.run_id == run.id)
        .order_by(RunTarget.id)
    ).all()

    result = []
    for target, device in rows:
        s = d = i = 0
        ref_len = 0
        n = 0
        if evaluation is not None:
            # 新数据是嵌套结构 metrics.cer.{s,d,i,n}（M5 §2.1）；
            # 旧数据是扁平 {cer, edits, ref_len}，用 coalesce 兼容历史 Run
            aggregate = db.execute(
                select(
                    func.coalesce(
                        func.sum(
                            func.coalesce(
                                cast(CaseScore.metrics["cer"]["s"].astext, Integer),
                                cast(CaseScore.metrics["edits"].astext, Integer),
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            func.coalesce(cast(CaseScore.metrics["cer"]["d"].astext, Integer), 0)
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            func.coalesce(cast(CaseScore.metrics["cer"]["i"].astext, Integer), 0)
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            func.coalesce(
                                cast(CaseScore.metrics["cer"]["n"].astext, Integer),
                                cast(CaseScore.metrics["ref_len"].astext, Integer),
                            )
                        ),
                        0,
                    ),
                    func.count(),
                )
                .join(CaseResult, CaseResult.id == CaseScore.case_result_id)
                .where(
                    CaseScore.evaluation_id == evaluation.id,
                    CaseResult.run_target_id == target.id,
                )
            ).one()
            s, d, i, ref_len, n = (int(value) for value in aggregate)
        edits = s + d + i

        result.append(
            {
                "target_id": str(target.id),
                "device_id": str(device.id),
                "device_name": device.name,
                "subset": target.subset,
                "status": target.status,
                "cer": (edits / ref_len) if ref_len else None,
                "n": n,
            }
        )
    return {
        "run_id": str(run.id),
        "evaluation_id": str(evaluation.id) if evaluation else None,
        "rows": result,
    }
