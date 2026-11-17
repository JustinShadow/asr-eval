"""Evaluation 横评：可比性、指标矩阵与配对显著性（M6 §3/§5）。"""

from __future__ import annotations

import json
import random
import uuid
from typing import Any, Literal

from sqlalchemy import Float, Integer, and_, cast, func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domain.eta import duration_bucket
from app.domain.models import (
    Benchmark,
    CaseResult,
    CaseScore,
    Device,
    Evaluation,
    Item,
    MetricValue,
    Run,
    RunTarget,
)

PairBy = Literal["content_uid", "pair_key", "none"]
_ELIGIBLE_EVALUATION_STATUSES = {"completed", "degraded"}


def _canonical(value: object) -> str:
    """用稳定序列化比较 JSONB 口径与采样参数。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compare_evaluations(
    db: Session,
    evaluation_ids: list[uuid.UUID],
    *,
    pair_by: PairBy = "content_uid",
) -> dict[str, Any]:
    """检查 2～4 个 Evaluation 的可比性，并返回带口径信息的指标矩阵。"""
    if not 2 <= len(evaluation_ids) <= 4:
        raise ApiError("E_SEMANTIC", "对比需要 2～4 个 Evaluation", status_code=422)
    if len(set(evaluation_ids)) != len(evaluation_ids):
        raise ApiError("E_SEMANTIC", "Evaluation 不能重复", status_code=422)
    if pair_by not in {"content_uid", "pair_key", "none"}:
        raise ApiError("E_VALIDATION", "不支持的配对方式", status_code=422)

    entries: list[dict[str, Any]] = []
    target_keys_by_eval: dict[str, set[tuple[str, str]]] = {}
    for evaluation_id in evaluation_ids:
        evaluation = db.get(Evaluation, evaluation_id)
        if evaluation is None:
            raise ApiError("E_NOT_FOUND", f"Evaluation {evaluation_id} 不存在", status_code=404)
        run = db.get(Run, evaluation.run_id)
        benchmark = db.get(Benchmark, run.benchmark_id) if run else None
        targets = []
        if run is not None:
            for target, device in db.execute(
                select(RunTarget, Device)
                .join(Device, Device.id == RunTarget.device_id)
                .where(RunTarget.run_id == run.id)
                .order_by(Device.name, RunTarget.subset)
            ).all():
                targets.append(
                    {
                        "id": str(target.id),
                        "device_id": str(device.id),
                        "device_name": device.name,
                        "subset": target.subset,
                        "status": target.status,
                    }
                )
        subsets = sorted({target["subset"] for target in targets})
        target_keys_by_eval[str(evaluation.id)] = {
            (target["device_id"], target["subset"]) for target in targets
        }
        entries.append(
            {
                "id": str(evaluation.id),
                "run_id": str(evaluation.run_id),
                "run_name": run.name if run else "—",
                "run_status": run.status if run else "missing",
                "targets": targets,
                "devices": sorted({target["device_name"] for target in targets}),
                "subsets": subsets,
                "profile_hash": evaluation.profile_hash,
                "scorer_version": evaluation.scorer_version,
                "created_at": evaluation.created_at.isoformat() if evaluation.created_at else None,
                "dataset_version_id": str(benchmark.dataset_version_id) if benchmark else None,
                "benchmark_version_hash": benchmark.version_hash if benchmark else None,
                "sampling": benchmark.sampling if benchmark else {},
                "bucket_rules": benchmark.bucket_rules if benchmark else {},
                "status": evaluation.status,
                "coverage": evaluation.coverage,
                "metrics": evaluation.metrics,
            }
        )

    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def add_issue(
        destination: list[dict[str, Any]], code: str, message: str, ids: list[str]
    ) -> None:
        destination.append({"code": code, "message": message, "evaluation_ids": ids})

    incomplete = [
        entry["id"] for entry in entries if entry["status"] not in _ELIGIBLE_EVALUATION_STATUSES
    ]
    if incomplete:
        add_issue(blockers, "E_EVAL_NOT_DONE", "存在未完成的评分，无法对比", incomplete)
    aborted = [
        entry["id"]
        for entry in entries
        if entry["run_status"] == "aborted_condition_changed"
        or any(target["status"] == "aborted_condition_changed" for target in entry["targets"])
    ]
    if aborted:
        add_issue(blockers, "E_RUN_ABORTED", "设备或模型条件发生变化的 Run 不参与对比", aborted)

    profile_mismatch = len({entry["profile_hash"] for entry in entries}) > 1
    if profile_mismatch:
        add_issue(
            blockers,
            "E_PROFILE_MISMATCH",
            "口径不一致，无法直接比较",
            [entry["id"] for entry in entries],
        )
    scorer_mismatch = len({entry["scorer_version"] for entry in entries}) > 1
    if scorer_mismatch:
        add_issue(
            blockers,
            "E_SCORER_MISMATCH",
            "评分器版本不一致，无法直接比较",
            [entry["id"] for entry in entries],
        )

    benchmark_hashes = [entry["benchmark_version_hash"] for entry in entries]
    if any(value is not None for value in benchmark_hashes):
        benchmark_mismatch = len(set(benchmark_hashes)) > 1
    else:
        benchmark_signatures = {
            (
                entry["dataset_version_id"],
                _canonical(entry["sampling"]),
                _canonical(entry["bucket_rules"]),
            )
            for entry in entries
        }
        benchmark_mismatch = len(benchmark_signatures) > 1
    if benchmark_mismatch:
        add_issue(
            blockers,
            "E_BENCHMARK_MISMATCH",
            "评测定义或样本版本不一致，无法直接比较",
            [entry["id"] for entry in entries],
        )

    subset_sets = [set(entry["subsets"]) for entry in entries]
    has_subsets = any(subset_sets)
    common_subsets = sorted(set.intersection(*subset_sets)) if subset_sets else []
    subset_union = set.union(*subset_sets) if subset_sets else set()
    if has_subsets and subset_union != set(common_subsets):
        add_issue(
            warnings,
            "W_SUBSET_COVERAGE",
            "各评分的 subset 不完全一致；矩阵标出缺失单元格",
            [entry["id"] for entry in entries],
        )
    if pair_by == "content_uid" and has_subsets and not common_subsets:
        add_issue(
            blockers,
            "E_NO_COMMON_SUBSET",
            "没有共同 subset，无法按 content_uid 配对",
            [entry["id"] for entry in entries],
        )

    degraded = [
        entry["id"]
        for entry in entries
        if entry["status"] == "degraded"
        or (
            isinstance(entry["coverage"], dict)
            and int(entry["coverage"].get("scored", 0)) < int(entry["coverage"].get("planned", 0))
        )
        or entry["run_status"] in {"partial", "failed"}
    ]
    if degraded:
        add_issue(
            warnings,
            "W_PARTIAL_COVERAGE",
            "存在部分覆盖，比较结果需结合样本量判断",
            degraded,
        )

    row_values: dict[tuple[str | None, str, str, str, str], dict[str, dict[str, Any]]] = {}
    row_meta: dict[tuple[str | None, str, str, str, str], dict[str, Any]] = {}
    if evaluation_ids:
        statement = (
            select(MetricValue, RunTarget, Device)
            .outerjoin(RunTarget, RunTarget.id == MetricValue.run_target_id)
            .outerjoin(Device, Device.id == RunTarget.device_id)
            .where(MetricValue.evaluation_id.in_(evaluation_ids))
            .order_by(MetricValue.metric, MetricValue.scope, MetricValue.subset, MetricValue.id)
        )
        for metric_value, target, device in db.execute(statement).all():
            target_key = str(device.id) if target is not None and device is not None else None
            target_name = (
                device.name if target is not None and device is not None else "Evaluation 总计"
            )
            bucket_json = _canonical(metric_value.bucket or {})
            row_key: tuple[str | None, str, str, str, str] = (
                target_key,
                metric_value.subset,
                metric_value.metric,
                metric_value.scope,
                bucket_json,
            )
            row_meta[row_key] = {
                "target_id": target_key,
                "target": target_name,
                "subset": metric_value.subset,
                "metric": metric_value.metric,
                "scope": metric_value.scope,
                "bucket": metric_value.bucket or {},
            }
            row_values.setdefault(row_key, {})[str(metric_value.evaluation_id)] = {
                "evaluation_id": str(metric_value.evaluation_id),
                "value": metric_value.value,
                "n": metric_value.n,
                "ci_low": metric_value.ci_low,
                "ci_high": metric_value.ci_high,
                "status": metric_value.status,
                "reason": metric_value.reason,
            }

    rows: list[dict[str, Any]] = []
    for row_key in sorted(
        row_meta,
        key=lambda key: (
            key[2],
            key[3],
            key[1],
            key[0] or "",
            key[4],
        ),
    ):
        meta = row_meta[row_key]
        cells = row_values[row_key]
        values = []
        for entry in entries:
            evaluation_id = entry["id"]
            cell = cells.get(evaluation_id)
            if cell is None:
                target_id, subset, _metric, _scope, _bucket = row_key
                target_present = (
                    target_id is not None
                    and (
                        target_id,
                        subset,
                    )
                    in target_keys_by_eval[evaluation_id]
                )
                cell = {
                    "evaluation_id": evaluation_id,
                    "value": None,
                    "n": 0,
                    "ci_low": None,
                    "ci_high": None,
                    "status": "na",
                    "reason": (
                        "metric_not_available"
                        if target_present or target_id is None
                        else "target_not_present"
                    ),
                }
            values.append(cell)
        rows.append({**meta, "values": values})

    return {
        "comparable": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "pair_by": pair_by,
        "common_subsets": common_subsets,
        "evaluations": entries,
        "rows": rows,
    }


def _item_units(
    db: Session,
    evaluation_id: uuid.UUID,
    *,
    pair_by: Literal["content_uid", "pair_key"],
    subset: str | None = None,
) -> dict[str, tuple[int, int, str]]:
    """按 content_uid 或 pair_key 聚合 utterance，合并多 target 的重复样本。"""
    key_column = Item.content_uid if pair_by == "content_uid" else Item.pair_key
    status_expr = CaseScore.metrics["cer"]["status"].astext
    value_expr = func.coalesce(
        cast(CaseScore.metrics["cer"]["value"].astext, Float),
        cast(CaseScore.metrics["cer"].astext, Float),
    )
    s_expr = cast(CaseScore.metrics["cer"]["s"].astext, Integer)
    d_expr = cast(CaseScore.metrics["cer"]["d"].astext, Integer)
    i_expr = cast(CaseScore.metrics["cer"]["i"].astext, Integer)
    structured_edits = s_expr + d_expr + i_expr
    edits_expr = func.coalesce(
        structured_edits,
        cast(CaseScore.metrics["edits"].astext, Integer),
        0,
    )
    ref_len_expr = func.coalesce(
        cast(CaseScore.metrics["cer"]["n"].astext, Integer),
        cast(CaseScore.metrics["ref_len"].astext, Integer),
        0,
    )
    statement = (
        select(key_column, edits_expr, ref_len_expr, Item.duration_ms)
        .join(CaseResult, CaseResult.item_id == Item.id)
        .join(CaseScore, CaseScore.case_result_id == CaseResult.id)
        .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
        .where(
            CaseScore.evaluation_id == evaluation_id,
            CaseResult.status.in_(["ok", "cached"]),
            ref_len_expr > 0,
            or_(status_expr == "ok", and_(status_expr.is_(None), value_expr.is_not(None))),
        )
        .order_by(key_column, Item.duration_ms, CaseResult.id)
    )
    if subset is not None:
        statement = statement.where(RunTarget.subset == subset)
    if pair_by == "pair_key":
        statement = statement.where(Item.pair_key.is_not(None))

    grouped: dict[str, list[int | str]] = {}
    for raw_key, edits, ref_len, duration_ms in db.execute(statement).all():
        if raw_key is None or int(ref_len or 0) <= 0:
            continue
        key = str(raw_key)
        current = grouped.get(key)
        if current is None:
            grouped[key] = [int(edits or 0), int(ref_len or 0), duration_bucket(int(duration_ms))]
        else:
            current[0] = int(current[0]) + int(edits or 0)
            current[1] = int(current[1]) + int(ref_len or 0)
    return {
        key: (int(values[0]), int(values[1]), str(values[2])) for key, values in grouped.items()
    }


def _bootstrap_delta(
    pairs: list[tuple[int, int, int, int]],
    *,
    n_resample: int = 1000,
    seed: int = 42,
) -> dict[str, Any]:
    """配对 bootstrap（M6 §5.5）：返回观测 Δ、95% CI 与有限样本双侧 p 值。"""
    valid_pairs = [pair for pair in pairs if pair[1] > 0 and pair[3] > 0]
    if not valid_pairs:
        return {"delta": None, "ci_low": None, "ci_high": None, "p": None, "n": 0}
    if n_resample < 1:
        raise ValueError("n_resample 必须大于 0")

    size = len(valid_pairs)
    observed_base_edits = sum(pair[0] for pair in valid_pairs)
    observed_base_len = sum(pair[1] for pair in valid_pairs)
    observed_other_edits = sum(pair[2] for pair in valid_pairs)
    observed_other_len = sum(pair[3] for pair in valid_pairs)
    observed_delta = observed_base_edits / observed_base_len - (
        observed_other_edits / observed_other_len
    )

    rng = random.Random(seed)  # noqa: S311 - 统计重采样需要可复现，不需要密码学随机
    deltas: list[float] = []
    for _ in range(n_resample):
        base_edits = base_len = other_edits = other_len = 0
        for _ in range(size):
            b_edits, b_len, o_edits, o_len = valid_pairs[rng.randrange(size)]
            base_edits += b_edits
            base_len += b_len
            other_edits += o_edits
            other_len += o_len
        if base_len and other_len:
            deltas.append(base_edits / base_len - other_edits / other_len)
    if not deltas:
        return {"delta": observed_delta, "ci_low": None, "ci_high": None, "p": None, "n": size}

    deltas.sort()
    low = deltas[int(len(deltas) * 0.025)]
    high = deltas[min(len(deltas) - 1, int(len(deltas) * 0.975))]
    below = sum(value <= 0 for value in deltas)
    above = sum(value >= 0 for value in deltas)
    p = min(1.0, 2 * (min(below, above) + 1) / (len(deltas) + 1))
    return {"delta": observed_delta, "ci_low": low, "ci_high": high, "p": p, "n": size}


def _bh_adjust(p_values: list[float]) -> list[float]:
    """Benjamini–Hochberg 校正（M6 D-M6-3），返回与输入顺序一致的 q 值。"""
    count = len(p_values)
    if count == 0:
        return []
    sorted_indices = sorted(range(count), key=lambda index: p_values[index])
    adjusted = [1.0] * count
    previous = 1.0
    for reverse_rank, index in enumerate(reversed(sorted_indices), start=1):
        rank = count - reverse_rank + 1
        adjusted[index] = min(previous, p_values[index] * count / rank)
        previous = adjusted[index]
    return adjusted


def _pairing_subset(
    db: Session,
    evaluation_id: uuid.UUID,
    requested: dict[uuid.UUID, str] | None,
) -> str | None:
    """pair_key 配对时解析单一 subset；多 subset 必须由调用方明确指定。"""
    if requested and evaluation_id in requested:
        subset = requested[evaluation_id]
        run_id = db.scalar(select(Evaluation.run_id).where(Evaluation.id == evaluation_id))
        exists = db.scalar(
            select(RunTarget.id)
            .where(RunTarget.run_id == run_id, RunTarget.subset == subset)
            .limit(1)
        )
        if exists is None:
            raise ApiError("E_VALIDATION", f"Evaluation 不包含 subset：{subset}", status_code=422)
        return subset

    run_id = db.scalar(select(Evaluation.run_id).where(Evaluation.id == evaluation_id))
    subsets = db.scalars(
        select(RunTarget.subset).where(RunTarget.run_id == run_id).distinct()
    ).all()
    if len(subsets) > 1:
        raise ApiError(
            "E_VALIDATION",
            "pair_key 配对需要每个 Evaluation 只有一个 subset，或显式指定配对子集",
            status_code=422,
        )
    return str(subsets[0]) if subsets else None


def _significant(result: dict[str, Any]) -> bool:
    """显著性需同时满足 p 阈值与 CI 不跨 0。"""
    return bool(
        result["p"] is not None
        and result["p"] < 0.05
        and result["ci_low"] is not None
        and (result["ci_low"] > 0 or result["ci_high"] < 0)
    )


def _aggregate_cer(db: Session, evaluation_id: uuid.UUID) -> tuple[float | None, int]:
    """读取 Evaluation 总计 CER，供 near/far 等无配对条件做描述性比较。"""
    row = db.execute(
        select(MetricValue.value, MetricValue.n)
        .where(
            MetricValue.evaluation_id == evaluation_id,
            MetricValue.metric == "cer",
            MetricValue.scope == "micro",
            MetricValue.run_target_id.is_(None),
            MetricValue.bucket == {},
            MetricValue.status == "ok",
        )
        .order_by(MetricValue.id)
        .limit(1)
    ).first()
    return (row[0], int(row[1] or 0)) if row else (None, 0)


def significance_vs_baseline(
    db: Session,
    baseline: uuid.UUID,
    others: list[uuid.UUID],
    *,
    n_resample: int = 1000,
    pair_by: PairBy = "content_uid",
    pairing_subsets: dict[uuid.UUID, str] | None = None,
) -> dict[str, Any]:
    """计算基线与各 Evaluation 的配对 Δ；分桶 p 值按 BH 校正。"""
    if pair_by not in {"content_uid", "pair_key", "none"}:
        raise ApiError("E_VALIDATION", "不支持的配对方式", status_code=422)
    if n_resample < 1:
        raise ApiError("E_VALIDATION", "bootstrap 次数必须大于 0", status_code=422)

    comparisons: list[dict[str, Any]] = []
    base_subset = _pairing_subset(db, baseline, pairing_subsets) if pair_by == "pair_key" else None
    base_units = (
        _item_units(db, baseline, pair_by=pair_by, subset=base_subset) if pair_by != "none" else {}
    )

    for other in others:
        if pair_by == "none":
            base_value, base_n = _aggregate_cer(db, baseline)
            other_value, other_n = _aggregate_cer(db, other)
            delta = (
                base_value - other_value
                if base_value is not None and other_value is not None
                else None
            )
            comparisons.append(
                {
                    "evaluation_id": str(other),
                    "delta": delta,
                    "ci_low": None,
                    "ci_high": None,
                    "p": None,
                    "n": 0,
                    "n_baseline": base_n,
                    "n_comparison": other_n,
                    "significant": False,
                    "reliable": False,
                    "reason": None if delta is not None else "aggregate_metric_unavailable",
                    "warning": "仅作聚合比较，未进行样本配对与显著性检验",
                    "baseline_subset": None,
                    "comparison_subset": None,
                    "buckets": [],
                }
            )
            continue

        other_subset = (
            _pairing_subset(db, other, pairing_subsets) if pair_by == "pair_key" else None
        )
        other_units = _item_units(db, other, pair_by=pair_by, subset=other_subset)
        common = sorted(set(base_units) & set(other_units))
        pairs = [
            (base_units[key][0], base_units[key][1], other_units[key][0], other_units[key][1])
            for key in common
        ]
        overall = _bootstrap_delta(pairs, n_resample=n_resample)
        buckets: list[dict[str, Any]] = []
        for bucket_name in sorted({base_units[key][2] for key in common}):
            bucket_pairs = [
                (
                    base_units[key][0],
                    base_units[key][1],
                    other_units[key][0],
                    other_units[key][1],
                )
                for key in common
                if base_units[key][2] == bucket_name
            ]
            bucket_result = _bootstrap_delta(bucket_pairs, n_resample=n_resample)
            bucket_result.update(
                {
                    "bucket": {"duration_bucket": bucket_name},
                    "reliable": bucket_result["n"] >= 50,
                    "warning": (
                        "共同样本少于 50，显著性不可靠" if bucket_result["n"] < 50 else None
                    ),
                }
            )
            buckets.append(bucket_result)

        for bucket_result in buckets:
            bucket_result["p_bh"] = None
            bucket_result["significant_bh"] = False

        comparisons.append(
            {
                "evaluation_id": str(other),
                **overall,
                "significant": _significant(overall),
                "reliable": overall["n"] >= 50,
                "reason": "no_common_samples" if overall["n"] == 0 else None,
                "warning": ("共同样本少于 50，显著性不可靠" if overall["n"] < 50 else None),
                "baseline_subset": base_subset,
                "comparison_subset": other_subset,
                "buckets": buckets,
            }
        )

    bucket_tests = [
        bucket
        for comparison in comparisons
        for bucket in comparison["buckets"]
        if bucket["p"] is not None
    ]
    adjusted = _bh_adjust([float(bucket["p"]) for bucket in bucket_tests])
    for bucket, adjusted_p in zip(bucket_tests, adjusted, strict=True):
        bucket["p_bh"] = adjusted_p
        bucket["significant_bh"] = bool(
            adjusted_p < 0.05
            and bucket["ci_low"] is not None
            and (bucket["ci_low"] > 0 or bucket["ci_high"] < 0)
        )
    return {
        "baseline_evaluation_id": str(baseline),
        "pair_by": pair_by,
        "n_resample": n_resample,
        "comparisons": comparisons,
    }
