"""Benchmark 采样确定性测试。"""

import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.domain.benchmarks import sampled_item_ids
from app.domain.models import (
    AudioAsset,
    Base,
    Dataset,
    DatasetSubset,
    DatasetVersion,
    Item,
    RefText,
)
from app.domain.runs import ensure_benchmark, ensure_default_profile

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


def test_random_sampling_is_deterministic(db: Session) -> None:
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(name=f"sample-{suffix}", source_type="aishell1")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready")
    db.add(version)
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    db.add(subset)
    db.flush()

    for index in range(50):
        asset = AudioAsset(sha256=f"{suffix}{index:04d}".ljust(64, "0"), uri=f"/data/{index}.wav")
        db.add(asset)
        db.flush()
        item = Item(
            dataset_version_id=version.id,
            subset_id=subset.id,
            item_key=f"utt-{index}",
            content_uid=f"{suffix}{index:04d}".ljust(24, "0"),
            audio_asset_id=asset.id,
            duration_ms=3000,
        )
        db.add(item)
        db.flush()
        db.add(RefText(item_id=item.id, kind="verbatim", text="测试", sha256=suffix))
    db.commit()

    profile = ensure_default_profile(db)
    benchmark = ensure_benchmark(db, version, profile)
    benchmark.sampling = {"mode": "random", "n": 10, "seed": 42}
    db.commit()

    first = sampled_item_ids(db, benchmark)
    second = sampled_item_ids(db, benchmark)
    assert len(first) == 10
    assert first == second


def _mk_version(session: Session, suffix: str, subsets: dict[str, int]) -> DatasetVersion:
    """造一个版本：subsets 给出 {subset 名: 条数}；每条 content_uid 唯一。"""
    dataset = Dataset(name=f"vh-{suffix}", source_type="aishell1")
    session.add(dataset)
    session.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready")
    session.add(version)
    session.flush()
    subset_rows = {
        name: DatasetSubset(dataset_version_id=version.id, name=name, is_primary=index == 0)
        for index, name in enumerate(subsets)
    }
    for subset in subset_rows.values():
        session.add(subset)
    session.flush()
    for name, count in subsets.items():
        for index in range(count):
            asset = AudioAsset(
                sha256=f"{suffix}{name}{index:04d}".ljust(64, "0"),
                uri=f"/data/{suffix}-{name}-{index}.wav",
            )
            session.add(asset)
            session.flush()
            session.add(
                Item(
                    dataset_version_id=version.id,
                    subset_id=subset_rows[name].id,
                    item_key=f"{name}-{index}",
                    content_uid=f"{suffix}{name}{index:04d}".ljust(24, "0"),
                    audio_asset_id=asset.id,
                    duration_ms=3000,
                )
            )
    session.commit()
    return version


def test_version_hash_stable_and_sensitive(db: Session) -> None:
    """相同输入 hash 相同；sampling/params 任一变化 → hash 变（M3 §5.6/§8.1）。"""
    suffix = uuid.uuid4().hex[:8]
    version = _mk_version(db, suffix, {"all": 5})
    profile = ensure_default_profile(db)
    from app.domain.benchmarks import compute_version_hash

    base = dict(
        dataset_version_id=version.id,
        subsets=["all"],
        profile_hash=profile.profile_hash,
        bucket_rules={},
        params={},
    )
    h1 = compute_version_hash(db, sampling={"mode": "all"}, **base)
    h2 = compute_version_hash(db, sampling={"mode": "all"}, **base)
    assert h1 == h2
    h3 = compute_version_hash(db, sampling={"mode": "random", "n": 3, "seed": 1}, **base)
    assert h3 != h1
    h4 = compute_version_hash(
        db,
        sampling={"mode": "all"},
        params={"itn": True},
        **{k: v for k, v in base.items() if k != "params"},
    )
    assert h4 != h1


def test_named_subsets_sampled_independently(db: Session) -> None:
    """多 subset 各自独立采样（M3 §5.3）：phone 30 条、mic 20 条，各取 10。"""
    suffix = uuid.uuid4().hex[:8]
    version = _mk_version(db, suffix, {"phone": 30, "mic": 20})
    profile = ensure_default_profile(db)
    from app.domain.benchmarks import create_benchmark, sampled_item_ids

    benchmark = create_benchmark(
        db,
        name=f"ab-{suffix}",
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        subsets=["phone", "mic"],
        sampling={"mode": "random", "n": 10, "seed": 7},
    )
    ids = sampled_item_ids(db, benchmark)
    assert len(ids) == 20  # 每 subset 各 10，不因条数不同而失衡
    assert len(set(ids)) == 20


def test_create_benchmark_reuses_same_version_hash(db: Session) -> None:
    """version_hash 冲突幂等复用（M3 §7）。"""
    suffix = uuid.uuid4().hex[:8]
    version = _mk_version(db, suffix, {"all": 5})
    profile = ensure_default_profile(db)
    from app.domain.benchmarks import create_benchmark

    first = create_benchmark(
        db,
        name=f"b1-{suffix}",
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        subsets=["all"],
        sampling={"mode": "all"},
    )
    second = create_benchmark(
        db,
        name=f"b2-{suffix}",
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        subsets=["all"],
        sampling={"mode": "all"},
    )
    assert second.id == first.id
    assert second.version_hash == first.version_hash is not None


def _mk_history_version(db: Session, suffix: str) -> tuple[DatasetVersion, dict[str, int]]:
    """造一个带 dialect 分桶与 speakers 的版本，用于历史测试集复现。"""
    dataset = Dataset(name=f"history-{suffix}", source_type="kespeech")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready")
    db.add(version)
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    db.add(subset)
    db.flush()
    keys: dict[str, int] = {}
    fixtures = [
        ("beijing-003", "Beijing", ["S1"]),
        ("beijing-001", "Beijing", ["S1"]),
        ("beijing-002", "Beijing", ["S1"]),
        ("jilu-002", "Ji-Lu", ["S2"]),
        ("jilu-001", "Ji-Lu", ["S2"]),
        ("aishell-S0764", "Mandarin", ["S0764"]),
        ("aishell-S0765", "Mandarin", ["S0765"]),
        ("aishell-S0766", "Mandarin", ["S0766"]),
    ]
    for index, (key, dialect, speakers) in enumerate(fixtures):
        asset = AudioAsset(sha256=f"{suffix}{index:04d}".ljust(64, "0"), uri=f"/data/{key}.wav")
        db.add(asset)
        db.flush()
        item = Item(
            dataset_version_id=version.id,
            subset_id=subset.id,
            item_key=key,
            content_uid=f"{suffix}{index:04d}".ljust(24, "0"),
            audio_asset_id=asset.id,
            duration_ms=3000,
            speakers=speakers,
            buckets={"dialect": dialect, "language": "zh"},
        )
        db.add(item)
        db.flush()
        db.add(RefText(item_id=item.id, kind="verbatim", text="测试", sha256=suffix))
        keys[key] = item.id
    db.commit()
    return version, keys


def test_stratified_item_key_order_reproduces_first_n_per_group(db: Session) -> None:
    """stratified + order=item_key 复现“每个方言目录前 N 条”。"""
    suffix = uuid.uuid4().hex[:8]
    version, keys = _mk_history_version(db, suffix)
    profile = ensure_default_profile(db)
    benchmark = ensure_benchmark(db, version, profile)
    benchmark.sampling = {
        "mode": "stratified",
        "dim": "dialect",
        "per_stratum": 2,
        "order": "item_key",
    }
    db.commit()

    selected = sampled_item_ids(db, benchmark)
    assert selected == [
        keys["beijing-001"],
        keys["beijing-002"],
        keys["jilu-001"],
        keys["jilu-002"],
        keys["aishell-S0764"],
        keys["aishell-S0765"],
    ]


def test_speaker_filter_supports_exact_history_subset(db: Session) -> None:
    """mode=all + filter.speakers 可精确取 AISHELL S0765/S0766。"""
    suffix = uuid.uuid4().hex[:8]
    version, keys = _mk_history_version(db, suffix)
    profile = ensure_default_profile(db)
    benchmark = ensure_benchmark(db, version, profile)
    benchmark.sampling = {
        "mode": "all",
        "filter": {"speakers": ["S0765", "S0766"]},
    }
    db.commit()

    selected = sampled_item_ids(db, benchmark)
    assert selected == [keys["aishell-S0765"], keys["aishell-S0766"]]


def test_sampling_validation_accepts_order_and_speaker_filter() -> None:
    """新增的 order/filter 进入校验，非法值能定位字段。"""
    from app.domain.benchmarks import validate_benchmark_definition

    assert (
        validate_benchmark_definition(
            {
                "mode": "stratified",
                "dim": "dialect",
                "per_stratum": 250,
                "order": "item_key",
            },
            {},
        )
        == []
    )
    assert (
        validate_benchmark_definition(
            {"mode": "all", "filter": {"speakers": ["S0765", "S0766"]}},
            {},
        )
        == []
    )
    errors = validate_benchmark_definition(
        {"mode": "random", "n": 10, "order": "bad", "filter": {"unknown": 1}},
        {},
    )
    assert any("sampling.order" in error for error in errors)
    assert any("sampling.filter" in error for error in errors)


def test_bucket_coverage_reports_unknown_dimensions(db: Session) -> None:
    """分桶覆盖率用于自动勾选：缺失维度 coverage=0，duration 始终可用。"""
    from app.domain.benchmarks import bucket_coverage

    suffix = uuid.uuid4().hex[:8]
    version, _keys = _mk_history_version(db, suffix)
    result = bucket_coverage(db, dataset_version_id=version.id, subsets=["all"])
    by_key = {item["key"]: item for item in result["items"]}

    assert by_key["dialect"]["coverage"] == 1.0
    assert by_key["dialect"]["distinct"] == 3
    assert by_key["scene"]["coverage"] == 0.0
    assert by_key["scene"]["known"] == 0
    assert by_key["lang"]["coverage"] == 1.0
    assert by_key["duration_bin"]["coverage"] == 1.0
    assert result["total"] == 8
