"""Run 列表筛选、游标分页与行摘要验收（M8 §3.3/§9.3）。"""

import hashlib
import os
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password
from app.domain.models import (
    AppUser,
    AudioAsset,
    Benchmark,
    Dataset,
    DatasetSubset,
    DatasetVersion,
    Deployment,
    Device,
    Evaluation,
    Item,
    Job,
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
    """通过集成测试 PostgreSQL 创建 Run 列表数据。"""
    engine = create_engine(TEST_URL, pool_pre_ping=True)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_runs(db: Session) -> tuple[list[Run], Benchmark, Benchmark, Device, Device]:
    """创建两个基准、两台设备及三个时间有序的 Run。"""
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(name=f"运行列表-{suffix}", source_type="aishell1")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready")
    profile = MetricProfile(
        name=f"运行列表口径-{suffix}", params={}, profile_hash=suffix.ljust(16, "0")
    )
    db.add_all([version, profile])
    db.flush()
    first_benchmark = Benchmark(
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        name=f"回归基准 A {suffix}",
    )
    second_benchmark = Benchmark(
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        name=f"回归基准 B {suffix}",
    )
    first_device = Device(
        name=f"测试设备 A {suffix}", platform="205", kind="etsstt", host="127.0.0.1", port=9000
    )
    second_device = Device(
        name=f"测试设备 B {suffix}", platform="205", kind="etsstt", host="127.0.0.1", port=9001
    )
    db.add_all([first_benchmark, second_benchmark, first_device, second_device])
    db.flush()

    created_at = datetime.now(UTC) - timedelta(minutes=3)
    first_run_id = uuid.uuid4()
    second_run_id = uuid.UUID(int=first_run_id.int + 1)
    runs = [
        Run(
            id=first_run_id,
            benchmark_id=first_benchmark.id,
            name=f"冒烟评测 {suffix}",
            status="completed",
            created_at=created_at,
            started_at=created_at,
            finished_at=created_at + timedelta(seconds=12),
            progress={"done": 10, "total": 10},
        ),
        Run(
            id=second_run_id,
            benchmark_id=second_benchmark.id,
            name=f"夜间回归 {suffix}",
            status="running",
            created_at=created_at,
            started_at=created_at,
            eta_ms=90_000,
            eta_confidence="medium",
            progress={"done": 4, "total": 10},
        ),
        Run(
            benchmark_id=first_benchmark.id,
            name=f"第二轮冒烟 {suffix}",
            status="failed",
            created_at=created_at + timedelta(minutes=2),
            progress={"done": 2, "total": 10},
        ),
    ]
    db.add_all(runs)
    db.flush()
    db.add_all(
        [
            RunTarget(run_id=runs[0].id, device_id=first_device.id, subset="near"),
            RunTarget(run_id=runs[1].id, device_id=second_device.id, subset="far"),
            RunTarget(run_id=runs[2].id, device_id=first_device.id, subset="near"),
            Evaluation(
                run_id=runs[0].id,
                metric_profile_id=profile.id,
                profile_hash=profile.profile_hash,
                scorer_version="cer-1",
                status="completed",
                metrics={"cer": 0.037, "n": 10},
            ),
        ]
    )
    db.commit()
    for run in runs:
        db.refresh(run)
    return runs, first_benchmark, second_benchmark, first_device, second_device


def _auth(client: TestClient) -> dict[str, str]:
    """创建隔离测试用户并返回鉴权头，避免依赖其他用例改写的管理员凭据。"""
    username = f"run-list-{uuid.uuid4().hex[:12]}"
    password = "run-list-test-password"
    factory = client.app.state.evaluation_session_factory
    with factory() as session:
        session.add(
            AppUser(
                username=username,
                display_name="Run 列表测试用户",
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


def test_run_list_cursor_filters_facets_and_summary(client: TestClient, db: Session) -> None:
    """列表按稳定游标分页，筛选和行摘要与关联数据一致。"""
    runs, first_benchmark, second_benchmark, first_device, second_device = _make_runs(db)
    headers = _auth(client)
    query_name = runs[0].name.split(" ")[1]

    first_page = client.get(
        "/api/v1/runs",
        params={"limit": 2, "q": query_name},
        headers=headers,
    )
    assert first_page.status_code == 200
    payload = first_page.json()
    assert [item["id"] for item in payload["items"]] == [str(runs[2].id), str(runs[1].id)]
    assert payload["has_more"] is True
    assert payload["next_cursor"]
    assert {item["name"] for item in payload["facets"]["benchmarks"]} >= {
        first_benchmark.name,
        second_benchmark.name,
    }
    assert {item["name"] for item in payload["facets"]["devices"]} >= {
        first_device.name,
        second_device.name,
    }
    running = payload["items"][1]
    assert running["benchmark"] == {"id": str(second_benchmark.id), "name": second_benchmark.name}
    assert running["targets"] == [
        {"device_id": str(second_device.id), "device_name": second_device.name, "subset": "far"}
    ]
    assert running["eta_ms"] == 90_000
    assert running["eta_confidence"] == "medium"

    second_page = client.get(
        "/api/v1/runs",
        params={"limit": 2, "q": query_name, "cursor": payload["next_cursor"]},
        headers=headers,
    )
    assert [item["id"] for item in second_page.json()["items"]] == [str(runs[0].id)]
    assert second_page.json()["has_more"] is False
    assert second_page.json()["items"][0]["cer"] == pytest.approx(0.037)
    assert second_page.json()["items"][0]["evaluation"]["id"]
    assert second_page.json()["items"][0]["duration_ms"] == 12_000

    filtered = client.get(
        "/api/v1/runs",
        params={
            "status": "completed",
            "benchmark": str(first_benchmark.id),
            "device": str(first_device.id),
            "q": query_name,
        },
        headers=headers,
    )
    assert [item["id"] for item in filtered.json()["items"]] == [str(runs[0].id)]


def test_run_list_rejects_invalid_cursor_and_unknown_status(client: TestClient) -> None:
    """无效游标和状态返回统一验证错误。"""
    headers = _auth(client)
    invalid_cursor = client.get("/api/v1/runs?cursor=bad", headers=headers)
    assert invalid_cursor.status_code == 422
    assert invalid_cursor.json()["code"] == "E_VALIDATION"

    unknown_status = client.get("/api/v1/runs?status=unknown", headers=headers)
    assert unknown_status.status_code == 422
    assert unknown_status.json()["code"] == "E_VALIDATION"


def test_run_create_uses_selected_benchmark(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run 创建时保留用户选择的 Benchmark，而非静默换成默认口径。"""
    _runs, benchmark, _other, device, _other_device = _make_runs(db)
    version = db.get(DatasetVersion, benchmark.dataset_version_id)
    assert version is not None
    subset = DatasetSubset(
        dataset_version_id=version.id,
        name="all",
        is_primary=True,
        item_count=1,
        capabilities={"text_ratio": 1.0},
    )
    db.add(subset)
    db.flush()
    asset = AudioAsset(sha256=uuid.uuid4().hex.ljust(64, "0"), uri="/data/run-create.wav")
    db.add(asset)
    db.flush()
    item = Item(
        dataset_version_id=version.id,
        subset_id=subset.id,
        item_key=f"run-create-{uuid.uuid4().hex[:8]}",
        content_uid=uuid.uuid4().hex[:24],
        audio_asset_id=asset.id,
        split="test",
        duration_ms=4000,
    )
    db.add(item)
    db.flush()
    ref_text_hash = hashlib.sha256("运行创建测试".encode()).hexdigest()[:16]
    db.add(RefText(item_id=item.id, kind="verbatim", text="运行创建测试", sha256=ref_text_hash))
    version.item_count = 1
    db.add(
        Deployment(
            device_id=device.id,
            uid=uuid.uuid4().hex[:16],
            model_key="run-list-test",
            platform="205",
            accel={},
            capabilities={},
        )
    )
    db.commit()
    executed: set[uuid.UUID] = set()
    executed_lock = threading.Lock()

    async def fake_execute_run(session: Session, run_id: uuid.UUID) -> Run:
        run = session.get(Run, run_id)
        assert run is not None
        run.status = "completed"
        session.commit()
        with executed_lock:
            executed.add(run_id)
        return run

    monkeypatch.setattr("app.domain.run_worker.execute_run", fake_execute_run)
    response = client.post(
        "/api/v1/runs",
        headers={**_auth(client), "Idempotency-Key": str(uuid.uuid4())},
        json={
            "name": "按基准创建 Run",
            "dataset_version_id": str(benchmark.dataset_version_id),
            "benchmark_id": str(benchmark.id),
            "device_id": str(device.id),
        },
    )

    assert response.status_code == 201
    created_id = uuid.UUID(response.json()["run"]["id"])
    # worker 可能先处理同一会话里更早遗留的 queued Run；必须等到本次 Run 完成，
    # 否则会把“别的 Run 执行完”误判成本 Run 完成（全量顺序下的偶发失败）。
    deadline = time.monotonic() + 5
    with executed_lock:
        done = created_id in executed
    while not done and time.monotonic() < deadline:
        time.sleep(0.02)
        with executed_lock:
            done = created_id in executed
    created = db.get(Run, created_id)
    assert created is not None
    assert created.benchmark_id == benchmark.id
    db.refresh(created)
    assert created.status == "completed"
    job_deadline = time.monotonic() + 2
    job = None
    while time.monotonic() < job_deadline:
        job = db.scalar(
            select(Job).where(
                Job.kind == "run", Job.ref_type == "run", Job.ref_id == str(created_id)
            )
        )
        if job is not None and job.status == "succeeded":
            break
        time.sleep(0.01)
        db.expire_all()
    assert job is not None and job.status == "succeeded"
