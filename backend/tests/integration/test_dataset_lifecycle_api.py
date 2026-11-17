"""数据集版本删除与重新分析接口测试（M1 §3.3、M7 §3.5/§6）。"""

import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.domain.models import (
    AudioAsset,
    AuditLog,
    Base,
    Benchmark,
    Dataset,
    DatasetSubset,
    DatasetVersion,
    Item,
    Job,
    MetricProfile,
    RefSegment,
    RefText,
)
from app.domain.reanalysis import wait_for_reanalysis_tasks

TEST_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://asr:asr@127.0.0.1:5432/asr_eval_test",
)
pytestmark = pytest.mark.integration


@pytest.fixture()
def db() -> Iterator[Session]:
    """使用集成测试 PostgreSQL 准备版本生命周期数据。"""
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


def _version(db: Session, *, source_type: str = "aishell3") -> DatasetVersion:
    suffix = uuid.uuid4().hex[:10]
    dataset = Dataset(name=f"版本生命周期-{suffix}", source_type=source_type)
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready")
    db.add(version)
    db.commit()
    return version


def test_delete_unreferenced_version_records_audit(client: TestClient, db: Session) -> None:
    version = _version(db)
    version_id = version.id
    token = _token(client)

    response = client.delete(
        f"/api/v1/dataset-versions/{version.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    db.expunge(version)
    assert db.get(DatasetVersion, version_id) is None
    audit_row = db.scalar(
        select(AuditLog).where(
            AuditLog.action == "dataset.delete",
            AuditLog.resource == f"dataset_version:{version_id}",
        )
    )
    assert audit_row is not None and audit_row.actor == "admin"


def test_delete_rejects_version_referenced_by_benchmark(client: TestClient, db: Session) -> None:
    version = _version(db)
    profile = MetricProfile(
        name=f"profile-{uuid.uuid4().hex[:8]}", params={}, profile_hash="a" * 16
    )
    db.add(profile)
    db.flush()
    db.add(
        Benchmark(
            dataset_version_id=version.id,
            metric_profile_id=profile.id,
            name=f"benchmark-{uuid.uuid4().hex[:8]}",
        )
    )
    db.commit()
    token = _token(client)

    response = client.delete(
        f"/api/v1/dataset-versions/{version.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "E_VERSION_IN_USE"
    assert db.get(DatasetVersion, version.id) is not None


def test_reanalyze_returns_job_and_rebuilds_capabilities(client: TestClient, db: Session) -> None:
    version = _version(db)
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    asset = AudioAsset(sha256=uuid.uuid4().hex.ljust(64, "0"), uri="/missing/reanalyze.wav")
    db.add_all([subset, asset])
    db.flush()
    item = Item(
        dataset_version_id=version.id,
        subset_id=subset.id,
        item_key="meeting-1",
        content_uid=uuid.uuid4().hex[:24],
        audio_asset_id=asset.id,
        split="test",
        duration_ms=1000,
        lang="zh",
        speakers=["S1", "S2"],
        buckets={"gender": "M"},
    )
    db.add(item)
    db.flush()
    db.add_all(
        [
            RefText(item_id=item.id, kind="verbatim", lang="zh", text="你好。", sha256="a" * 16),
            RefText(item_id=item.id, kind="itn", lang="zh", text="1", sha256="b" * 16),
            RefSegment(
                item_id=item.id,
                seq=0,
                speaker="S1",
                start_ms=0,
                end_ms=700,
                text="你好。",
            ),
            RefSegment(
                item_id=item.id,
                seq=1,
                speaker="S2",
                start_ms=300,
                end_ms=600,
                text="嗯",
                overlap=True,
            ),
        ]
    )
    db.commit()
    token = _token(client)

    response = client.post(
        f"/api/v1/dataset-versions/{version.id}/reanalyze",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 202
    assert response.headers["location"] == f"/api/v1/jobs/{response.json()['id']}"
    wait_for_reanalysis_tasks()

    db.expire_all()
    job = db.get(Job, uuid.UUID(response.json()["id"]))
    analyzed = db.get(DatasetVersion, version.id)
    assert job is not None and job.status == "succeeded"
    assert analyzed is not None
    assert analyzed.capabilities["segment_ratio"] == 1.0
    assert analyzed.capabilities["speaker_ratio"] == 1.0
    assert analyzed.capabilities["itn_ratio"] == 1.0
    assert analyzed.capabilities["overlap_ratio"] == 0.3
    assert analyzed.capabilities["punct_supported"] is True
