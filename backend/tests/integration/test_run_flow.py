"""Run 全链路（不依赖真机）：用假转写验证执行与 CER 聚合。"""

import asyncio
import copy
import os
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.etsstt import IdentityOutcome, TranscribeOutcome
from app.core.sse import sse_event_buffer
from app.domain import runs as runs_module
from app.domain.devices import compute_uid
from app.domain.models import (
    AudioAsset,
    Base,
    Benchmark,
    CaseResult,
    Dataset,
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
    RefSegment,
    RefText,
    Run,
    RunTarget,
)
from app.domain.run_worker import RunWorkerPool
from app.domain.runs import (
    create_evaluation_record,
    create_run,
    execute_run,
    preview_run,
    process_evaluation,
    resume_run,
    run_summary,
)

TEST_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://asr:asr@127.0.0.1:5432/asr_eval_test",
)

pytestmark = pytest.mark.integration


def _wait_for_evaluation(
    db: Session, *, run_id: uuid.UUID | None = None, evaluation_id: uuid.UUID | None = None
) -> None:
    """等后台评分进入终态，避免测试依赖线程调度时序。"""
    from app.domain.models import Evaluation

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        db.expire_all()
        if evaluation_id is not None:
            evaluation = db.get(Evaluation, evaluation_id)
        else:
            evaluation = db.scalar(
                select(Evaluation)
                .where(Evaluation.run_id == run_id)
                .order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
                .limit(1)
            )
        if evaluation is not None and evaluation.status in {"completed", "failed", "cancelled"}:
            assert evaluation.status == "completed", evaluation.metrics
            return
        time.sleep(0.01)
    raise AssertionError("Evaluation worker 在 30 秒内未完成评分")


@pytest.fixture()
def db() -> Iterator[Session]:
    engine = create_engine(TEST_URL, pool_pre_ping=True)
    # 本文件使用专用 asr_eval_test；每个用例清空旧 schema，避免 ETA 历史和缓存互相污染。
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        from app.domain.evaluation_worker import get_evaluation_worker

        get_evaluation_worker().shutdown()
        engine.dispose()


def test_run_executes_and_scores(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(name=f"ds-{suffix}", source_type="aishell1")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready", item_count=1)
    db.add(version)
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    db.add(subset)
    db.flush()

    asset = AudioAsset(sha256=suffix.ljust(64, "0"), uri="/data/fake.wav")
    db.add(asset)
    db.flush()
    ref_text = "甚至出现交易几乎停滞的情况"
    item = Item(
        dataset_version_id=version.id,
        subset_id=subset.id,
        item_key=f"utt-{suffix}",
        content_uid=suffix.ljust(24, "0"),
        audio_asset_id=asset.id,
        split="test",
        duration_ms=4000,
        speakers=["S0001"],
        buckets={"language": "zh"},
    )
    db.add(item)
    db.flush()
    db.add(RefText(item_id=item.id, kind="verbatim", text=ref_text, sha256=suffix))
    db.commit()

    device = Device(
        name=f"fake-{suffix}",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=9,
        status="online",
    )
    db.add(device)
    db.flush()
    db.add(
        Deployment(
            device_id=device.id,
            uid=suffix,
            platform="205",
            accel={"type": "rknn"},
            model_key="etsstt",
            capabilities={},
        )
    )
    db.commit()

    async def fake_transcribe(*args: object, **kwargs: object) -> TranscribeOutcome:
        return TranscribeOutcome(ok=True, text=ref_text, wall_ms=5)

    monkeypatch.setattr(runs_module, "transcribe_file", fake_transcribe)

    run = create_run(
        db,
        name=f"run-{suffix}",
        dataset_version_id=version.id,
        device_id=device.id,
        created_by="test",
    )
    asyncio.run(execute_run(db, run.id))
    _wait_for_evaluation(db, run_id=run.id)

    summary = run_summary(db, run)
    assert summary["run"]["status"] == "completed"
    assert summary["metrics"][0]["metric"] == "cer"
    assert summary["metrics"][0]["value"] == 0
    assert summary["metrics"][0]["n"] == 1


def test_disabled_cer_and_perf_write_na_only(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """口径关闭 CER/性能时只落 na(metric_disabled)，不留下 0 或假读数。"""
    suffix = uuid.uuid4().hex[:8]
    params = {
        "evaluator": "custom",
        "cer": {"enabled": False},
        "perf": {"enabled": False},
    }
    profile = MetricProfile(
        name=f"disabled-{suffix}", params=params, profile_hash=suffix.ljust(16, "0")
    )
    dataset = Dataset(name=f"disabled-{suffix}", source_type="synthetic")
    db.add_all([profile, dataset])
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready", item_count=1)
    db.add(version)
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    db.add(subset)
    db.flush()
    asset = AudioAsset(sha256=suffix.ljust(64, "0"), uri="/data/disabled.wav")
    db.add(asset)
    db.flush()
    ref_text = "关闭指标不应产生数值"
    item = Item(
        dataset_version_id=version.id,
        subset_id=subset.id,
        item_key=f"disabled-{suffix}",
        content_uid=suffix.ljust(24, "0"),
        audio_asset_id=asset.id,
        split="test",
        duration_ms=3000,
    )
    db.add(item)
    db.flush()
    db.add(RefText(item_id=item.id, kind="verbatim", text=ref_text, sha256=suffix))
    benchmark = Benchmark(
        name=f"disabled-{suffix}",
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        subsets=["all"],
        sampling={},
    )
    device = Device(
        name=f"disabled-{suffix}",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=9,
        status="online",
    )
    db.add_all([benchmark, device])
    db.flush()
    db.add(
        Deployment(
            device_id=device.id,
            uid=suffix[:16],
            platform="205",
            accel={"type": "rknn"},
            model_key="etsstt",
            capabilities={},
        )
    )
    db.commit()

    async def fake_transcribe(*_args: object, **_kwargs: object) -> TranscribeOutcome:
        return TranscribeOutcome(ok=True, text=ref_text, wall_ms=5, ttft_ms=10, finalize_ms=2)

    monkeypatch.setattr(runs_module, "transcribe_file", fake_transcribe)
    run = create_run(
        db,
        name=f"disabled-{suffix}",
        dataset_version_id=version.id,
        benchmark_id=benchmark.id,
        device_id=device.id,
        created_by="test",
    )
    asyncio.run(execute_run(db, run.id))
    _wait_for_evaluation(db, run_id=run.id)
    evaluation = db.scalar(
        select(Evaluation).where(Evaluation.run_id == run.id).order_by(Evaluation.id.desc())
    )
    assert evaluation is not None

    assert evaluation.metrics["cer"] is None
    rows = db.scalars(select(MetricValue).where(MetricValue.evaluation_id == evaluation.id)).all()
    cer_rows = [row for row in rows if row.metric == "cer"]
    assert [(row.value, row.status, row.reason) for row in cer_rows] == [
        (None, "na", "metric_disabled")
    ]
    perf_metrics = {
        "ttft_ms",
        "finalize_ms",
        "rtf_dev",
        "rtf_e2e",
        "rtf_realtime",
        "overhead_ratio",
        "throughput",
    }
    assert not [row for row in rows if row.metric in perf_metrics]


def test_run_worker_recovers_interrupted_run_and_job() -> None:
    """进程重启后把 running Run/target/Job 恢复为可继续派发状态。"""
    schema = f"m4_recovery_{uuid.uuid4().hex[:12]}"
    admin_engine = create_engine(TEST_URL, pool_pre_ping=True)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(TEST_URL, connect_args={"options": f"-csearch_path={schema}"})
    pool = RunWorkerPool(workers=1, queue_capacity=4)
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as db:
            suffix = uuid.uuid4().hex[:8]
            dataset = Dataset(name=f"恢复-{suffix}", source_type="synthetic")
            profile = MetricProfile(
                name=f"恢复口径-{suffix}", params={}, profile_hash=suffix.ljust(16, "0")
            )
            db.add_all([dataset, profile])
            db.flush()
            version = DatasetVersion(
                dataset_id=dataset.id, version="v1", status="ready", item_count=0
            )
            db.add(version)
            db.flush()
            benchmark = Benchmark(
                dataset_version_id=version.id,
                metric_profile_id=profile.id,
                name=f"恢复基准-{suffix}",
            )
            device = Device(
                name=f"恢复设备-{suffix}",
                platform="205",
                kind="etsstt",
                host="127.0.0.1",
                port=8701,
                status="online",
            )
            db.add_all([benchmark, device])
            db.flush()
            deployment = Deployment(
                device_id=device.id,
                uid=suffix,
                platform="205",
                accel={},
                model_key="recovery-test",
                capabilities={},
            )
            db.add(deployment)
            db.flush()
            run = Run(
                benchmark_id=benchmark.id,
                name=f"中断的 Run-{suffix}",
                status="running",
                snapshot={},
                progress={"done": 2, "total": 5, "phase": "running"},
            )
            db.add(run)
            db.flush()
            target = RunTarget(
                run_id=run.id,
                device_id=device.id,
                deployment_id=deployment.id,
                subset="all",
                status="running",
                stats={"done": 2},
            )
            job = Job(
                kind="run",
                ref_type="run",
                ref_id=str(run.id),
                status="running",
                progress={"phase": "running"},
            )
            db.add_all([target, job])
            db.commit()

            pool._session_factory = factory
            pool._recover_pending()

            db.expire_all()
            recovered_run = db.get(Run, run.id)
            assert recovered_run is not None
            assert recovered_run.status == "queued"
            assert recovered_run.progress["phase"] == "recovered"
            recovered_target = db.get(RunTarget, target.id)
            assert recovered_target is not None
            assert recovered_target.status == "pending"
            assert recovered_target.stats["recovered"] is True
            recovered_job = db.get(Job, job.id)
            assert recovered_job is not None
            assert recovered_job.status == "queued"
            assert recovered_job.progress["phase"] == "recovered"
            assert recovered_job.version > 1
            assert pool._queue.get_nowait() == run.id
            pool._queue.task_done()
    finally:
        pool.shutdown(timeout_s=1)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def test_identity_change_aborts_target_and_keeps_completed_cases(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运行中身份变化会优雅停止在途转写、保留已完成样本且不自动评分。"""
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(name=f"condition-{suffix}", source_type="aishell1")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready", item_count=2)
    db.add(version)
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    db.add(subset)
    db.flush()
    ref_text = "测试身份变化"
    for index in range(2):
        asset = AudioAsset(sha256=f"{suffix}{index}".ljust(64, "0"), uri=f"/data/{index}.wav")
        db.add(asset)
        db.flush()
        item = Item(
            dataset_version_id=version.id,
            subset_id=subset.id,
            item_key=f"condition-{suffix}-{index}",
            content_uid=f"{suffix}{index}".ljust(24, "0"),
            audio_asset_id=asset.id,
            split="test",
            duration_ms=4000,
            speakers=["S0001"],
        )
        db.add(item)
        db.flush()
        db.add(RefText(item_id=item.id, kind="verbatim", text=ref_text, sha256=suffix))

    old_identity = {
        "platform": "205",
        "kind": "etsstt",
        "bigversion": "3.2.412",
        "etsllm_version": "1.4.49",
        "accel": {"type": "cann"},
        "model_key": "qwen3-asr-1.7b",
        "model_version": None,
    }
    device = Device(
        name=f"condition-{suffix}",
        sn="SN-CONDITION-A",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=8701,
        status="online",
    )
    db.add(device)
    db.flush()
    deployment = Deployment(
        device_id=device.id,
        uid=compute_uid(old_identity),
        platform="205",
        bigversion="3.2.412",
        etsllm_version="1.4.49",
        accel={"type": "cann"},
        model_key="qwen3-asr-1.7b",
        capabilities={},
        probe_meta={"probe_version": "probe-identity-1"},
    )
    db.add(deployment)
    db.commit()

    run = create_run(
        db,
        name=f"condition-{suffix}",
        dataset_version_id=version.id,
        device_id=device.id,
        created_by="tester",
    )
    monkeypatch.setattr(runs_module, "IDENTITY_CHECK_INTERVAL_S", 0.05)
    identity_calls = 0
    identity_change_detected = asyncio.Event()

    async def fake_identity(*_args: object, **_kwargs: object) -> IdentityOutcome:
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 1:
            return IdentityOutcome(
                ok=True,
                identity=old_identity,
                sn="SN-CONDITION-A",
                raw={},
                version_collection_ok=True,
            )
        if identity_calls == 2:
            return IdentityOutcome(
                ok=True,
                identity={**old_identity, "bigversion": None, "etsllm_version": None},
                sn="SN-CONDITION-A",
                raw={},
                version_collection_ok=False,
                warnings=["版本命令暂时不可用"],
            )
        identity_change_detected.set()
        return IdentityOutcome(
            ok=True,
            identity={**old_identity, "model_key": "qwen3-asr-1.8b"},
            sn="SN-CONDITION-B",
            raw={},
            version_collection_ok=True,
        )

    second_case_started = asyncio.Event()
    transcribe_count = 0

    async def fake_transcribe(*_args: object, **kwargs: object) -> TranscribeOutcome:
        nonlocal transcribe_count
        transcribe_count += 1
        if transcribe_count == 1:
            return TranscribeOutcome(ok=True, text=ref_text, wall_ms=5)
        second_case_started.set()
        abort_event = kwargs.get("abort_event")
        assert isinstance(abort_event, asyncio.Event)
        await asyncio.wait_for(abort_event.wait(), timeout=2)
        return TranscribeOutcome(ok=True, text=ref_text, wall_ms=5)

    monkeypatch.setattr("app.domain.devices.probe_identity", fake_identity)
    monkeypatch.setattr(runs_module, "transcribe_file", fake_transcribe)

    asyncio.run(execute_run(db, run.id))

    db.expire_all()
    refreshed_run = db.get(Run, run.id)
    assert refreshed_run is not None
    assert refreshed_run.status == "aborted_condition_changed"
    target = db.scalar(select(RunTarget).where(RunTarget.run_id == run.id))
    assert target is not None
    assert target.status == "aborted_condition_changed"
    assert target.abort_reason == "condition_changed"
    assert (
        db.scalar(select(func.count(CaseResult.id)).where(CaseResult.run_target_id == target.id))
        == 1
    )
    assert db.scalar(select(func.count(Evaluation.id)).where(Evaluation.run_id == run.id)) == 0
    assert identity_change_detected.is_set()
    assert identity_calls >= 3
    assert second_case_started.is_set()
    events = sse_event_buffer.replay(f"run:{run.id}", "0").events
    assert any(event.event == "target.aborted_condition_changed" for event in events)


def test_multi_target_run(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """两个目标设备共享同一数据集，评分合并。"""
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(name=f"ds2-{suffix}", source_type="aishell1")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready", item_count=1)
    db.add(version)
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    db.add(subset)
    db.flush()
    asset = AudioAsset(sha256=suffix.ljust(64, "1"), uri="/data/fake2.wav")
    db.add(asset)
    db.flush()
    ref_text = "甚至出现交易几乎停滞的情况"
    item = Item(
        dataset_version_id=version.id,
        subset_id=subset.id,
        item_key=f"utt2-{suffix}",
        content_uid=suffix.ljust(24, "1"),
        audio_asset_id=asset.id,
        split="test",
        duration_ms=4000,
        speakers=["S0002"],
        buckets={"language": "zh"},
    )
    db.add(item)
    db.flush()
    db.add(RefText(item_id=item.id, kind="verbatim", text=ref_text, sha256=suffix))
    db.commit()

    device_ids = []
    for index in range(2):
        device = Device(
            name=f"fake2-{suffix}-{index}",
            platform="205",
            kind="etsstt",
            host="127.0.0.1",
            port=9,
            status="online",
        )
        db.add(device)
        db.flush()
        db.add(
            Deployment(
                device_id=device.id,
                uid=f"{suffix}{index}"[:16],
                platform="205",
                accel={"type": "rknn"},
                model_key="etsstt",
                capabilities={},
            )
        )
        device_ids.append(device.id)
    db.commit()

    active_calls = 0
    max_active_calls = 0

    async def fake_transcribe(*args: object, **kwargs: object) -> TranscribeOutcome:
        nonlocal active_calls, max_active_calls
        active_calls += 1
        max_active_calls = max(max_active_calls, active_calls)
        await asyncio.sleep(0.05)
        active_calls -= 1
        return TranscribeOutcome(ok=True, text=ref_text, wall_ms=5)

    monkeypatch.setattr(runs_module, "transcribe_file", fake_transcribe)

    run = create_run(
        db,
        name=f"run2-{suffix}",
        dataset_version_id=version.id,
        targets=[(device_ids[0], "all"), (device_ids[1], "all")],
        created_by="test",
    )
    asyncio.run(execute_run(db, run.id))
    _wait_for_evaluation(db, run_id=run.id)
    summary = run_summary(db, run)
    assert summary["run"]["status"] == "completed"
    assert len(summary["targets"]) == 2
    assert summary["metrics"][0]["n"] == 2
    assert summary["metrics"][0]["value"] == 0
    assert max_active_calls == 2


def test_same_device_subset_targets_only_run_their_own_samples(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同设备多个子集串行执行，target 结果不得混入其他子集样本。"""
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(name=f"subset-run-{suffix}", source_type="synthetic")
    profile = MetricProfile(
        name=f"subset-profile-{suffix}", params={}, profile_hash=suffix.ljust(16, "0")
    )
    db.add_all([dataset, profile])
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready", item_count=2)
    db.add(version)
    db.flush()
    subsets = {
        name: DatasetSubset(dataset_version_id=version.id, name=name, is_primary=index == 0)
        for index, name in enumerate(("far", "near"))
    }
    db.add_all(subsets.values())
    db.flush()
    item_by_subset: dict[str, int] = {}
    for index, name in enumerate(("far", "near")):
        asset = AudioAsset(sha256=f"{suffix}{index}".ljust(64, "0"), uri=f"/data/{name}.wav")
        db.add(asset)
        db.flush()
        item = Item(
            dataset_version_id=version.id,
            subset_id=subsets[name].id,
            item_key=f"{name}-{suffix}",
            content_uid=f"{suffix}{index}".ljust(24, "0"),
            audio_asset_id=asset.id,
            split="test",
            duration_ms=4_000,
        )
        db.add(item)
        db.flush()
        item_by_subset[name] = item.id
        db.add(RefText(item_id=item.id, kind="verbatim", text="目标子集", sha256=suffix))
    device = Device(
        name=f"subset-device-{suffix}",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=8701,
        status="online",
    )
    db.add(device)
    db.flush()
    deployment = Deployment(
        device_id=device.id,
        uid=suffix[:16],
        platform="205",
        accel={"type": "rknn"},
        model_key="etsstt",
        capabilities={},
    )
    benchmark = Benchmark(
        name=f"subset-benchmark-{suffix}",
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        subsets=["far", "near"],
        sampling={},
    )
    db.add_all([deployment, benchmark])
    db.commit()

    active_calls = 0
    max_active_calls = 0

    async def fake_transcribe(*_args: object, **_kwargs: object) -> TranscribeOutcome:
        nonlocal active_calls, max_active_calls
        active_calls += 1
        max_active_calls = max(max_active_calls, active_calls)
        await asyncio.sleep(0.01)
        active_calls -= 1
        return TranscribeOutcome(ok=True, text="目标子集", wall_ms=5)

    monkeypatch.setattr(runs_module, "transcribe_file", fake_transcribe)
    run = create_run(
        db,
        name=f"subset-run-{suffix}",
        dataset_version_id=version.id,
        benchmark_id=benchmark.id,
        targets=[(device.id, "far"), (device.id, "near")],
        created_by="tester",
        auto_score=False,
    )

    asyncio.run(execute_run(db, run.id))
    db.refresh(run)
    assert run.status == "completed"
    assert max_active_calls == 1

    targets = db.scalars(select(RunTarget).where(RunTarget.run_id == run.id)).all()
    assert {target.subset: target.stats["total"] for target in targets} == {"far": 1, "near": 1}
    for target in targets:
        result_item_id = db.scalar(
            select(CaseResult.item_id).where(CaseResult.run_target_id == target.id)
        )
        assert result_item_id == item_by_subset[target.subset]


def test_perf_run_uses_stream_profile_and_parallel_lanes(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """性能 Run 按口径的流式参数执行，并发路数不超 target.lanes。"""
    from app.domain.metric_profiles import TEMPLATE_PARAMS, profile_hash

    suffix = uuid.uuid4().hex[:8]
    params = copy.deepcopy(TEMPLATE_PARAMS["perf_30ms"])
    profile = MetricProfile(
        name=f"perf-stream-{suffix}", params=params, profile_hash=profile_hash(params)
    )
    dataset = Dataset(name=f"perf-stream-{suffix}", source_type="synthetic")
    db.add_all([profile, dataset])
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready", item_count=3)
    db.add(version)
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    db.add(subset)
    db.flush()
    refs = []
    for index in range(3):
        asset = AudioAsset(sha256=f"{suffix}{index}".ljust(64, "0"), uri=f"/data/{index}.wav")
        db.add(asset)
        db.flush()
        item = Item(
            dataset_version_id=version.id,
            subset_id=subset.id,
            item_key=f"perf-stream-{suffix}-{index}",
            content_uid=f"{suffix}{index}".ljust(24, "0"),
            audio_asset_id=asset.id,
            split="test",
            duration_ms=1000,
        )
        db.add(item)
        db.flush()
        refs.append(RefText(item_id=item.id, kind="verbatim", text="流式评测", sha256=suffix))
    db.add_all(refs)
    device = Device(
        name=f"stream-gateway-{suffix}",
        platform="502",
        kind="gateway",
        host="127.0.0.1",
        port=9,
        status="online",
    )
    db.add(device)
    db.flush()
    deployment = Deployment(
        device_id=device.id,
        uid=suffix[:16],
        platform="502",
        accel={"type": "rknn"},
        model_key="etsstt",
        capabilities={},
        limits={"realtime_slots": 3},
    )
    db.add(deployment)
    benchmark = Benchmark(
        name=f"perf-stream-benchmark-{suffix}",
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        subsets=["all"],
        sampling={},
    )
    db.add(benchmark)
    db.commit()

    active_calls = 0
    max_active_calls = 0
    observed_params: list[tuple[str, int, float]] = []

    async def fake_transcribe(*_args: object, **kwargs: object) -> TranscribeOutcome:
        nonlocal active_calls, max_active_calls
        active_calls += 1
        max_active_calls = max(max_active_calls, active_calls)
        observed_params.append(
            (str(kwargs["mode"]), int(kwargs["chunk_ms"]), float(kwargs["input_rate"]))
        )
        await asyncio.sleep(0.05)
        active_calls -= 1
        return TranscribeOutcome(
            ok=True,
            text="流式评测",
            wall_ms=25,
            usage={"inference_ms": 20},
            ttft_ms=35,
            finalize_ms=12,
        )

    monkeypatch.setattr(runs_module, "transcribe_file", fake_transcribe)
    run = create_run(
        db,
        name=f"perf-stream-run-{suffix}",
        dataset_version_id=version.id,
        benchmark_id=benchmark.id,
        targets=[(device.id, "all")],
        created_by="tester",
        kind="perf",
        lanes=3,
        auto_score=False,
    )
    asyncio.run(execute_run(db, run.id))

    target = db.scalar(select(RunTarget).where(RunTarget.run_id == run.id))
    cases = db.scalars(
        select(CaseResult).where(CaseResult.run_target_id == target.id).order_by(CaseResult.id)
    ).all()
    hypotheses = db.scalars(
        select(Hypothesis).where(Hypothesis.deployment_id == deployment.id)
    ).all()
    assert run.kind == "perf"
    assert target.lanes == 3
    assert run.status == "completed"
    assert max_active_calls == 3
    assert observed_params == [("stream", 30, 1.0)] * 3
    assert len(cases) == 3
    assert all(case.ttft_ms == 35 and case.finalize_ms == 12 for case in cases)
    assert len(hypotheses) == 3
    assert all(
        hypothesis.mode == "stream"
        and hypothesis.chunk_ms == 30
        and hypothesis.input_rate == 1.0
        and hypothesis.concurrency == 3
        for hypothesis in hypotheses
    )


def test_run_preview_reports_metrics_queue_and_eta(db: Session) -> None:
    """创建前预览返回指标可用性、设备排队位置与分路 ETA。"""
    suffix = uuid.uuid4().hex[:8]
    profile = MetricProfile(name=f"preview-{suffix}", params={}, profile_hash=suffix.ljust(16, "0"))
    dataset = Dataset(name=f"preview-{suffix}", source_type="synthetic")
    db.add_all([profile, dataset])
    db.flush()
    version = DatasetVersion(
        dataset_id=dataset.id,
        version="v1",
        status="ready",
        item_count=2,
        capabilities={"text_ratio": 1.0},
    )
    db.add(version)
    db.flush()
    subset = DatasetSubset(
        dataset_version_id=version.id,
        name="near",
        is_primary=True,
        item_count=2,
        capabilities={"text_ratio": 1.0, "segment_ratio": 1.0, "speaker_ratio": 1.0},
    )
    db.add(subset)
    db.flush()
    for index in range(2):
        asset = AudioAsset(sha256=f"{suffix}{index}".ljust(64, "0"), uri=f"/data/{index}.wav")
        db.add(asset)
        db.flush()
        item = Item(
            dataset_version_id=version.id,
            subset_id=subset.id,
            item_key=f"preview-{suffix}-{index}",
            content_uid=f"{suffix}{index}".ljust(24, "0"),
            audio_asset_id=asset.id,
            split="test",
            duration_ms=10_000,
            speakers=["S0001"],
        )
        db.add(item)
    device = Device(
        name=f"preview-device-{suffix}",
        platform="502",
        kind="gateway",
        host="127.0.0.1",
        port=8701,
        status="online",
    )
    db.add(device)
    db.flush()
    deployment = Deployment(
        device_id=device.id,
        uid=suffix[:16],
        platform="502",
        accel={"type": "cann"},
        model_key="qwen3-asr-1.7b",
        capabilities={
            "declared": {"itn": True},
            "observed": {
                "punctuation_output": True,
                "segments": True,
                "speakers_in_sentence": True,
                "naming": True,
                "usage_field": True,
                "partials": True,
            },
        },
        limits={"realtime_slots": 3},
    )
    benchmark = Benchmark(
        name=f"preview-benchmark-{suffix}",
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        subsets=["near"],
        sampling={},
    )
    db.add_all([deployment, benchmark])
    db.flush()
    active_run = Run(
        benchmark_id=benchmark.id,
        name=f"active-{suffix}",
        status="running",
        eta_ms=3_000,
    )
    queued_run = Run(
        benchmark_id=benchmark.id,
        name=f"queued-{suffix}",
        status="queued",
        eta_ms=4_000,
    )
    db.add_all([active_run, queued_run])
    db.flush()
    active_target = RunTarget(
        run_id=active_run.id,
        device_id=device.id,
        deployment_id=deployment.id,
        subset="near",
        status="running",
    )
    db.add_all(
        [
            active_target,
            RunTarget(
                run_id=queued_run.id,
                device_id=device.id,
                deployment_id=deployment.id,
                subset="near",
                status="pending",
            ),
        ]
    )
    db.flush()
    db.add(
        DeviceLease(
            device_id=device.id,
            run_target_id=active_target.id,
            holder=str(active_run.id),
            expires_at=datetime.now(UTC) + timedelta(minutes=2),
        )
    )
    db.commit()

    result = preview_run(
        db,
        benchmark_id=benchmark.id,
        targets=[(device.id, "near")],
        kind="perf",
        lanes=2,
    )

    target = result["targets"][0]
    metric_by_name = {metric["name"]: metric for metric in target["metrics"]}
    assert target["item_count"] == 2
    assert target["queue"] == {"busy": True, "queued_ahead": 1, "position": 3}
    assert target["eta"]["queue_wait_ms"] == 7_000
    assert target["eta"]["total_ms"] == result["eta_ms"]
    assert target["eta"]["source"] == "fallback"
    assert metric_by_name["cer"]["status"] == "ok"
    assert metric_by_name["naming_acc"]["reason"] == "enrollment_missing"
    assert any(warning["code"] == "device_busy" for warning in target["warnings"])


def test_cancel_and_resume(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """取消 30s 内停止派发；续跑只补缺失样本（M4 §5.9）。"""
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(name=f"ds3-{suffix}", source_type="aishell1")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready", item_count=3)
    db.add(version)
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    db.add(subset)
    db.flush()
    ref_text = "甚至出现交易几乎停滞的情况"
    for index in range(3):
        asset = AudioAsset(sha256=f"{suffix}{index}".ljust(64, "0"), uri=f"/data/{index}.wav")
        db.add(asset)
        db.flush()
        item = Item(
            dataset_version_id=version.id,
            subset_id=subset.id,
            item_key=f"c-{suffix}-{index}",
            content_uid=f"{suffix}{index}".ljust(24, "0"),
            audio_asset_id=asset.id,
            split="test",
            duration_ms=4000,
            speakers=["S0003"],
        )
        db.add(item)
        db.flush()
        db.add(RefText(item_id=item.id, kind="verbatim", text=ref_text, sha256=suffix))
    device = Device(
        name=f"fake3-{suffix}",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=9,
        status="online",
    )
    db.add(device)
    db.flush()
    db.add(
        Deployment(
            device_id=device.id,
            uid=suffix[:16],
            platform="205",
            accel={"type": "rknn"},
            model_key="etsstt",
            capabilities={},
        )
    )
    db.commit()

    run = create_run(
        db,
        name=f"cancel-{suffix}",
        dataset_version_id=version.id,
        created_by="tester",
        targets=[(device.id, "all")],
    )
    # 创建时首估 ETA 必须带出处（M4 §4/§8），无历史 → fallback
    assert run.eta_ms is not None
    assert run.eta_source == "fallback"
    assert run.eta_confidence in {"low", "medium", "high"}

    state = {"cancelled": False}

    async def fake_transcribe_cancels(*args: object, **kwargs: object) -> TranscribeOutcome:
        if not state["cancelled"]:
            state["cancelled"] = True
            db_run = db.get(Run, run.id)
            assert db_run is not None
            db_run.status = "cancelling"
            db.commit()
        return TranscribeOutcome(ok=True, text=ref_text, wall_ms=5)

    monkeypatch.setattr(runs_module, "transcribe_file", fake_transcribe_cancels)
    asyncio.run(execute_run(db, run.id))
    db.refresh(run)
    assert run.status == "cancelled"

    # 续跑：只补缺失的 2 条
    resumed = resume_run(db, run.id)
    assert resumed.status == "queued"

    async def fake_transcribe_ok(*args: object, **kwargs: object) -> TranscribeOutcome:
        return TranscribeOutcome(ok=True, text=ref_text, wall_ms=5)

    monkeypatch.setattr(runs_module, "transcribe_file", fake_transcribe_ok)
    asyncio.run(execute_run(db, run.id))
    db.refresh(run)
    assert run.status == "completed"
    _wait_for_evaluation(db, run_id=run.id)
    ok_count = (
        db.query(CaseResult)
        .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
        .filter(RunTarget.run_id == run.id, CaseResult.status == "ok")
        .count()
    )
    assert ok_count == 3


def test_evaluation_matrix_and_buckets(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """矩阵与分桶端点读 metric_value / CaseScore（M5 §3.2/§4）。"""
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(name=f"ds4-{suffix}", source_type="aishell1")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(
        dataset_id=dataset.id,
        version="v1",
        status="ready",
        item_count=2,
        capabilities={"punct_supported": True, "itn_ratio": 1.0},
    )
    db.add(version)
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    db.add(subset)
    db.flush()
    ref_text = "2014年3月5日，增长3.5%"
    for index, ref in enumerate([ref_text, ref_text]):
        asset = AudioAsset(sha256=f"{suffix}{index}x".ljust(64, "0"), uri=f"/data/{index}.wav")
        db.add(asset)
        db.flush()
        item = Item(
            dataset_version_id=version.id,
            subset_id=subset.id,
            item_key=f"m-{suffix}-{index}",
            content_uid=f"{suffix}{index}x".ljust(24, "0"),
            audio_asset_id=asset.id,
            split="test",
            duration_ms=4000,
            buckets={"language": "zh"},
        )
        db.add(item)
        db.flush()
        db.add(RefText(item_id=item.id, kind="verbatim", text=ref, sha256=suffix))
        db.add(RefText(item_id=item.id, kind="itn", text=ref, sha256=f"itn-{suffix}"))
    device = Device(
        name=f"fake4-{suffix}",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=9,
        status="online",
    )
    db.add(device)
    db.flush()
    db.add(
        Deployment(
            device_id=device.id,
            uid=suffix[:16],
            platform="205",
            accel={"type": "rknn"},
            model_key="etsstt",
            capabilities={},
        )
    )
    db.commit()

    run = create_run(
        db,
        name=f"matrix-{suffix}",
        dataset_version_id=version.id,
        created_by="tester",
        targets=[(device.id, "all")],
    )

    async def fake_transcribe(*args: object, **kwargs: object) -> TranscribeOutcome:
        # 中文口语数字须经 WeTextProcessing 归一后与数字稿匹配。
        outcome = TranscribeOutcome(ok=True, text="二零一四年三月五日，增长百分之三点五", wall_ms=5)
        outcome.usage = {"inference_ms": 800}
        return outcome

    monkeypatch.setattr(runs_module, "transcribe_file", fake_transcribe)
    asyncio.run(execute_run(db, run.id))
    _wait_for_evaluation(db, run_id=run.id)

    from app.api.evaluations import (
        EvaluationCreateIn,
        create_run_evaluation,
        evaluation_buckets,
        evaluation_matrix,
        get_evaluation,
        list_run_evaluations,
    )
    from app.domain.metric_profiles import TEMPLATE_PARAMS, profile_hash
    from app.domain.models import CaseScore, Evaluation, MetricProfile, MetricValue

    evaluation = db.scalar(select(Evaluation).where(Evaluation.run_id == run.id))
    assert evaluation is not None
    case_scores = db.scalars(
        select(CaseScore).where(CaseScore.evaluation_id == evaluation.id)
    ).all()
    assert len(case_scores) == 2
    assert all(score.metrics["punct_f1"]["status"] == "ok" for score in case_scores)
    assert all(score.metrics["itn_acc"]["value"] == 1.0 for score in case_scores)
    assert all(score.metrics["numeric_cer"]["approximate"] is True for score in case_scores)
    assert all(score.metrics["numeric_cer"]["value"] > 0.0 for score in case_scores)

    matrix = evaluation_matrix(evaluation_id=evaluation.id, db=db, _=None)
    cells = matrix["cells"]
    # 无桶目标级与 evaluation 级准确率指标各一行。
    accuracy_metrics = {
        "cer",
        "der",
        "sa_der",
        "naming_acc",
        "speaker_eer",
        "punct_f1",
        "itn_acc",
        "numeric_cer",
    }
    assert (
        len([cell for cell in cells if cell["metric"] in accuracy_metrics and not cell["bucket"]])
        == 16
    )
    target_cell = next(c for c in cells if c["target_id"] is not None)
    assert target_cell["metric"] == "cer"
    assert target_cell["status"] == "ok"
    assert target_cell["ci"] is not None
    target_punct = next(
        c for c in cells if c["target_id"] is not None and c["metric"] == "punct_f1"
    )
    assert target_punct["value"] == 1.0
    assert target_punct["status"] == "ok"
    assert target_punct["ci"] == [1.0, 1.0]
    target_itn = next(c for c in cells if c["target_id"] is not None and c["metric"] == "itn_acc")
    assert target_itn["value"] == 1.0
    assert target_itn["status"] == "ok"
    assert target_itn["ci"] == [1.0, 1.0]
    target_numeric = next(
        c for c in cells if c["target_id"] is not None and c["metric"] == "numeric_cer"
    )
    assert target_numeric["value"] > 0.0
    assert target_numeric["status"] == "ok"
    assert target_numeric["detail"]["approximate"] is True
    target_der = next(c for c in cells if c["target_id"] is not None and c["metric"] == "der")
    assert target_der["status"] == "na"
    overall = next(c for c in cells if c["target_id"] is None and c["metric"] == "cer")
    assert overall["n"] == 2
    assert overall["ci"] is not None
    der_cell = next(c for c in cells if c["metric"] == "der")
    assert der_cell["status"] == "na"  # 假转写没有分段 → 不应伪造 DER
    assert der_cell["reason"] in {"no_diarization_output", "no_segment_timeline"}
    punct_cell = next(c for c in cells if c["metric"] == "punct_f1")
    assert punct_cell["value"] == 1.0
    assert punct_cell["ci"] == [1.0, 1.0]
    itn_cell = next(c for c in cells if c["metric"] == "itn_acc")
    assert itn_cell["value"] == 1.0
    assert itn_cell["ci"] == [1.0, 1.0]
    numeric_cell = next(c for c in cells if c["metric"] == "numeric_cer")
    assert numeric_cell["value"] > 0.0
    assert numeric_cell["detail"]["numeric_characters"] > 0

    perf_rtf = next(
        c for c in cells if c["target_id"] is None and c["metric"] == "rtf_dev" and not c["bucket"]
    )
    assert perf_rtf["value"] == 0.2
    assert perf_rtf["detail"]["median"] == 0.2
    assert perf_rtf["detail"]["p95"] == 0.2
    perf_e2e = next(
        c for c in cells if c["target_id"] is None and c["metric"] == "rtf_e2e" and not c["bucket"]
    )
    assert perf_e2e["value"] == 0.00125
    throughput_cell = next(
        c
        for c in cells
        if c["target_id"] is None and c["metric"] == "throughput" and not c["bucket"]
    )
    assert throughput_cell["value"] == 800.0
    bucket_rtf = next(
        c
        for c in cells
        if c["target_id"] is None
        and c["metric"] == "rtf_dev"
        and c["bucket"] == {"duration_bucket": "lt_5s"}
    )
    assert bucket_rtf["value"] == 0.2
    bucket_punct = next(
        c for c in cells if c["target_id"] is None and c["metric"] == "punct_f1" and c["bucket"]
    )
    assert bucket_punct["value"] == 1.0
    bucket_itn = next(
        c for c in cells if c["target_id"] is None and c["metric"] == "itn_acc" and c["bucket"]
    )
    assert bucket_itn["value"] == 1.0
    bucket_numeric = next(
        c for c in cells if c["target_id"] is None and c["metric"] == "numeric_cer" and c["bucket"]
    )
    assert bucket_numeric["value"] > 0.0

    buckets = evaluation_buckets(
        evaluation_id=evaluation.id, dim="duration_bucket", scope="micro", db=db, _=None
    )
    assert buckets["buckets"][0]["bucket"] == {"duration_bucket": "lt_5s"}
    assert buckets["buckets"][0]["n"] == 2
    assert buckets["buckets"][0]["value"] is not None

    # 新 profile 在既有结果上重评，不触发转写，也不覆盖旧 Evaluation。
    profile_params = copy.deepcopy(TEMPLATE_PARAMS["default_zh"])
    profile_params["cer"]["aggregate"] = "both"
    profile_params["punct"]["enabled"] = False
    profile_params["itn"]["enabled"] = False
    rescore_profile = MetricProfile(
        name=f"rescore-{suffix}",
        params=profile_params,
        profile_hash=profile_hash(profile_params),
    )
    db.add(rescore_profile)
    db.commit()

    from app.core import config as config_module
    from app.domain.auth import create_stream_ticket
    from app.domain.evaluations import cancel_evaluation, resume_evaluation

    settings = config_module.get_settings()
    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: settings.model_copy(update={"scoring_page_size": 1}),
    )
    interrupted = create_evaluation_record(db, run.id, rescore_profile.id)
    interrupted.status = "scoring"
    db.commit()
    compute_cer = runs_module.compute_cer
    processed_cases = 0

    def cancel_after_one_page(*args: object, **kwargs: object) -> object:
        nonlocal processed_cases
        result = compute_cer(*args, **kwargs)
        processed_cases += 1
        if processed_cases == 1:
            cancel_evaluation(db, interrupted.id)
        return result

    monkeypatch.setattr(runs_module, "compute_cer", cancel_after_one_page)
    cancelled = process_evaluation(db, interrupted.id)
    assert cancelled.status == "cancelled"
    assert cancelled.progress["done"] == 1
    assert cancelled.progress["phase"] == "cancelled"
    assert (
        db.scalar(
            select(func.count())
            .select_from(CaseScore)
            .where(CaseScore.evaluation_id == interrupted.id)
        )
        == 1
    )

    resumed_evaluation = resume_evaluation(db, interrupted.id)
    assert resumed_evaluation.status == "queued"
    completed = process_evaluation(db, interrupted.id)
    assert completed.status == "completed"
    assert completed.progress["done"] == 2
    assert (
        db.scalar(
            select(func.count())
            .select_from(CaseScore)
            .where(CaseScore.evaluation_id == interrupted.id)
        )
        == 2
    )
    monkeypatch.setattr(runs_module, "compute_cer", compute_cer)

    async def unexpected_transcribe(*args: object, **kwargs: object) -> TranscribeOutcome:
        raise AssertionError("重新评分不得调用设备")

    monkeypatch.setattr(runs_module, "transcribe_file", unexpected_transcribe)
    response = create_run_evaluation(
        run.id,
        EvaluationCreateIn(metric_profile_id=rescore_profile.id),
        db,
        {},
    )
    assert response["trigger"] == "manual"
    assert response["status"] == "queued"
    assert response["progress"]["phase"] == "queued"
    assert response["profile_hash"] == rescore_profile.profile_hash
    rescored_id = uuid.UUID(response["id"])
    _wait_for_evaluation(db, evaluation_id=rescored_id)
    rescored = db.get(Evaluation, rescored_id)
    assert rescored is not None and rescored.id != evaluation.id

    # 中断续跑与从头重评必须产生相同的逐样本分数和聚合指标。
    import json

    assert completed.metrics == rescored.metrics
    resumed_scores = db.scalars(
        select(CaseScore)
        .where(CaseScore.evaluation_id == completed.id)
        .order_by(CaseScore.case_result_id)
    ).all()
    clean_scores = db.scalars(
        select(CaseScore)
        .where(CaseScore.evaluation_id == rescored.id)
        .order_by(CaseScore.case_result_id)
    ).all()
    assert [score.metrics for score in resumed_scores] == [score.metrics for score in clean_scores]

    def metric_snapshot(evaluation_id: uuid.UUID) -> list[tuple[object, ...]]:
        """忽略数据库主键与时间戳，比较评分结果的全部有效字段。"""
        rows = db.scalars(
            select(MetricValue).where(MetricValue.evaluation_id == evaluation_id)
        ).all()
        snapshot = [
            (
                row.metric,
                row.subset,
                row.scope,
                str(row.run_target_id),
                json.dumps(row.bucket, ensure_ascii=False, sort_keys=True),
                row.value,
                row.n,
                row.ci_low,
                row.ci_high,
                row.status,
                row.reason,
                json.dumps(row.detail, ensure_ascii=False, sort_keys=True),
            )
            for row in rows
        ]
        return sorted(snapshot, key=lambda cell: tuple(str(value) for value in cell[:5]))

    assert metric_snapshot(completed.id) == metric_snapshot(rescored.id)

    ticket, _expires_in = create_stream_ticket(db, "admin")

    async def read_evaluation_events() -> str:
        from app.api.evaluations import evaluation_events

        response = await evaluation_events(rescored_id, ticket, db, None)
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    event_stream = asyncio.run(read_evaluation_events())
    assert "event: evaluation.completed" in event_stream
    assert "id: 1" in event_stream
    assert '"status": "completed"' in event_stream
    assert "event: end" in event_stream

    replay_ticket, _expires_in = create_stream_ticket(db, "admin")

    async def replay_evaluation_events(last_event_id: str) -> str:
        from app.api.evaluations import evaluation_events

        response = await evaluation_events(rescored_id, replay_ticket, db, None, last_event_id)
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    replayed_stream = asyncio.run(replay_evaluation_events("0"))
    assert "id: 1" in replayed_stream
    assert "event: evaluation.completed" in replayed_stream
    assert "event: resync" not in replayed_stream

    resync_ticket, _expires_in = create_stream_ticket(db, "admin")
    replay_ticket = resync_ticket
    resynced_stream = asyncio.run(replay_evaluation_events("999"))
    assert "event: resync" in resynced_stream
    assert "event: evaluation.completed" in resynced_stream
    rescore_cer = db.scalars(
        select(MetricValue).where(
            MetricValue.evaluation_id == rescored.id,
            MetricValue.metric == "cer",
            MetricValue.run_target_id.is_(None),
            MetricValue.bucket == {},
        )
    ).all()
    assert {row.scope for row in rescore_cer} == {"micro", "macro"}
    rescored_cases = db.scalars(
        select(CaseScore).where(CaseScore.evaluation_id == rescored.id)
    ).all()
    assert len(rescored_cases) == 2
    assert all(row.metrics["punct_f1"]["reason"] == "metric_disabled" for row in rescored_cases)
    assert all(row.metrics["itn_acc"]["reason"] == "metric_disabled" for row in rescored_cases)
    disabled_cells = db.scalars(
        select(MetricValue).where(
            MetricValue.evaluation_id == rescored.id,
            MetricValue.metric.in_(["punct_f1", "itn_acc"]),
        )
    ).all()
    assert len(disabled_cells) == 8
    assert all(cell.status == "na" and cell.reason == "metric_disabled" for cell in disabled_cells)
    assert len(list_run_evaluations(run.id, db, {})["items"]) == 3
    assert get_evaluation(rescored.id, db, {})["id"] == response["id"]

    # M8 §9.4：抽屉最多返回按 CER 降序排列的 20 条最差样本。
    target = db.scalar(select(RunTarget).where(RunTarget.run_id == run.id))
    assert target is not None
    for index in range(22):
        asset = AudioAsset(
            sha256=f"cell-{suffix}-{index:016x}".ljust(64, "0"),
            uri=f"/data/cell-{suffix}-{index}.wav",
        )
        db.add(asset)
        db.flush()
        item = Item(
            dataset_version_id=version.id,
            subset_id=subset.id,
            item_key=f"cell-{suffix}-{index}",
            content_uid=f"{suffix}{index:016x}",
            audio_asset_id=asset.id,
            split="test",
            duration_ms=4000,
        )
        db.add(item)
        db.flush()
        db.add(RefText(item_id=item.id, kind="verbatim", text=ref_text, sha256=suffix))
        case_result = CaseResult(
            run_target_id=target.id,
            item_id=item.id,
            status="ok",
            text=ref_text,
        )
        db.add(case_result)
        db.flush()
        db.add(
            CaseScore(
                evaluation_id=rescored.id,
                case_result_id=case_result.id,
                metrics={
                    "cer": {
                        "status": "ok",
                        "value": 1.2 if index == 21 else (index + 1) / 100,
                        "s": 0,
                        "d": 0,
                        "i": 120 if index == 21 else index + 1,
                        "n": 100,
                    }
                },
            )
        )
    db.flush()
    from app.domain.readout import target_cell

    drilldown = target_cell(db, run, target)
    assert drilldown["n"] == 24
    assert len(drilldown["worst"]) == 20
    assert [row["cer"] for row in drilldown["worst"]] == sorted(
        (row["cer"] for row in drilldown["worst"]), reverse=True
    )
    assert sum(bin_["count"] for bin_ in drilldown["histogram"]) == 24
    assert drilldown["histogram"][-1]["bin"] == "≥60%"


def test_speaker_metrics_use_transcript_voiceprint_matches(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DER、SA-DER、命名和簇级 EER 消费转写保存的说话人结果。"""
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(name=f"speaker-{suffix}", source_type="alimeeting")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready", item_count=1)
    db.add(version)
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    db.add(subset)
    db.flush()
    asset = AudioAsset(sha256=f"speaker{suffix}".ljust(64, "0"), uri="/data/speaker.wav")
    db.add(asset)
    db.flush()
    item = Item(
        dataset_version_id=version.id,
        subset_id=subset.id,
        item_key=f"speaker-{suffix}",
        content_uid=suffix.ljust(24, "0"),
        audio_asset_id=asset.id,
        split="test",
        duration_ms=4000,
        pair_key=f"session-{suffix}",
    )
    db.add(item)
    db.flush()
    reference = "欢迎参加今天的会议"
    db.add(RefText(item_id=item.id, kind="verbatim", text=reference, sha256=suffix))
    db.add(
        RefSegment(
            item_id=item.id,
            seq=0,
            speaker="S0001",
            start_ms=0,
            end_ms=4000,
            text=reference,
        )
    )
    device = Device(
        name=f"speaker-device-{suffix}",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=9,
        status="online",
    )
    db.add(device)
    db.flush()
    db.add(
        Deployment(
            device_id=device.id,
            uid=suffix[:16],
            platform="205",
            accel={"type": "rknn"},
            model_key="etsstt",
            capabilities={},
        )
    )
    db.commit()

    run = create_run(
        db,
        name=f"speaker-run-{suffix}",
        dataset_version_id=version.id,
        created_by="tester",
        device_id=device.id,
    )
    requested_diarization: list[bool] = []

    async def fake_transcribe(*args: object, **kwargs: object) -> TranscribeOutcome:
        requested_diarization.append(bool(kwargs.get("diarization")))
        return TranscribeOutcome(
            ok=True,
            text=reference,
            wall_ms=10,
            segments=[{"speaker": "0", "start_ms": 0, "end_ms": 4000, "text": reference}],
            speaker_match={"0": {"match_info": {"matched_id": "S0001", "confidence": 0.95}}},
        )

    monkeypatch.setattr(runs_module, "transcribe_file", fake_transcribe)
    asyncio.run(execute_run(db, run.id))
    assert requested_diarization == [True]
    _wait_for_evaluation(db, run_id=run.id)

    from app.domain.models import CaseScore, Evaluation, MetricValue

    evaluation = db.scalar(select(Evaluation).where(Evaluation.run_id == run.id))
    assert evaluation is not None
    case_score = db.scalar(select(CaseScore).where(CaseScore.evaluation_id == evaluation.id))
    assert case_score is not None
    assert case_score.metrics["der"]["value"] == 0.0
    assert case_score.metrics["sa_der"]["value"] == 0.0
    assert case_score.metrics["naming_acc"]["value"] == 1.0

    values = db.scalars(select(MetricValue).where(MetricValue.evaluation_id == evaluation.id)).all()
    target_values = {row.metric: row for row in values if row.run_target_id is not None}
    assert target_values["der"].value == 0.0
    assert target_values["sa_der"].value == 0.0
    assert target_values["sa_der"].ci_low == target_values["sa_der"].ci_high == 0.0
    assert target_values["naming_acc"].value == 1.0
    assert target_values["naming_acc"].ci_low == target_values["naming_acc"].ci_high == 1.0
    assert target_values["speaker_eer"].reason == "insufficient_trials"


def test_hypothesis_cache_same_audio_two_subsets(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同音频多 subset：第二条命中 hypothesis 缓存，status=cached 无 timing（M4 §5.6/D-M4-12）。"""
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(name=f"ds5-{suffix}", source_type="aishell1")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready", item_count=2)
    db.add(version)
    db.flush()
    phone = DatasetSubset(dataset_version_id=version.id, name="phone", is_primary=True)
    mic = DatasetSubset(dataset_version_id=version.id, name="mic", is_primary=False)
    db.add_all([phone, mic])
    db.flush()
    ref_text = "甚至出现交易几乎停滞的情况"
    # 同一个音频资产挂两个 item（phone/mic 各一条），content 相同 → 缓存 key 相同
    asset = AudioAsset(sha256=f"cache{suffix}".ljust(64, "0"), uri="/data/cache.wav")
    db.add(asset)
    db.flush()
    for subset, key in [(phone, "p"), (mic, "m")]:
        item = Item(
            dataset_version_id=version.id,
            subset_id=subset.id,
            item_key=f"{key}-{suffix}",
            content_uid=f"{suffix}{key}".ljust(24, "0"),
            audio_asset_id=asset.id,
            split="test",
            duration_ms=4000,
        )
        db.add(item)
        db.flush()
        db.add(RefText(item_id=item.id, kind="verbatim", text=ref_text, sha256=suffix))
    device = Device(
        name=f"fake5-{suffix}",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=9,
        status="online",
    )
    db.add(device)
    db.flush()
    deployment = Deployment(
        device_id=device.id,
        uid=suffix[:16],
        platform="205",
        accel={"type": "rknn"},
        model_key="etsstt",
        capabilities={},
    )
    db.add(deployment)
    db.commit()

    run = create_run(
        db,
        name=f"cache-{suffix}",
        dataset_version_id=version.id,
        created_by="tester",
        targets=[(device.id, "phone"), (device.id, "mic")],
    )
    # D-M4-9：target 冻结 deployment
    targets = db.query(RunTarget).filter(RunTarget.run_id == run.id).all()
    assert all(t.deployment_id == deployment.id for t in targets)
    assert all(t.lanes == 1 for t in targets)

    calls = {"n": 0}

    async def fake_transcribe(*args: object, **kwargs: object) -> TranscribeOutcome:
        calls["n"] += 1
        return TranscribeOutcome(ok=True, text=ref_text, wall_ms=5)

    monkeypatch.setattr(runs_module, "transcribe_file", fake_transcribe)
    asyncio.run(execute_run(db, run.id))
    _wait_for_evaluation(db, run_id=run.id)
    cases = (
        db.query(CaseResult)
        .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
        .filter(RunTarget.run_id == run.id)
        .all()
    )
    statuses = sorted(c.status for c in cases)
    # 每个 target 只执行本 subset 的样本；跨 subset 的同一音频只转写一次。
    assert statuses == ["cached", "ok"]
    assert all(case.created_at is not None for case in cases)
    assert calls["n"] == 1
    cached = next(c for c in cases if c.status == "cached")
    assert cached.wall_ms is None  # cached 无 timing
    assert cached.hypothesis_id is not None
    hyp_ids = {c.hypothesis_id for c in cases if c.hypothesis_id}
    assert len(hyp_ids) == 1  # 内容寻址：同内容只应有一条 hypothesis
    from app.domain.models import Evaluation, MetricValue

    evaluation = db.scalar(select(Evaluation).where(Evaluation.run_id == run.id))
    assert evaluation is not None
    throughput_cell = db.scalar(
        select(MetricValue).where(
            MetricValue.evaluation_id == evaluation.id,
            MetricValue.metric == "throughput",
            MetricValue.run_target_id.is_(None),
            MetricValue.bucket == {},
        )
    )
    assert throughput_cell is not None
    assert throughput_cell.n == 1  # 只用一次真实转写的耗时，三个缓存样本均排除


def test_deployment_uid_lock_mismatch(db: Session) -> None:
    """D-M4-11：锁定 deployment_uid 与当前不一致 → 409 E_DEPLOYMENT_CHANGED。"""
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(name=f"ds6-{suffix}", source_type="aishell1")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready", item_count=0)
    db.add(version)
    db.flush()
    db.add(DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True))
    device = Device(
        name=f"fake6-{suffix}",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=9,
        status="online",
    )
    db.add(device)
    db.flush()
    db.add(
        Deployment(
            device_id=device.id,
            uid=suffix[:16],
            platform="205",
            accel={"type": "rknn"},
            model_key="etsstt",
            capabilities={},
        )
    )
    db.commit()

    from app.core.errors import ApiError

    try:
        create_run(
            db,
            name=f"lock-{suffix}",
            dataset_version_id=version.id,
            created_by="tester",
            targets=[(device.id, "all")],
            deployment_uids={f"{device.id}:all": "0123456789abcdef"},
        )
        raise AssertionError("should raise")
    except ApiError as exc:
        assert exc.code == "E_DEPLOYMENT_CHANGED"
        assert exc.status_code == 409


def test_ten_thousand_case_scores_complete_within_one_minute(db: Session) -> None:
    """10,000 条 CER-only 重评分在 60 秒内完成，验收不计 DER 耗时。"""
    from app.domain.metric_profiles import TEMPLATE_PARAMS, profile_hash
    from app.domain.models import Benchmark, CaseScore, MetricProfile, MetricValue
    from app.domain.runs import score_run

    suffix = uuid.uuid4().hex[:8]
    params = copy.deepcopy(TEMPLATE_PARAMS["default_zh"])
    params["punct"]["enabled"] = False
    params["itn"]["enabled"] = False
    params["der"]["enabled"] = False
    params["naming"]["enabled"] = False
    profile = MetricProfile(
        name=f"perf-{suffix}",
        params=params,
        profile_hash=profile_hash(params),
    )
    dataset = Dataset(name=f"perf-{suffix}", source_type="aishell1")
    db.add_all([profile, dataset])
    db.flush()
    version = DatasetVersion(
        dataset_id=dataset.id,
        version="v1",
        status="ready",
        item_count=10_000,
        capabilities={},
    )
    db.add(version)
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    asset = AudioAsset(sha256=suffix.ljust(64, "0"), uri="/data/perf.wav")
    device = Device(
        name=f"perf-{suffix}",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=9,
        status="online",
    )
    db.add_all([subset, asset, device])
    db.flush()
    benchmark = Benchmark(
        name="perf-benchmark",
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        subsets=["all"],
        sampling={},
    )
    db.add(benchmark)
    db.flush()
    run = Run(
        benchmark_id=benchmark.id,
        name=f"perf-run-{suffix}",
        status="completed",
        auto_score=False,
    )
    db.add(run)
    db.flush()
    target = RunTarget(run_id=run.id, device_id=device.id, subset="all", status="completed")
    db.add(target)
    db.flush()

    reference = "今天天气很好"
    items = [
        Item(
            dataset_version_id=version.id,
            subset_id=subset.id,
            item_key=f"perf-{index}",
            content_uid=f"{index:024d}",
            audio_asset_id=asset.id,
            split="test",
            duration_ms=4000,
            buckets={"language": "zh"},
        )
        for index in range(10_000)
    ]
    db.add_all(items)
    db.flush()
    db.add_all(
        RefText(item_id=item.id, kind="verbatim", text=reference, sha256=suffix) for item in items
    )
    db.add_all(
        CaseResult(
            run_target_id=target.id,
            item_id=item.id,
            status="ok",
            text=reference,
            wall_ms=10,
        )
        for item in items
    )
    db.commit()

    started = time.perf_counter()
    evaluation = score_run(db, run.id, profile.id)
    elapsed = time.perf_counter() - started

    assert elapsed < 60
    assert evaluation.status == "completed"
    assert evaluation.progress["phase"] == "completed"
    assert evaluation.progress["done"] == 10_000
    assert evaluation.coverage == {"scored": 10_000, "planned": 10_000}
    assert (
        db.scalar(
            select(func.count())
            .select_from(CaseScore)
            .where(CaseScore.evaluation_id == evaluation.id)
        )
        == 10_000
    )
    der_value = db.scalar(
        select(MetricValue).where(
            MetricValue.evaluation_id == evaluation.id,
            MetricValue.metric == "der",
            MetricValue.run_target_id.is_(None),
            MetricValue.bucket == {},
        )
    )
    assert der_value is not None and der_value.reason == "metric_disabled"
