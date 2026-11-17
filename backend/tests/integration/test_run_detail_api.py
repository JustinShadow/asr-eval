"""Run 详情分桶读数和持久化执行时间线验收（M8 §9.4）。"""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password
from app.domain.models import (
    AppUser,
    AudioAsset,
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
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _auth(client: TestClient) -> dict[str, str]:
    username = f"run-detail-{uuid.uuid4().hex[:12]}"
    password = "run-detail-test-password"
    factory = client.app.state.evaluation_session_factory
    with factory() as session:
        session.add(
            AppUser(
                username=username,
                display_name="Run 详情测试用户",
                role="tester",
                role_source="local",
                password_hash=hash_password(password),
                active=True,
            )
        )
        session.commit()
    client.cookies.clear()
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _make_run(db: Session) -> Run:
    suffix = uuid.uuid4().hex[:12]
    dataset = Dataset(name=f"Run 详情数据集 {suffix}", source_type="test")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(
        dataset_id=dataset.id,
        version="v1",
        status="ready",
        item_count=3,
    )
    profile = MetricProfile(
        name=f"Run 详情口径 {suffix}",
        params={},
        profile_hash=suffix.ljust(16, "0"),
    )
    db.add_all([version, profile])
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="near")
    db.add(subset)
    db.flush()
    benchmark = Benchmark(
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        name=f"Run 详情基准 {suffix}",
        subsets=["near"],
    )
    device = Device(
        name=f"Run 详情设备 {suffix}",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=9000,
    )
    asset = AudioAsset(
        sha256=uuid.uuid4().hex * 2,
        uri=f"file:///tmp/{suffix}.wav",
        duration_ms=2_000,
        sample_rate=16_000,
        channels=1,
    )
    db.add_all([benchmark, device, asset])
    db.flush()

    created_at = datetime.now(UTC) - timedelta(minutes=3)
    run = Run(
        benchmark_id=benchmark.id,
        name=f"Run 详情验收 {suffix}",
        status="completed",
        snapshot={},
        created_at=created_at,
        started_at=created_at + timedelta(seconds=5),
        finished_at=created_at + timedelta(seconds=30),
        progress={"done": 2, "total": 3},
    )
    db.add(run)
    db.flush()
    target = RunTarget(
        run_id=run.id,
        device_id=device.id,
        subset="near",
        status="completed",
        started_at=created_at + timedelta(seconds=7),
        finished_at=created_at + timedelta(seconds=25),
        stats={"done": 3, "total": 3, "failed": 1},
    )
    db.add(target)
    db.flush()

    items = [
        Item(
            dataset_version_id=version.id,
            subset_id=subset.id,
            item_key=f"sample-{index}",
            content_uid=f"{index:024x}",
            audio_asset_id=asset.id,
            split="test",
            duration_ms=2_000,
            lang=language,
            buckets={"dialect": language},
        )
        for index, language in enumerate(["zh", "yue", "zh"], start=1)
    ]
    db.add_all(items)
    db.flush()
    db.add_all(
        [
            RefText(
                item_id=item.id,
                kind="verbatim",
                lang=item.lang,
                text=f"参考文本 {index}",
                sha256=f"{suffix[:8]}{index:08x}",
            )
            for index, item in enumerate(items, start=1)
        ]
    )
    cases = [
        CaseResult(
            run_target_id=target.id,
            item_id=item.id,
            status="ok" if index < 3 else "failed",
            text="示例识别",
            error="设备响应超时" if index == 3 else None,
            created_at=created_at + timedelta(seconds=10 + index),
        )
        for index, item in enumerate(items, start=1)
    ]
    db.add_all(cases)
    db.flush()
    previous_evaluation = Evaluation(
        run_id=run.id,
        metric_profile_id=profile.id,
        profile_hash=profile.profile_hash,
        scorer_version="cer-1",
        status="completed",
        metrics={"cer": 0.05, "n": 2},
        created_at=created_at + timedelta(seconds=31),
    )
    evaluation = Evaluation(
        run_id=run.id,
        metric_profile_id=profile.id,
        profile_hash=profile.profile_hash,
        scorer_version="cer-2",
        status="completed",
        metrics={"cer": 0.15, "n": 2},
        created_at=created_at + timedelta(seconds=32),
    )
    db.add_all([previous_evaluation, evaluation])
    db.flush()
    for score_evaluation, edits_values in (
        (previous_evaluation, [1, 0]),
        (evaluation, [2, 1]),
    ):
        db.add_all(
            [
                CaseScore(
                    evaluation_id=score_evaluation.id,
                    case_result_id=case.id,
                    metrics={
                        "cer": {
                            "value": edits / 10,
                            "s": edits,
                            "d": 0,
                            "i": 0,
                            "n": 10,
                        }
                    },
                )
                for case, edits in zip(cases[:2], edits_values, strict=True)
            ]
        )
    db.commit()
    db.refresh(run)
    return run


def test_run_readout_dimensions_and_timeline(client: TestClient, db: Session) -> None:
    """校验维度分桶可读数，时间线按落库时间包含目标和失败样本。"""
    run = _make_run(db)
    headers = _auth(client)

    readout = client.get(
        f"/api/v1/runs/{run.id}/readout",
        params={"dim": "lang"},
        headers=headers,
    )
    assert readout.status_code == 200
    payload = readout.json()
    assert payload["dim"] == "lang"
    assert {row["bucket"]["lang"] for row in payload["buckets"]} == {"zh", "yue"}
    assert all(row["n"] == 1 for row in payload["buckets"])

    evaluations = client.get(f"/api/v1/runs/{run.id}/evaluations", headers=headers).json()["items"]
    selected_id = evaluations[-1]["id"]
    selected_detail = client.get(
        f"/api/v1/runs/{run.id}",
        params={"evaluation_id": selected_id},
        headers=headers,
    )
    assert selected_detail.status_code == 200
    assert selected_detail.json()["evaluation"]["id"] == selected_id
    assert selected_detail.json()["evaluation"]["scorer_version"] == "cer-1"

    selected_readout = client.get(
        f"/api/v1/runs/{run.id}/readout",
        params={"dim": "lang", "evaluation_id": selected_id},
        headers=headers,
    )
    assert selected_readout.status_code == 200
    assert selected_readout.json()["cer"] == pytest.approx(0.05)
    selected_matrix = client.get(
        f"/api/v1/runs/{run.id}/matrix",
        params={"evaluation_id": selected_id},
        headers=headers,
    )
    assert selected_matrix.status_code == 200
    assert selected_matrix.json()["rows"][0]["cer"] == pytest.approx(0.05)
    selected_cases = client.get(
        f"/api/v1/runs/{run.id}/cases",
        params={"evaluation_id": selected_id, "limit": 10},
        headers=headers,
    )
    assert selected_cases.status_code == 200
    assert [item["cer"] for item in selected_cases.json()["items"][:2]] == [0.1, 0.0]
    foreign_evaluation = client.get(
        f"/api/v1/runs/{run.id}",
        params={"evaluation_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert foreign_evaluation.status_code == 404

    invalid_dimension = client.get(
        f"/api/v1/runs/{run.id}/readout",
        params={"dim": "snr"},
        headers=headers,
    )
    assert invalid_dimension.status_code == 422
    assert invalid_dimension.json()["code"] == "E_VALIDATION"

    timeline = client.get(f"/api/v1/runs/{run.id}/timeline", headers=headers)
    assert timeline.status_code == 200
    timeline_payload = timeline.json()
    event_types = [event["type"] for event in timeline_payload["events"]]
    assert event_types == [
        "run.created",
        "run.started",
        "target.started",
        "case.failed",
        "target.finished",
        "run.finished",
    ]
    failure = next(event for event in timeline_payload["events"] if event["type"] == "case.failed")
    assert failure["detail"]["item_key"] == "sample-3"
    assert failure["detail"]["error"] == "设备响应超时"
    assert timeline_payload["failed_case_count"] == 1
    assert timeline_payload["omitted_failed_cases"] == 0
    assert timeline_payload["untimed_failed_cases"] == 0
