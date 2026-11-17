"""Run / Dataset 整组删除的权限、引用保护、审计与级联（M7 D-M7-21、M1 §3.2）。"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.security import hash_password
from app.domain.metric_profiles import TEMPLATE_PARAMS
from app.domain.models import (
    AppUser,
    AuditLog,
    Base,
    Benchmark,
    Dataset,
    DatasetVersion,
    Device,
    Evaluation,
    Job,
    MetricProfile,
    Report,
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
    """使用集成测试 PostgreSQL，建表后返回独立 Session。"""
    engine = create_engine(TEST_URL, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _token(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _dataset(db: Session, *, status: str = "ready") -> tuple[Dataset, DatasetVersion]:
    suffix = uuid.uuid4().hex[:10]
    dataset = Dataset(name=f"删除数据集-{suffix}", source_type="aishell1")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status=status)
    db.add(version)
    db.commit()
    return dataset, version


def _benchmark(db: Session, version: DatasetVersion) -> Benchmark:
    suffix = uuid.uuid4().hex[:8]
    profile = MetricProfile(
        name=f"删除口径-{suffix}", params={}, profile_hash=suffix.ljust(16, "0")
    )
    db.add(profile)
    db.flush()
    benchmark = Benchmark(
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        name=f"删除基准-{suffix}",
    )
    db.add(benchmark)
    db.commit()
    return benchmark


def _run(
    db: Session, benchmark: Benchmark, *, status: str = "completed"
) -> tuple[Run, Device, Evaluation]:
    suffix = uuid.uuid4().hex[:8]
    device = Device(
        name=f"删除设备-{suffix}",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=9000,
    )
    db.add(device)
    db.flush()
    run = Run(benchmark_id=benchmark.id, name=f"待删 Run-{suffix}", status=status)
    db.add(run)
    db.flush()
    db.add(RunTarget(run_id=run.id, device_id=device.id, subset="all", status=status))
    evaluation = Evaluation(
        run_id=run.id,
        metric_profile_id=benchmark.metric_profile_id,
        profile_hash="0123456789abcdef",
        scorer_version="cer-4",
        status="completed",
    )
    db.add(evaluation)
    db.commit()
    return run, device, evaluation


def test_delete_completed_run_cascades_and_records_audit(client: TestClient, db: Session) -> None:
    """终态 Run 可删除，评分/目标级联清理并写审计。"""
    _, version = _dataset(db)
    run, _, evaluation = _run(db, _benchmark(db, version))
    run_id, evaluation_id = run.id, evaluation.id
    token = _token(client)

    response = client.delete(
        f"/api/v1/runs/{run_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    db.expire_all()
    assert db.get(Run, run_id) is None
    assert db.get(Evaluation, evaluation_id) is None
    assert db.scalar(select(AuditLog).where(AuditLog.action == "run.delete")) is not None


def test_delete_active_run_is_rejected(client: TestClient, db: Session) -> None:
    """运行中的 Run 必须先取消并等待结束（M7 D-M7-21）。"""
    _, version = _dataset(db)
    run, _, _ = _run(db, _benchmark(db, version), status="running")
    token = _token(client)

    response = client.delete(
        f"/api/v1/runs/{run.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "E_RUN_ACTIVE"
    assert db.get(Run, run.id) is not None


def test_delete_run_with_active_evaluation_is_rejected(client: TestClient, db: Session) -> None:
    """Run 已终态但评分仍在排队/执行时，先等待或取消评分，避免 worker 丢失目标。"""
    _, version = _dataset(db)
    run, _, evaluation = _run(db, _benchmark(db, version))
    evaluation.status = "scoring"
    db.commit()
    token = _token(client)

    response = client.delete(
        f"/api/v1/runs/{run.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "E_EVALUATION_ACTIVE"
    assert response.json()["detail"]["evaluation_ids"] == [str(evaluation.id)]
    assert db.get(Run, run.id) is not None


def test_delete_run_referenced_by_report_is_rejected(client: TestClient, db: Session) -> None:
    """不可变报告仍引用评分时禁止删除 Run，避免报告失去来源。"""
    _, version = _dataset(db)
    run, _, evaluation = _run(db, _benchmark(db, version))
    db.add(
        Report(
            kind="single",
            evaluation_ids=[str(evaluation.id)],
            input_hash="a" * 64,
            template_version="report-2",
        )
    )
    db.commit()
    token = _token(client)

    response = client.delete(
        f"/api/v1/runs/{run.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "E_RUN_REFERENCED"
    assert db.get(Run, run.id) is not None


def test_delete_run_with_queued_multi_source_report_job_is_rejected(
    client: TestClient, db: Session
) -> None:
    """多评分报告的 Job 只在 progress.evaluation_ids 中记录来源，也必须阻止删除。"""
    _, version = _dataset(db)
    run, _, evaluation = _run(db, _benchmark(db, version))
    db.add(
        Job(
            kind="report",
            ref_type="evaluation",
            ref_id=str(evaluation.id),
            status="queued",
            progress={"phase": "queued", "evaluation_ids": [str(evaluation.id)]},
        )
    )
    db.commit()
    token = _token(client)

    response = client.delete(
        f"/api/v1/runs/{run.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "E_RUN_REFERENCED"


def test_delete_run_requires_admin(client: TestClient, db: Session) -> None:
    """run:delete 仅 admin 拥有；工程师角色返回 403。"""
    _, version = _dataset(db)
    run, _, _ = _run(db, _benchmark(db, version))
    db.merge(
        AppUser(
            username="engineer-delete",
            display_name="工程师",
            role="engineer",
            role_source="local",
            password_hash=hash_password("engineer123"),
            active=True,
        )
    )
    db.commit()
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "engineer-delete", "password": "engineer123"},
    )
    assert login.status_code == 200

    response = client.delete(
        f"/api/v1/runs/{run.id}",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )

    assert response.status_code == 403


def test_delete_dataset_cascades_versions_and_manifest_directory(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """无引用的数据集整组删除，版本级联且受管 manifest 目录清理。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    dataset, version = _dataset(db)
    manifest_dir = tmp_path / "manifests" / str(version.id)
    manifest_dir.mkdir(parents=True)
    manifest_file = manifest_dir / "manifest.jsonl.gz"
    manifest_file.write_bytes(b"manifest")
    version.manifest_key = str(manifest_file)
    db.commit()
    dataset_id, version_id = dataset.id, version.id
    token = _token(client)

    response = client.delete(
        f"/api/v1/datasets/{dataset_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    db.expire_all()
    assert db.get(Dataset, dataset_id) is None
    assert db.get(DatasetVersion, version_id) is None
    assert not manifest_dir.exists()
    assert db.scalar(select(AuditLog).where(AuditLog.action == "dataset.deleted")) is not None


def test_delete_dataset_rejects_benchmark_reference(client: TestClient, db: Session) -> None:
    """被基准引用的数据集不能整组删除。"""
    dataset, version = _dataset(db)
    _benchmark(db, version)
    token = _token(client)

    response = client.delete(
        f"/api/v1/datasets/{dataset.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "E_DATASET_IN_USE"
    assert db.get(Dataset, dataset.id) is not None


def test_delete_unreferenced_benchmark_records_audit(client: TestClient, db: Session) -> None:
    """未被 Run 引用的基准可彻底删除，并记录审计。"""
    _, version = _dataset(db)
    benchmark = _benchmark(db, version)
    benchmark_id = benchmark.id
    token = _token(client)

    response = client.delete(
        f"/api/v1/benchmarks/{benchmark_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    db.expire_all()
    assert db.get(Benchmark, benchmark_id) is None
    assert db.scalar(select(AuditLog).where(AuditLog.action == "benchmark.delete")) is not None


def test_delete_referenced_benchmark_is_rejected(client: TestClient, db: Session) -> None:
    """已有 Run 引用时只能归档，不能删除基准。"""
    _, version = _dataset(db)
    benchmark = _benchmark(db, version)
    _run(db, benchmark)
    token = _token(client)

    response = client.delete(
        f"/api/v1/benchmarks/{benchmark.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "E_BENCHMARK_IN_USE"
    assert db.get(Benchmark, benchmark.id) is not None


def test_official_evaluator_is_created_locked_and_cannot_be_edited(
    client: TestClient,
) -> None:
    """官方口径由代码固定：创建时覆盖用户参数，后续 PATCH 被拒绝。"""
    token = _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    payload_params = {
        **TEMPLATE_PARAMS["alimeeting_official"],
        "der": {"enabled": True, "collar_ms": 999, "skip_overlap": True},
    }
    created = client.post(
        "/api/v1/metric-profiles",
        headers=headers,
        json={"name": f"官方锁定-{uuid.uuid4().hex[:8]}", "params": payload_params},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["evaluator"] == "alimeeting_official"
    assert body["evaluator_locked"] is True
    assert body["params"]["der"]["collar_ms"] == 250
    assert body["params"]["der"]["skip_overlap"] is False

    patched = client.patch(
        f"/api/v1/metric-profiles/{body['id']}",
        headers={
            **headers,
            "If-Match": created.headers["ETag"],
        },
        json={"name": "改名"},
    )
    assert patched.status_code == 409
    assert patched.json()["code"] == "E_EVALUATOR_LOCKED"


def test_delete_unreferenced_metric_profile_records_audit(client: TestClient, db: Session) -> None:
    """未被 Benchmark 引用的口径版本可删除并写审计。"""
    profile = MetricProfile(
        name=f"待删口径-{uuid.uuid4().hex[:8]}", params={}, profile_hash="c" * 16
    )
    db.add(profile)
    db.commit()
    profile_id = profile.id
    token = _token(client)

    response = client.delete(
        f"/api/v1/metric-profiles/{profile_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    db.expire_all()
    assert db.get(MetricProfile, profile_id) is None
    assert db.scalar(select(AuditLog).where(AuditLog.action == "metric_profile.delete")) is not None


def test_delete_referenced_metric_profile_is_rejected(client: TestClient, db: Session) -> None:
    """被 Benchmark 引用的口径不能删除。"""
    _, version = _dataset(db)
    benchmark = _benchmark(db, version)
    token = _token(client)

    response = client.delete(
        f"/api/v1/metric-profiles/{benchmark.metric_profile_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "E_METRIC_PROFILE_IN_USE"
    assert db.get(MetricProfile, benchmark.metric_profile_id) is not None


def test_delete_terminal_evaluation_records_audit(client: TestClient, db: Session) -> None:
    """终态评分可删除，审计记录来源 Run。"""
    _, version = _dataset(db)
    _, _, evaluation = _run(db, _benchmark(db, version))
    evaluation_id = evaluation.id
    token = _token(client)

    response = client.delete(
        f"/api/v1/evaluations/{evaluation_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    db.expire_all()
    assert db.get(Evaluation, evaluation_id) is None
    assert db.scalar(select(AuditLog).where(AuditLog.action == "evaluation.delete")) is not None


def test_delete_active_evaluation_is_rejected(client: TestClient, db: Session) -> None:
    """评分仍在执行时不能删除。"""
    _, version = _dataset(db)
    _, _, evaluation = _run(db, _benchmark(db, version))
    evaluation.status = "scoring"
    db.commit()
    token = _token(client)

    response = client.delete(
        f"/api/v1/evaluations/{evaluation.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "E_EVALUATION_ACTIVE"


def test_delete_report_referenced_evaluation_is_rejected(client: TestClient, db: Session) -> None:
    """报告快照引用评分时不能删除该评分。"""
    _, version = _dataset(db)
    _, _, evaluation = _run(db, _benchmark(db, version))
    db.add(
        Report(
            kind="single",
            evaluation_ids=[str(evaluation.id)],
            input_hash="b" * 64,
            template_version="report-2",
        )
    )
    db.commit()
    token = _token(client)

    response = client.delete(
        f"/api/v1/evaluations/{evaluation.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "E_EVALUATION_REFERENCED"


def test_delete_dataset_rejects_active_import_job(client: TestClient, db: Session) -> None:
    """存在排队/运行中的导入任务时不能删除数据集。"""
    dataset, _ = _dataset(db, status="importing")
    db.add(
        Job(
            kind="import",
            ref_type="dataset",
            ref_id=str(dataset.id),
            status="running",
        )
    )
    db.commit()
    token = _token(client)

    response = client.delete(
        f"/api/v1/datasets/{dataset.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "E_DATASET_IN_USE"
