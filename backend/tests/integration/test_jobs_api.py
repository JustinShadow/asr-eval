"""M7 §6 统一 Job API、对象权限、取消与 SSE 验收。"""

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.security import hash_password
from app.domain.models import AppUser, Job

pytestmark = pytest.mark.integration


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    """登录并返回 Bearer 鉴权头。"""
    client.cookies.clear()
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_job(
    client: TestClient,
    *,
    created_by: str = "admin",
    status: str = "queued",
    kind: str = "import",
) -> Job:
    """写入测试 Job，避免为接口权限测试启动后台工作线程。"""
    factory = client.app.state.evaluation_session_factory
    with factory() as db:
        job = Job(
            kind=kind,
            ref_type="dataset",
            ref_id=str(uuid.uuid4()),
            status=status,
            progress={"phase": "persisting", "persisted": 2},
            created_by=created_by,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    with factory() as db:
        saved = db.get(Job, job_id)
        assert saved is not None
        return saved


def test_job_read_and_cancel_are_limited_to_owner_or_admin(client: TestClient) -> None:
    """任务资源仅创建者或管理员可读写；运行中取消转为安全检查点请求。"""
    factory = client.app.state.evaluation_session_factory
    with factory() as db:
        db.add(
            AppUser(
                username="job-owner",
                display_name="任务创建者",
                role="tester",
                role_source="local",
                password_hash=hash_password("job-owner-password"),
                active=True,
            )
        )
        db.commit()

    admin_headers = _login(client, "admin", "admin12345")
    owner_headers = _login(client, "job-owner", "job-owner-password")
    job = _create_job(client, created_by="job-owner")
    owner_read = client.get(f"/api/v1/jobs/{job.id}", headers=owner_headers)
    assert owner_read.status_code == 200
    admin_read = client.get(f"/api/v1/jobs/{job.id}", headers=admin_headers)
    assert admin_read.status_code == 200
    payload = admin_read.json()
    assert payload["id"] == str(job.id)
    assert payload["kind"] == "import"
    assert payload["ref"] == {"type": "dataset", "id": job.ref_id}
    assert payload["progress"]["persisted"] == 2

    with factory() as db:
        row = db.get(Job, job.id)
        assert row is not None
        row.created_by = "admin"
        db.commit()
    denied_owner = client.get(f"/api/v1/jobs/{job.id}", headers=owner_headers)
    assert denied_owner.status_code == 403
    assert denied_owner.json()["code"] == "E_NOT_OWNER"
    owner_ticket = client.post("/api/v1/auth/stream-ticket", headers=owner_headers)
    assert owner_ticket.status_code == 200
    denied_stream = client.get(
        f"/api/v1/jobs/{job.id}/events",
        params={"ticket": owner_ticket.json()["ticket"]},
    )
    assert denied_stream.status_code == 403
    assert denied_stream.json()["code"] == "E_NOT_OWNER"

    cancellable = _create_job(client, created_by="job-owner", status="running")
    accepted = client.post(f"/api/v1/jobs/{cancellable.id}/cancel", headers=owner_headers)
    assert accepted.status_code == 202
    repeated_request = client.post(f"/api/v1/jobs/{cancellable.id}/cancel", headers=owner_headers)
    assert repeated_request.status_code == 202
    with factory() as db:
        saved = db.get(Job, cancellable.id)
        assert saved is not None
        assert saved.status == "running"
        assert saved.cancel_requested_at is not None

    probe_job = _create_job(client, created_by="job-owner", status="running", kind="probe")
    probe_cancel = client.post(f"/api/v1/jobs/{probe_job.id}/cancel", headers=owner_headers)
    assert probe_cancel.status_code == 202
    with factory() as db:
        saved_probe = db.get(Job, probe_job.id)
        assert saved_probe is not None
        assert saved_probe.cancel_requested_at is not None

    report_job = _create_job(client, created_by="job-owner", status="running", kind="report")
    report_cancel = client.post(f"/api/v1/jobs/{report_job.id}/cancel", headers=owner_headers)
    assert report_cancel.status_code == 202

    queued = _create_job(client, created_by="job-owner")
    cancelled = client.post(f"/api/v1/jobs/{queued.id}/cancel", headers=owner_headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    repeated = client.post(f"/api/v1/jobs/{queued.id}/cancel", headers=owner_headers)
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "cancelled"

    queued_report = _create_job(client, created_by="job-owner", kind="report")
    cancelled_report = client.post(f"/api/v1/jobs/{queued_report.id}/cancel", headers=owner_headers)
    assert cancelled_report.status_code == 200
    assert cancelled_report.json()["status"] == "cancelled"
    assert cancelled_report.json()["error_detail"]["message"] == "报告生成任务已取消"

    unsupported = _create_job(client, created_by="job-owner", kind="export")
    conflict = client.post(f"/api/v1/jobs/{unsupported.id}/cancel", headers=owner_headers)
    assert conflict.status_code == 409


def test_job_events_use_one_time_stream_ticket_and_finish_terminal_stream(
    client: TestClient,
) -> None:
    """Job SSE 用一次性 ticket 鉴权，并为终态任务输出结果事件与 end。"""
    headers = _login(client, "admin", "admin12345")
    job = _create_job(client, status="succeeded")
    ticket_response = client.post("/api/v1/auth/stream-ticket", headers=headers)
    assert ticket_response.status_code == 200
    ticket = ticket_response.json()["ticket"]

    stream = client.get(
        f"/api/v1/jobs/{job.id}/events",
        params={"ticket": ticket},
    )
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: job.succeeded" in stream.text
    data_line = next(line for line in stream.text.splitlines() if line.startswith("data: "))
    assert json.loads(data_line.removeprefix("data: "))["id"] == str(job.id)
    assert "event: end" in stream.text

    reused = client.get(
        f"/api/v1/jobs/{job.id}/events",
        params={"ticket": ticket},
    )
    assert reused.status_code == 401
    assert reused.json()["code"] == "E_TICKET_INVALID"


def test_dataset_version_endpoint_returns_idempotent_job_location(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """导入入口返回 202 + /jobs Location，并由幂等键防止重复启动。"""
    headers = _login(client, "admin", "admin12345")
    dataset = client.post(
        "/api/v1/datasets",
        headers=headers,
        json={"name": f"job-dataset-{uuid.uuid4().hex[:8]}", "source_type": "apy"},
    )
    assert dataset.status_code == 201
    dataset_id = dataset.json()["id"]
    calls = 0

    def fake_start_import(db, dataset_row, **_kwargs):
        nonlocal calls
        calls += 1
        job = Job(
            kind="import",
            ref_type="dataset",
            ref_id=str(dataset_row.id),
            status="queued",
            created_by="admin",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    monkeypatch.setattr("app.api.datasets.start_import", fake_start_import)
    key = f"import-job-{uuid.uuid4()}"
    payload = {"root": str(tmp_path / "import-root"), "limit": 5, "version": "v1"}
    first = client.post(
        f"/api/v1/datasets/{dataset_id}/versions",
        headers={**headers, "Idempotency-Key": key},
        json=payload,
    )
    assert first.status_code == 202
    assert first.headers["Location"] == f"/api/v1/jobs/{first.json()['id']}"
    assert first.json()["kind"] == "import"
    assert first.json()["status"] == "queued"

    replay = client.post(
        f"/api/v1/datasets/{dataset_id}/versions",
        headers={**headers, "Idempotency-Key": key},
        json=payload,
    )
    assert replay.status_code == 202
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json()["id"] == first.json()["id"]
    assert calls == 1


def test_report_endpoint_returns_idempotent_job_location(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """报告创建按 M7 §6 返回统一 Job，并按幂等键复用任务。"""
    headers = _login(client, "admin", "admin12345")
    calls = 0

    def fake_start_report(db, *, kind, evaluation_ids, created_by, **_kwargs):
        nonlocal calls
        calls += 1
        job = Job(
            kind="report",
            ref_type="evaluation",
            ref_id=str(evaluation_ids[0]),
            status="queued",
            created_by=created_by,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        assert kind == "single"
        return job

    monkeypatch.setattr("app.api.reports.start_report", fake_start_report)
    key = f"report-job-{uuid.uuid4()}"
    payload = {"kind": "single", "evaluation_ids": [str(uuid.uuid4())]}
    first = client.post(
        "/api/v1/reports",
        headers={**headers, "Idempotency-Key": key},
        json=payload,
    )
    assert first.status_code == 202
    assert first.headers["Location"] == f"/api/v1/jobs/{first.json()['id']}"
    assert first.json()["kind"] == "report"

    replay = client.post(
        "/api/v1/reports",
        headers={**headers, "Idempotency-Key": key},
        json=payload,
    )
    assert replay.status_code == 202
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json()["id"] == first.json()["id"]
    assert calls == 1
