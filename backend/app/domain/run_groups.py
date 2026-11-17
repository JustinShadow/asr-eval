"""跨数据集评测组（RunGroup）的创建与汇总。

一个 Benchmark 只绑定一个 DatasetVersion；需要一次评测多个数据集时，
由 RunGroup 为每个 Benchmark 各创建一条 Run，并在组级按 ref_len 加权汇总。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domain.models import Benchmark, Dataset, DatasetVersion, Run, RunGroup
from app.domain.runs import create_run, preview_run, run_summary

# 一次组评测的上限；避免误选全部 Benchmark 把设备队列瞬间塞满。
MAX_GROUP_BENCHMARKS = 20


def create_run_group(
    db: Session,
    *,
    name: str,
    benchmark_ids: list[uuid.UUID],
    targets: list[tuple[uuid.UUID, str]],
    created_by: str | None,
    kind: str = "accuracy",
    lanes: int = 1,
    auto_score: bool = True,
) -> RunGroup:
    """为一个或多个 Benchmark 各创建一条 Run。

    先对全部 Benchmark 做预检，再开始创建，避免“前几条已入队、后一条失败”
    留下半个组。单条 Run 的创建仍复用 `create_run`，因此部署快照与采样规则一致。
    """
    if not benchmark_ids:
        raise ApiError("E_VALIDATION", "至少选择一个 Benchmark", status_code=422)
    if len(benchmark_ids) > MAX_GROUP_BENCHMARKS:
        raise ApiError(
            "E_VALIDATION",
            f"一次最多选择 {MAX_GROUP_BENCHMARKS} 个 Benchmark",
            status_code=422,
        )
    if len(set(benchmark_ids)) != len(benchmark_ids):
        raise ApiError("E_VALIDATION", "Benchmark 不能重复", status_code=422)
    if not targets:
        raise ApiError("E_SEMANTIC", "至少需要一个目标设备", status_code=422)

    benchmarks: list[Benchmark] = []
    for benchmark_id in benchmark_ids:
        benchmark = db.get(Benchmark, benchmark_id)
        if benchmark is None:
            raise ApiError("E_NOT_FOUND", f"Benchmark {benchmark_id} 不存在", status_code=404)
        if benchmark.archived:
            raise ApiError("E_SEMANTIC", "已归档的 Benchmark 不能用于新 Run", status_code=422)
        benchmarks.append(benchmark)

    # 预检全部目标：空采样、subset 不匹配、部署变化都会在创建前暴露。
    for benchmark in benchmarks:
        preview_run(
            db,
            benchmark_id=benchmark.id,
            targets=targets,
            kind=kind,
            lanes=lanes,
        )

    group = RunGroup(name=name, kind=kind, created_by=created_by)
    db.add(group)
    db.flush()

    for benchmark in benchmarks:
        create_run(
            db,
            name=f"{name} / {benchmark.name}"[:128],
            dataset_version_id=benchmark.dataset_version_id,
            benchmark_id=benchmark.id,
            targets=targets,
            created_by=created_by,
            kind=kind,
            lanes=lanes,
            auto_score=auto_score,
            group_id=group.id,
        )
    db.refresh(group)
    return group


def _run_dataset_label(db: Session, run: Run) -> dict[str, str | None]:
    """解析 Run 对应的数据集标签；Benchmark 被删除时降级为空。"""
    benchmark = db.get(Benchmark, run.benchmark_id)
    if benchmark is None:
        return {"dataset": None, "version": None, "benchmark": None}
    version = db.get(DatasetVersion, benchmark.dataset_version_id)
    dataset = db.get(Dataset, version.dataset_id) if version is not None else None
    return {
        "dataset": dataset.name if dataset is not None else None,
        "version": version.version if version is not None else None,
        "benchmark": benchmark.name,
    }


def run_group_summary(db: Session, group: RunGroup) -> dict[str, Any]:
    """返回组内 Run 列表与按 ref_len 加权的 CER 汇总。

    组级 CER 用 Σ(S+D+I)/Σref_len，而不是对每条 Run 的 CER 取平均：
    不同数据集样本量差异很大，等权平均会放大小数据集的权重。
    """
    runs = db.scalars(
        select(Run).where(Run.group_id == group.id).order_by(Run.created_at, Run.id)
    ).all()
    entries: list[dict[str, Any]] = []
    total_s = total_d = total_i = total_ref = total_n = 0
    scored_planned = 0
    status_counts: dict[str, int] = {}

    for run in runs:
        summary = run_summary(db, run)
        evaluation = summary["evaluation"]
        metrics = evaluation["metrics"] if isinstance(evaluation, dict) else None
        if isinstance(metrics, dict) and isinstance(metrics.get("cer"), int | float):
            total_s += int(metrics.get("s", 0))
            total_d += int(metrics.get("d", 0))
            total_i += int(metrics.get("i", 0))
            total_ref += int(metrics.get("ref_len", 0))
            total_n += int(metrics.get("n", 0))
        planned_cases = 0
        if isinstance(evaluation, dict) and isinstance(evaluation.get("coverage"), dict):
            planned_cases = int(evaluation["coverage"].get("planned", 0))
        if planned_cases == 0:
            # 评分尚未完成时用目标计划数，避免组页早期显示 100% 覆盖。
            planned_cases = sum(
                int(target.get("stats", {}).get("total", 0))
                for target in summary["targets"]
                if isinstance(target.get("stats"), dict)
            )
        scored_planned += planned_cases
        status_counts[run.status] = status_counts.get(run.status, 0) + 1
        entries.append(
            {
                **summary["run"],
                "dataset": _run_dataset_label(db, run),
                "cer": metrics.get("cer") if isinstance(metrics, dict) else None,
                "coverage": evaluation.get("coverage") if isinstance(evaluation, dict) else None,
                "evaluation_status": evaluation.get("status")
                if isinstance(evaluation, dict)
                else None,
                "metrics": summary["metrics"],
            }
        )

    aggregate_cer = (total_s + total_d + total_i) / total_ref if total_ref else None
    return {
        "group": {
            "id": str(group.id),
            "name": group.name,
            "kind": group.kind,
            "created_by": group.created_by,
            "created_at": group.created_at.isoformat(),
            "run_count": len(runs),
            "status_counts": status_counts,
        },
        "runs": entries,
        "aggregate": {
            "cer": aggregate_cer,
            "s": total_s,
            "d": total_d,
            "i": total_i,
            "ref_len": total_ref,
            "n": total_n,
            "planned": scored_planned,
            # 组级覆盖率按样本数；与单条 Evaluation 的 coverage 口径一致。
            "coverage": (total_n / scored_planned if scored_planned else None),
        },
    }


def list_run_groups(db: Session, *, limit: int = 50) -> list[dict[str, Any]]:
    """按创建时间倒序返回评测组与组成员数/完成数。"""
    groups = db.scalars(
        select(RunGroup).order_by(RunGroup.created_at.desc(), RunGroup.id.desc()).limit(limit)
    ).all()
    if not groups:
        return []
    counts = dict(
        db.execute(
            select(Run.group_id, func.count())
            .where(Run.group_id.in_([group.id for group in groups]))
            .group_by(Run.group_id)
        ).all()
    )
    completed = dict(
        db.execute(
            select(Run.group_id, func.count())
            .where(
                Run.group_id.in_([group.id for group in groups]),
                Run.status == "completed",
            )
            .group_by(Run.group_id)
        ).all()
    )
    return [
        {
            "id": str(group.id),
            "name": group.name,
            "kind": group.kind,
            "created_by": group.created_by,
            "created_at": group.created_at.isoformat(),
            "run_count": int(counts.get(group.id, 0)),
            "completed_count": int(completed.get(group.id, 0)),
        }
        for group in groups
    ]


def get_run_group(db: Session, group_id: uuid.UUID) -> RunGroup:
    """按 id 取评测组；不存在返回 404。"""
    group = db.get(RunGroup, group_id)
    if group is None:
        raise ApiError("E_NOT_FOUND", "评测组不存在", status_code=404)
    return group
