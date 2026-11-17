"""Badcase 打标 API 集成测试（M8 §9.7）。

需要 PostgreSQL（asr_eval_test）；数据库不可用时整组跳过。
"""

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.evaluations import TagRequest, clear_case_tag, set_case_tag
from app.domain.models import (
    Base,
    Benchmark,
    CaseResult,
    CaseScore,
    CaseTag,
    Dataset,
    DatasetSubset,
    DatasetVersion,
    Evaluation,
    Item,
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
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_case(db: Session) -> tuple[uuid.UUID, int]:
    """造 Run→Target→Case→Evaluation→CaseScore 最小链。"""
    from app.domain.models import AudioAsset, Device, MetricProfile, RefText

    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(name=f"tag-{suffix}", source_type="aishell1")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready", item_count=1)
    db.add(version)
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    db.add(subset)
    db.flush()
    asset = AudioAsset(sha256=suffix.ljust(64, "0"), uri="/data/tag.wav")
    db.add(asset)
    db.flush()
    item = Item(
        dataset_version_id=version.id,
        subset_id=subset.id,
        item_key=f"tag-{suffix}",
        content_uid=suffix.ljust(24, "0"),
        audio_asset_id=asset.id,
        split="test",
        duration_ms=3000,
    )
    db.add(item)
    db.flush()
    db.add(RefText(item_id=item.id, kind="verbatim", text="测试", sha256=suffix))
    device = Device(name=f"tagdev-{suffix}", platform="205", kind="etsstt", host="1.1.1.1", port=9)
    db.add(device)
    db.flush()
    profile = MetricProfile(name=f"p-{suffix}", params={}, profile_hash=suffix[:16])
    db.add(profile)
    db.flush()
    benchmark = Benchmark(
        name=f"b-{suffix}",
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        subsets=["all"],
        version_hash=suffix[:16],
    )
    db.add(benchmark)
    db.flush()
    run = Run(benchmark_id=benchmark.id, name=f"tag-{suffix}", status="completed")
    db.add(run)
    db.flush()
    target = RunTarget(run_id=run.id, device_id=device.id, subset="all", status="completed")
    db.add(target)
    db.flush()
    case = CaseResult(run_target_id=target.id, item_id=item.id, status="ok", text="测试")
    db.add(case)
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
    db.flush()
    score = CaseScore(
        evaluation_id=evaluation.id,
        case_result_id=case.id,
        metrics={"cer": {"status": "ok", "value": 0.1, "s": 1, "d": 0, "i": 0, "n": 10}},
    )
    db.add(score)
    db.commit()
    return evaluation.id, score.id


def test_tag_upsert_and_clear(db: Session) -> None:
    evaluation_id, case_id = _make_case(db)

    result = set_case_tag(
        evaluation_id=evaluation_id,
        case_id=case_id,
        body=TagRequest(tag=5),
        db=db,
        claims={"sub": "tester"},
    )
    assert result["tag"] == 5
    row = db.scalar(select(CaseTag).where(CaseTag.case_score_id == case_id))
    assert row is not None and row.tag == 5 and row.created_by == "tester"

    # upsert：同一 case 再打标覆盖，不新增行
    set_case_tag(
        evaluation_id=evaluation_id,
        case_id=case_id,
        body=TagRequest(tag=9),
        db=db,
        claims={"sub": "tester"},
    )
    rows = db.scalars(select(CaseTag).where(CaseTag.case_score_id == case_id)).all()
    assert len(rows) == 1 and rows[0].tag == 9

    clear_case_tag(evaluation_id=evaluation_id, case_id=case_id, db=db, _=None)
    assert db.scalar(select(CaseTag).where(CaseTag.case_score_id == case_id)) is None


def test_tag_validation_range(db: Session) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TagRequest(tag=0)
    with pytest.raises(ValidationError):
        TagRequest(tag=10)
