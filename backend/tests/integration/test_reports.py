"""报告快照在来源删除后仍可读（M6 §5.8）。"""

import json
import os
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

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
    Job,
    MetricProfile,
    MetricValue,
    RefText,
    Report,
    Run,
    RunTarget,
)
from app.reports import service as report_service
from app.reports.tasks import start_report, wait_for_report_tasks

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


def test_snapshot_files_survive_source_evaluation_deletion(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(name=f"report-{suffix}", source_type="aishell1")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(
        dataset_id=dataset.id,
        version="v1",
        status="ready",
        item_count=100,
    )
    db.add(version)
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    profile = MetricProfile(name=f"report-{suffix}", params={}, profile_hash="report-profile")
    db.add_all([subset, profile])
    db.flush()
    benchmark = Benchmark(
        name=f"report-{suffix}",
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        subsets=["all"],
        version_hash="report-hash-v1",
    )
    db.add(benchmark)
    db.flush()
    run = Run(benchmark_id=benchmark.id, name=f"report-run-{suffix}", status="completed")
    db.add(run)
    db.flush()
    evaluation = Evaluation(
        run_id=run.id,
        metric_profile_id=profile.id,
        profile_hash=profile.profile_hash,
        scorer_version="cer-3",
        status="completed",
        coverage={"scored": 100, "planned": 100},
        metrics={"cer": 0.125, "n": 100},
    )
    db.add(evaluation)
    db.flush()
    asset = AudioAsset(
        sha256=f"{suffix}audio".ljust(64, "0"),
        uri=f"/audio/{suffix}.wav",
    )
    db.add(asset)
    db.flush()
    item = Item(
        dataset_version_id=version.id,
        subset_id=subset.id,
        item_key=f"sample-{suffix}",
        content_uid=f"{suffix:0<24}",
        audio_asset_id=asset.id,
        split="test",
        duration_ms=1200,
    )
    device = Device(
        name=f"report-device-{suffix}",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=9,
        status="online",
    )
    db.add_all([item, device])
    db.flush()
    target = RunTarget(run_id=run.id, device_id=device.id, subset="all", status="completed")
    db.add(target)
    db.flush()
    case = CaseResult(
        run_target_id=target.id,
        item_id=item.id,
        status="ok",
        text="今天天汽晴朗",
    )
    db.add(case)
    db.flush()
    db.add_all(
        [
            RefText(
                item_id=item.id,
                kind="verbatim",
                text="今天天气晴朗",
                sha256="a" * 16,
            ),
            CaseScore(
                evaluation_id=evaluation.id,
                case_result_id=case.id,
                metrics={"cer": {"status": "ok", "value": 0.2, "s": 1, "d": 0, "i": 0, "n": 5}},
            ),
        ]
    )
    db.add(
        MetricValue(
            evaluation_id=evaluation.id,
            run_target_id=None,
            metric="cer",
            subset="all",
            scope="micro",
            bucket={},
            value=0.125,
            n=100,
            ci_low=0.11,
            ci_high=0.14,
            status="ok",
            detail={},
        )
    )
    db.add(
        MetricValue(
            evaluation_id=evaluation.id,
            run_target_id=None,
            metric="cer",
            subset="all",
            scope="micro",
            bucket={"duration_bucket": "5_15s"},
            value=0.15,
            n=50,
            ci_low=0.1,
            ci_high=0.2,
            status="ok",
            detail={},
        )
    )
    db.commit()

    monkeypatch.setattr(
        report_service,
        "get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path),
    )
    job = start_report(
        db,
        kind="single",
        evaluation_ids=[evaluation.id],
        query={},
        level="summary",
        created_by="test",
    )
    wait_for_report_tasks()
    db.expire_all()
    finished_job = db.get(Job, job.id)
    assert finished_job is not None
    assert finished_job.status == "succeeded"
    assert finished_job.ref_type == "report"
    assert finished_job.result is not None
    assert finished_job.result["url"].endswith(finished_job.result["resource_id"])
    report = db.get(Report, uuid.UUID(str(finished_job.result["resource_id"])))
    assert report is not None
    report_id = report.id
    html_path = Path(report.html_uri or "")
    payload_path = Path(report.payload_uri or "")
    xlsx_path = Path(report.xlsx_uri or "")
    assert report_service.report_dict(db, report)["size_bytes"] == sum(
        path.stat().st_size for path in (html_path, payload_path, xlsx_path)
    )
    assert html_path.is_file() and payload_path.is_file() and xlsx_path.is_file()
    assert "12.50%" in html_path.read_text(encoding="utf-8")
    assert "10.00% – 20.00%" in html_path.read_text(encoding="utf-8")

    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert login.status_code == 200
    report_html = client.get(
        f"/api/v1/reports/{report.id}/html",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert report_html.status_code == 200
    assert report_html.headers["Content-Disposition"].startswith("attachment;")
    assert report_html.headers["Content-Security-Policy"].startswith("sandbox;")
    assert report_html.headers["X-Content-Type-Options"] == "nosniff"

    repeated = report_service.create_report(
        db,
        kind="single",
        evaluation_ids=[evaluation.id],
        created_by="test",
    )
    assert repeated.input_hash == report.input_hash

    evidence = report_service.create_report(
        db,
        kind="single",
        evaluation_ids=[evaluation.id],
        level="evidence",
        created_by="test",
    )
    evidence_html = Path(evidence.html_uri or "").read_text(encoding="utf-8")
    assert "今天天气晴朗" in evidence_html
    assert "今天天汽晴朗" in evidence_html
    assert f"/audio/{suffix}.wav" in evidence_html
    with zipfile.ZipFile(BytesIO(Path(evidence.xlsx_uri or "").read_bytes())) as archive:
        cases_xml = archive.read("xl/worksheets/sheet3.xml").decode()
        assert "今天天气晴朗" in cases_xml and "今天天汽晴朗" in cases_xml

    matrix_report = report_service.create_report(
        db,
        kind="matrix",
        evaluation_ids=[evaluation.id],
        created_by="test",
    )
    assert "横评矩阵" in Path(matrix_report.html_uri or "").read_text(encoding="utf-8")

    comparison_evaluation = Evaluation(
        run_id=run.id,
        metric_profile_id=profile.id,
        profile_hash=profile.profile_hash,
        scorer_version="cer-3",
        status="completed",
        coverage={"scored": 100, "planned": 100},
        metrics={"cer": 0.1, "n": 100},
    )
    db.add(comparison_evaluation)
    db.flush()
    db.add(
        MetricValue(
            evaluation_id=comparison_evaluation.id,
            run_target_id=None,
            metric="cer",
            subset="all",
            scope="micro",
            bucket={},
            value=0.1,
            n=100,
            ci_low=0.09,
            ci_high=0.11,
            status="ok",
            detail={},
        )
    )
    db.commit()
    compare_report = report_service.create_report(
        db,
        kind="compare",
        evaluation_ids=[evaluation.id, comparison_evaluation.id],
        query={"baseline": str(evaluation.id), "pair_by": "none"},
        created_by="test",
    )
    comparison_html = Path(compare_report.html_uri or "").read_text(encoding="utf-8")
    assert "条件对比" in comparison_html
    assert "10.00%" in comparison_html and "12.50%" in comparison_html
    with zipfile.ZipFile(BytesIO(Path(compare_report.xlsx_uri or "").read_bytes())) as archive:
        workbook = archive.read("xl/workbook.xml").decode()
        assert all(f'name="{name}"' in workbook for name in ("Summary", "Buckets", "Cases"))

    db.delete(evaluation)
    db.commit()
    db.expire_all()

    surviving_report = db.scalar(select(Report).where(Report.id == report_id))
    assert surviving_report is not None
    assert report_service.report_dict(db, surviving_report)["source_deleted"] is True
    comparison_snapshot = db.scalar(select(Report).where(Report.id == compare_report.id))
    assert comparison_snapshot is not None
    assert report_service.report_dict(db, comparison_snapshot)["source_deleted"] is True
    assert html_path.is_file() and payload_path.is_file() and xlsx_path.is_file()
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["evaluation"]["id"] == str(evaluation.id)
    assert payload["metrics"][0]["value"] == 0.125
