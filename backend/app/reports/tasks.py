"""报告生成 Job 的创建与后台执行（M7 §6）。"""

import threading
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ApiError
from app.domain.models import Job
from app.reports.service import create_report, validate_report_request

_report_workers: dict[uuid.UUID, threading.Thread] = {}


def start_report(
    db: Session,
    *,
    kind: str,
    evaluation_ids: list[uuid.UUID],
    query: dict[str, object],
    level: str,
    created_by: str | None,
) -> Job:
    """校验报告来源、创建 Job 并在后台构建快照。"""
    validate_report_request(
        db,
        kind=kind,
        evaluation_ids=evaluation_ids,
        level=level,
    )
    job = Job(
        kind="report",
        ref_type="evaluation",
        ref_id=str(evaluation_ids[0]),
        status="queued",
        # Run 删除保护需检查多评分报告的全部来源，ref_id 只保留首个评分用于旧查询兼容。
        progress={
            "phase": "queued",
            "evaluation_ids": [str(evaluation_id) for evaluation_id in evaluation_ids],
        },
        created_by=created_by,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    thread = threading.Thread(
        target=_run_report_job,
        args=(factory, job.id, kind, evaluation_ids, query, level, created_by),
        daemon=True,
        name=f"report-job-{job.id}",
    )
    _report_workers[job.id] = thread
    thread.start()
    return job


def _run_report_job(
    factory: sessionmaker[Session],
    job_id: uuid.UUID,
    kind: str,
    evaluation_ids: list[uuid.UUID],
    query: dict[str, object],
    level: str,
    created_by: str | None,
) -> None:
    """后台执行报告快照生成，并将失败或取消写回 Job。"""
    db = factory()
    try:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None or job.status == "cancelled":
            return
        if job.cancel_requested_at is not None:
            _finish_report_job_cancelled(job)
            db.commit()
            return

        job.status = "running"
        job.started_at = datetime.now(UTC)
        job.progress = {**(job.progress or {}), "phase": "preparing"}
        db.commit()
        create_report(
            db,
            kind=kind,
            evaluation_ids=evaluation_ids,
            query=query,
            level=level,
            created_by=created_by,
            job=job,
        )
    except Exception as exc:
        db.rollback()
        job = db.get(Job, job_id)
        if job is not None:
            if isinstance(exc, ApiError):
                error_code = exc.code
                message = exc.message
            else:
                error_code = "E_REPORT_FAILED"
                message = str(exc)
            if error_code == "E_JOB_CANCELLED" or job.cancel_requested_at is not None:
                _finish_report_job_cancelled(job)
            else:
                job.status = "failed"
                job.error_code = error_code
                job.error_detail = {"message": message[:1024]}
                job.progress = {**(job.progress or {}), "phase": "failed"}
                job.finished_at = datetime.now(UTC)
            db.commit()
    finally:
        db.close()
        _report_workers.pop(job_id, None)


def _finish_report_job_cancelled(job: Job) -> None:
    """将报告任务统一落为 cancelled 终态。"""
    job.status = "cancelled"
    job.error_code = "E_JOB_CANCELLED"
    job.error_detail = {"message": "报告生成任务已取消"}
    job.progress = {**(job.progress or {}), "phase": "cancelled"}
    job.finished_at = datetime.now(UTC)


def wait_for_report_tasks(timeout_s: float = 120.0) -> None:
    """测试辅助：等待当前报告生成 Job 线程结束。"""
    for thread in list(_report_workers.values()):
        thread.join(timeout=timeout_s)
