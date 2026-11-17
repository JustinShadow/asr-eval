"""Badcase 列表筛选集成测试（M5 §3.2、M8 §9.7）。"""

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.evaluations import list_cases
from app.core.errors import ApiError
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
    RefText,
    Run,
    RunTarget,
)

TEST_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://asr:asr@127.0.0.1:5432/asr_eval_test",
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def db() -> Iterator[Session]:
    engine = create_engine(TEST_URL, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_evaluation(
    db: Session,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """创建一个可挂载多条样本的 Evaluation。"""
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(name=f"badcase-filter-{suffix}", source_type="aishell1")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready", item_count=4)
    db.add(version)
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    db.add(subset)
    asset = AudioAsset(sha256=suffix.ljust(64, "0"), uri="/data/badcase-filter.wav")
    db.add_all([subset, asset])
    db.flush()
    device = Device(
        name=f"badcase-filter-device-{suffix}",
        platform="205",
        kind="etsstt",
        host="1.1.1.1",
        port=9,
    )
    profile = MetricProfile(
        name=f"badcase-filter-profile-{suffix}", params={}, profile_hash=suffix[:16]
    )
    db.add_all([device, profile])
    db.flush()
    benchmark = Benchmark(
        name=f"badcase-filter-benchmark-{suffix}",
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        subsets=["all"],
        version_hash=suffix[:16],
    )
    db.add(benchmark)
    db.flush()
    run = Run(benchmark_id=benchmark.id, name=f"badcase-filter-run-{suffix}", status="completed")
    db.add(run)
    db.flush()
    target = RunTarget(run_id=run.id, device_id=device.id, subset="all", status="completed")
    db.add(target)
    db.flush()
    evaluation = Evaluation(
        run_id=run.id,
        metric_profile_id=profile.id,
        profile_hash=suffix[:16],
        scorer_version="test",
        trigger="manual",
        status="completed",
    )
    db.add(evaluation)
    db.commit()
    return evaluation.id, target.id, version.id, subset.id, asset.id


def _add_case(
    db: Session,
    *,
    evaluation_id: uuid.UUID,
    target_id: uuid.UUID,
    version_id: uuid.UUID,
    subset_id: uuid.UUID,
    asset_id: uuid.UUID,
    lang: str,
    value: float,
    s: int,
    d: int,
    i: int,
    dialect: str,
    suffix: str,
) -> None:
    """新增带 CER 分解与语言分桶的逐样本结果。"""
    item = Item(
        dataset_version_id=version_id,
        subset_id=subset_id,
        item_key=f"{lang}/sample-{suffix}.wav",
        content_uid=suffix.ljust(24, "0"),
        audio_asset_id=asset_id,
        split="test",
        duration_ms=3000,
        lang=lang,
        buckets={"dialect": dialect},
    )
    db.add(item)
    db.flush()
    db.add(RefText(item_id=item.id, kind="verbatim", text="测试文本", sha256=suffix))
    case = CaseResult(run_target_id=target_id, item_id=item.id, status="ok", text="测试文字")
    db.add(case)
    db.flush()
    db.add(
        CaseScore(
            evaluation_id=evaluation_id,
            case_result_id=case.id,
            metrics={"cer": {"status": "ok", "value": value, "s": s, "d": d, "i": i, "n": 10}},
        )
    )
    db.commit()


def test_case_list_filters_by_threshold_language_error_and_bucket(db: Session) -> None:
    evaluation_id, target_id, version_id, subset_id, asset_id = _make_evaluation(db)
    samples = [
        ("yue", 0.25, 1, 0, 1, "Cantonese"),
        ("zh", 0.32, 0, 2, 0, "Mandarin"),
        ("yue", 0.10, 0, 0, 1, "Cantonese"),
        ("zh", 0.60, 3, 1, 2, "Mandarin"),
    ]
    for index, (lang, value, s, d, i, dialect) in enumerate(samples):
        _add_case(
            db,
            evaluation_id=evaluation_id,
            target_id=target_id,
            version_id=version_id,
            subset_id=subset_id,
            asset_id=asset_id,
            lang=lang,
            value=value,
            s=s,
            d=d,
            i=i,
            dialect=dialect,
            suffix=f"{index:08x}",
        )

    filtered = list_cases(
        evaluation_id=evaluation_id,
        limit=200,
        cursor=None,
        only_errors=True,
        cer_min=0.2,
        lang="yue",
        error_type="insertion",
        bucket=None,
        subset=None,
        db=db,
        _={},
    )
    assert len(filtered["items"]) == 1
    assert filtered["items"][0]["lang"] == "yue"
    assert filtered["items"][0]["buckets"] == {"dialect": "Cantonese"}
    assert filtered["items"][0]["cer"] == pytest.approx(0.25)

    bucket_filtered = list_cases(
        evaluation_id=evaluation_id,
        limit=200,
        cursor=None,
        only_errors=True,
        cer_min=0.0,
        lang=None,
        error_type=None,
        bucket="dialect:Cantonese",
        subset="all",
        db=db,
        _={},
    )
    assert [item["cer"] for item in bucket_filtered["items"]] == [0.25, 0.1]
    assert bucket_filtered["items"][0]["content_uid"] == "000000000000000000000000"
    assert bucket_filtered["items"][0]["pair_key"] is None
    assert bucket_filtered["items"][0]["subset"] == "all"

    duration_filtered = list_cases(
        evaluation_id=evaluation_id,
        limit=200,
        cursor=None,
        only_errors=False,
        cer_min=0.0,
        lang=None,
        error_type=None,
        bucket="duration_bucket:lt_5s",
        subset="all",
        db=db,
        _={},
    )
    assert len(duration_filtered["items"]) == 4


def test_case_list_rejects_malformed_bucket_filter(db: Session) -> None:
    evaluation_id, _target_id, _version_id, _subset_id, _asset_id = _make_evaluation(db)
    with pytest.raises(ApiError, match="bucket 筛选格式"):
        list_cases(
            evaluation_id=evaluation_id,
            limit=200,
            cursor=None,
            only_errors=True,
            cer_min=0.0,
            lang=None,
            error_type=None,
            bucket="dialect",
            subset=None,
            db=db,
            _={},
        )
