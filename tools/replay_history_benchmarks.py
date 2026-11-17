"""复现历史 CER 测试集：KeSpeech 每方言前 250 条 + AISHELL S0765/S0766。

用法：
    cd backend && uv run python ../tools/replay_history_benchmarks.py --dry-run
    cd backend && uv run python ../tools/replay_history_benchmarks.py --create

`--dry-run` 只统计，不写数据库；`--create` 以 version_hash 幂等创建基准。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.db import get_session_factory
from app.domain.benchmarks import create_benchmark, sampled_item_ids
from app.domain.models import Benchmark, Dataset, DatasetVersion, Item, MetricProfile

KESPEECH_NAME = "历史CER-KeSpeech-每方言前250"
AISHELL_NAME = "历史CER-AISHELL-S0765S0766"
KESPEECH_SAMPLING: dict[str, Any] = {
    "mode": "stratified",
    "dim": "dialect",
    "per_stratum": 250,
    "order": "item_key",
}
AISHELL_SAMPLING: dict[str, Any] = {
    "mode": "all",
    "filter": {"speakers": ["S0765", "S0766"]},
}
EXPECTED = {"KeSpeech": 2250, "AISHELL-1": 701}


def latest_ready_version(db: Session, dataset_name: str) -> DatasetVersion:
    """取数据集最新的 ready 版本；没有就报错。"""
    dataset = db.scalar(select(Dataset).where(Dataset.name == dataset_name))
    if dataset is None:
        raise SystemExit(f"找不到数据集 {dataset_name}")
    version = db.scalar(
        select(DatasetVersion)
        .where(
            DatasetVersion.dataset_id == dataset.id,
            DatasetVersion.status.in_(["ready", "ready_with_errors"]),
        )
        .order_by(DatasetVersion.created_at.desc())
    )
    if version is None:
        raise SystemExit(f"数据集 {dataset_name} 没有就绪版本")
    return version


def resolve_profile(db: Session, name: str) -> MetricProfile:
    """按名称取最新口径；找不到时报错，避免隐式用错评分口径。"""
    profile = db.scalar(
        select(MetricProfile)
        .where(MetricProfile.name == name)
        .order_by(MetricProfile.version.desc())
    )
    if profile is None:
        raise SystemExit(f"找不到口径 {name}；请先在评分口径页创建")
    return profile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create", action="store_true", help="写入基准（默认只统计）")
    parser.add_argument("--profile", default="default-zh", help="基准引用的口径名称")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = parser.parse_args()
    create = args.create and not args.dry_run

    with get_session_factory()() as db:
        profile = resolve_profile(db, args.profile)
        specs = [
            (
                "KeSpeech",
                KESPEECH_NAME,
                KESPEECH_SAMPLING,
                "KeSpeech 每个方言目录按 item_key 升序前 250 条；共 2250 条。",
            ),
            (
                "AISHELL-1",
                AISHELL_NAME,
                AISHELL_SAMPLING,
                "AISHELL-1 仅取说话人 S0765/S0766；共 701 条。",
            ),
        ]
        for dataset_name, benchmark_name, sampling, note in specs:
            version = latest_ready_version(db, dataset_name)
            temporary = Benchmark(
                name=benchmark_name,
                dataset_version_id=version.id,
                metric_profile_id=profile.id,
                subsets=["all"],
                sampling=sampling,
            )
            item_ids = sampled_item_ids(db, temporary)
            duration_ms = int(
                db.scalar(
                    select(func.coalesce(func.sum(Item.duration_ms), 0)).where(
                        Item.id.in_(item_ids)
                    )
                )
                or 0
            )
            expected = EXPECTED[dataset_name]
            print(
                f"{dataset_name}: 版本 {version.version}，选中 {len(item_ids)} 条"
                f"（预期 {expected}），音频 {duration_ms / 3600000:.2f} 小时"
            )
            if len(item_ids) != expected:
                print("  ⚠ 数量与历史预期不一致，请确认数据集版本与 item_key 排序")
            if create:
                benchmark = create_benchmark(
                    db,
                    name=benchmark_name,
                    dataset_version_id=version.id,
                    metric_profile_id=profile.id,
                    subsets=["all"],
                    sampling=sampling,
                    bucket_rules={"dims": []},
                    note=note,
                )
                print(f"  已就绪 Benchmark {benchmark.id} · hash {benchmark.version_hash}")
            else:
                print("  dry-run：未写入数据库")


if __name__ == "__main__":
    main()
