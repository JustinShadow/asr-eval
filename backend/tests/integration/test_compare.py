"""Evaluation 对比的可比性校验。"""

import os
import time
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.domain.compare import compare_evaluations, significance_vs_baseline
from app.domain.models import (
    AudioAsset,
    Base,
    Benchmark,
    CaseResult,
    CaseScore,
    Dataset,
    DatasetSubset,
    DatasetVersion,
    Device,
    Evaluation,
    Item,
    MetricProfile,
    MetricValue,
    Run,
    RunTarget,
)

TEST_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://asr:asr@127.0.0.1:5432/asr_eval_test",
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def db() -> Session:
    engine = create_engine(TEST_URL, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_version(db: Session, suffix: str) -> DatasetVersion:
    dataset = Dataset(name=f"cmp-{suffix}", source_type="aishell1")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready", item_count=10)
    db.add(version)
    db.flush()
    return version


def _make_eval(
    db: Session, suffix: str, profile_hash: str, version: DatasetVersion | None = None
) -> Evaluation:
    version = version or _make_version(db, suffix)
    profile = MetricProfile(name=f"p-{suffix}", params={}, profile_hash=profile_hash)
    db.add(profile)
    db.flush()
    benchmark = Benchmark(
        name=f"b-{suffix}",
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        subsets=["all"],
    )
    db.add(benchmark)
    db.flush()
    run = Run(benchmark_id=benchmark.id, name=f"r-{suffix}", status="completed")
    db.add(run)
    db.flush()
    evaluation = Evaluation(
        run_id=run.id,
        metric_profile_id=profile.id,
        profile_hash=profile_hash,
        scorer_version="cer-1",
        status="completed",
        metrics={"cer": 0.02, "n": 10},
    )
    db.add(evaluation)
    db.commit()
    db.refresh(evaluation)
    return evaluation


def test_comparable_same_profile(db: Session) -> None:
    suffix = uuid.uuid4().hex[:6]
    version = _make_version(db, f"a-{suffix}")
    first = _make_eval(db, f"a-{suffix}", "hash-1", version)
    second = _make_eval(db, f"b-{suffix}", "hash-1", version)
    result = compare_evaluations(db, [first.id, second.id])
    assert result["comparable"] is True
    assert result["blockers"] == []
    assert result["evaluations"][0]["created_at"] is not None
    # 没有 metric_value 明细时不伪造 0 值。
    assert result["rows"] == []


def test_unpaired_comparison_reports_aggregate_delta_without_significance(db: Session) -> None:
    suffix = uuid.uuid4().hex[:6]
    version = _make_version(db, f"aggregate-{suffix}")
    baseline = _make_eval(db, f"aggregate-base-{suffix}", "agg-profile", version)
    candidate = _make_eval(db, f"aggregate-candidate-{suffix}", "agg-profile", version)
    for evaluation, value, count in ((baseline, 0.2, 80), (candidate, 0.15, 75)):
        benchmark = db.get(Benchmark, db.get(Run, evaluation.run_id).benchmark_id)
        assert benchmark is not None
        benchmark.version_hash = f"agg-{suffix}"
        db.add(
            MetricValue(
                evaluation_id=evaluation.id,
                run_target_id=None,
                metric="cer",
                subset="all",
                scope="micro",
                bucket={},
                value=value,
                n=count,
                ci_low=None,
                ci_high=None,
                status="ok",
                detail={},
            )
        )
    db.commit()

    result = significance_vs_baseline(db, baseline.id, [candidate.id], pair_by="none")
    comparison = result["comparisons"][0]
    assert comparison["delta"] == pytest.approx(0.05)
    assert comparison["n_baseline"] == 80
    assert comparison["n_comparison"] == 75
    assert comparison["p"] is None
    assert comparison["ci_low"] is None


def test_profile_mismatch_blocks(db: Session) -> None:
    suffix = uuid.uuid4().hex[:6]
    first = _make_eval(db, f"c-{suffix}", "hash-1")
    second = _make_eval(db, f"d-{suffix}", "hash-2")
    result = compare_evaluations(db, [first.id, second.id])
    assert result["comparable"] is False
    codes = {blocker["code"] for blocker in result["blockers"]}
    assert "E_PROFILE_MISMATCH" in codes


def test_empty_target_subset_is_not_ignored_during_alignment(db: Session) -> None:
    suffix = uuid.uuid4().hex[:6]
    version = _make_version(db, f"empty-subset-{suffix}")
    first = _make_eval(db, f"has-target-{suffix}", "same-profile", version)
    second = _make_eval(db, f"no-target-{suffix}", "same-profile", version)
    device = Device(
        name=f"subset-device-{suffix}",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=9,
        status="online",
    )
    db.add(device)
    db.flush()
    db.add(RunTarget(run_id=first.run_id, device_id=device.id, subset="near", status="completed"))
    db.commit()

    result = compare_evaluations(db, [first.id, second.id], pair_by="content_uid")
    warning_codes = {warning["code"] for warning in result["warnings"]}
    blocker_codes = {blocker["code"] for blocker in result["blockers"]}
    assert "W_SUBSET_COVERAGE" in warning_codes
    assert "E_NO_COMMON_SUBSET" in blocker_codes


def test_pair_key_bootstrap_aggregates_repeated_targets(db: Session) -> None:
    """phone/mic 按 pair_key 配对；同一 Evaluation 的多目标合并到样本单元。"""
    suffix = uuid.uuid4().hex[:6]
    version = _make_version(db, f"pair-{suffix}")
    phone_subset = DatasetSubset(dataset_version_id=version.id, name="phone", is_primary=False)
    mic_subset = DatasetSubset(dataset_version_id=version.id, name="mic", is_primary=False)
    db.add_all([phone_subset, mic_subset])
    db.flush()

    baseline = _make_eval(db, f"phone-{suffix}", "pair-profile", version)
    candidate = _make_eval(db, f"mic-{suffix}", "pair-profile", version)
    for evaluation in (baseline, candidate):
        benchmark = db.get(Benchmark, db.get(Run, evaluation.run_id).benchmark_id)
        assert benchmark is not None
        benchmark.version_hash = "pair-hash-v1"

    device_rows = []
    for name in (f"base-a-{suffix}", f"base-b-{suffix}", f"mic-{suffix}"):
        device = Device(
            name=name,
            platform="205",
            kind="etsstt",
            host="127.0.0.1",
            port=9,
            status="online",
        )
        db.add(device)
        device_rows.append(device)
    db.flush()

    baseline_targets = [
        RunTarget(run_id=baseline.run_id, device_id=device.id, subset="phone", status="completed")
        for device in device_rows[:2]
    ]
    candidate_target = RunTarget(
        run_id=candidate.run_id,
        device_id=device_rows[2].id,
        subset="mic",
        status="completed",
    )
    db.add_all([*baseline_targets, candidate_target])
    db.flush()

    items: list[tuple[Item, str, int]] = []
    for subset_name, subset_row, prefix in (
        ("phone", phone_subset, "p"),
        ("mic", mic_subset, "m"),
    ):
        for index in range(50):
            asset = AudioAsset(
                sha256=f"{suffix}{prefix}{index:03d}".ljust(64, "0"),
                uri=f"/data/{prefix}-{index}.wav",
            )
            db.add(asset)
            db.flush()
            item = Item(
                dataset_version_id=version.id,
                subset_id=subset_row.id,
                item_key=f"{prefix}-{suffix}-{index}",
                pair_key=f"pair-{index}",
                content_uid=f"{prefix}{index:023d}",
                audio_asset_id=asset.id,
                split="test",
                duration_ms=1000,
            )
            items.append((item, subset_name, index))
    db.add_all(item for item, _subset_name, _index in items)
    db.flush()

    cases: list[tuple[CaseResult, int]] = []
    for item, subset_name, _index in items:
        targets = baseline_targets if subset_name == "phone" else [candidate_target]
        for target_index, target in enumerate(targets):
            case = CaseResult(
                run_target_id=target.id,
                item_id=item.id,
                status="ok",
                text="参考文本",
            )
            db.add(case)
            edits = 2 if subset_name == "phone" and target_index == 0 else 0
            cases.append((case, edits))
    db.flush()
    db.add_all(
        CaseScore(
            evaluation_id=baseline.id
            if case.run_target_id in {target.id for target in baseline_targets}
            else candidate.id,
            case_result_id=case.id,
            metrics={
                "cer": {
                    "status": "ok",
                    "value": edits / 10,
                    "s": edits,
                    "d": 0,
                    "i": 0,
                    "n": 10,
                }
            },
        )
        for case, edits in cases
    )
    db.commit()

    compatibility = compare_evaluations(db, [baseline.id, candidate.id], pair_by="pair_key")
    assert compatibility["comparable"] is True
    assert any(issue["code"] == "W_SUBSET_COVERAGE" for issue in compatibility["warnings"])

    result = significance_vs_baseline(
        db,
        baseline.id,
        [candidate.id],
        n_resample=200,
        pair_by="pair_key",
    )
    comparison = result["comparisons"][0]
    assert comparison["baseline_subset"] == "phone"
    assert comparison["comparison_subset"] == "mic"
    assert comparison["n"] == 50
    assert comparison["delta"] == pytest.approx(0.1)
    assert comparison["reliable"] is True
    assert comparison["significant"] is True


def test_three_target_two_subset_eight_metric_matrix_under_half_second(db: Session) -> None:
    """3 × 2 × 8 的 M6 指标矩阵在 500ms 内返回。"""
    suffix = uuid.uuid4().hex[:6]
    version = _make_version(db, f"matrix-{suffix}")
    evaluation_rows = [
        _make_eval(db, f"matrix-{suffix}-{index}", "matrix-profile", version) for index in range(3)
    ]
    devices = [
        Device(
            name=f"matrix-device-{suffix}-{index}",
            platform="205",
            kind="etsstt",
            host="127.0.0.1",
            port=9,
            status="online",
        )
        for index in range(3)
    ]
    db.add_all(devices)
    db.flush()
    metrics = ["cer", "wer", "punct_f1", "der", "sa_der", "naming_acc", "speaker_eer", "rtf_e2e"]
    for evaluation in evaluation_rows:
        benchmark = db.get(Benchmark, db.get(Run, evaluation.run_id).benchmark_id)
        assert benchmark is not None
        benchmark.version_hash = "matrix-hash-v1"
        targets = [
            RunTarget(
                run_id=evaluation.run_id, device_id=device.id, subset=subset, status="completed"
            )
            for device in devices
            for subset in ("near", "far")
        ]
        db.add_all(targets)
        db.flush()
        db.add_all(
            MetricValue(
                evaluation_id=evaluation.id,
                run_target_id=target.id,
                metric=metric,
                subset=target.subset,
                scope="micro",
                bucket={},
                value=0.05,
                n=1000,
                ci_low=0.04,
                ci_high=0.06,
                status="ok",
                detail={},
            )
            for target in targets
            for metric in metrics
        )
    db.commit()

    started = time.perf_counter()
    result = compare_evaluations(db, [evaluation.id for evaluation in evaluation_rows])
    elapsed = time.perf_counter() - started

    assert result["comparable"] is True
    assert len(result["rows"]) == 3 * 2 * 8
    assert all(len(row["values"]) == 3 for row in result["rows"])
    assert elapsed < 0.5
