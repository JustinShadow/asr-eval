"""数据集能力重新分析任务（M1 §3.2、M7 §6）。"""

import threading
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ApiError
from app.domain.datasets import recompute_version_capabilities
from app.domain.models import DatasetVersion, Job

_workers: dict[uuid.UUID, threading.Thread] = {}


def start_reanalysis(
    db: Session,
    version_id: uuid.UUID,
    *,
    created_by: str | None,
) -> Job:
    """校验版本并创建统一 Job，后台从已落库样本重算 capabilities。"""
    version = db.get(DatasetVersion, version_id)
    if version is None:
        raise ApiError("E_NOT_FOUND", "版本不存在", status_code=404)
    if version.status not in {"ready", "ready_with_errors"}:
        raise ApiError("E_CONFLICT", "只有已完成导入的版本可以重新分析", status_code=409)
    existing = db.scalar(
        select(Job.id)
        .where(
            Job.kind == "reanalyze",
            Job.ref_type == "dataset_version",
            Job.ref_id == str(version_id),
            Job.status.in_(["queued", "running"]),
        )
        .limit(1)
    )
    if existing is not None:
        raise ApiError(
            "E_JOB_ACTIVE",
            "该数据集版本已有重新分析任务运行中",
            status_code=409,
            detail={"job_id": str(existing)},
        )

    job = Job(
        kind="reanalyze",
        ref_type="dataset_version",
        ref_id=str(version_id),
        status="queued",
        created_by=created_by,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    thread = threading.Thread(
        target=_run_reanalysis,
        args=(factory, job.id, version_id),
        daemon=True,
        name=f"reanalyze-job-{job.id}",
    )
    _workers[job.id] = thread
    thread.start()
    return job


def _run_reanalysis(
    factory: sessionmaker[Session], job_id: uuid.UUID, version_id: uuid.UUID
) -> None:
    """后台重算能力并写回统一 Job 的阶段、结果或错误。"""
    db = factory()
    try:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None or job.status == "cancelled":
            return
        if job.cancel_requested_at is not None:
            _finish_cancelled(job)
            db.commit()
            return
        job.status = "running"
        job.started_at = datetime.now(UTC)
        job.progress = {"phase": "analyzing"}
        db.commit()

        capabilities = recompute_version_capabilities(db, version_id)
        db.flush()
        db.refresh(job)
        if job.cancel_requested_at is not None:
            db.rollback()
            cancelled_job = db.get(Job, job_id)
            if cancelled_job is not None:
                _finish_cancelled(cancelled_job)
                db.commit()
            return

        job.result = {
            "resource_id": str(version_id),
            "url": f"/api/v1/dataset-versions/{version_id}",
            "capabilities": capabilities,
        }
        job.progress = {"phase": "done", "completed": 1, "total": 1}
        job.status = "succeeded"
        job.finished_at = datetime.now(UTC)
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.get(Job, job_id)
        if job is not None:
            if isinstance(exc, ApiError):
                code = exc.code
                message = exc.message
            else:
                code = "E_REANALYZE_FAILED"
                message = str(exc)
            if code == "E_JOB_CANCELLED" or job.cancel_requested_at is not None:
                _finish_cancelled(job)
            else:
                job.status = "failed"
                job.error_code = code
                job.error_detail = {"message": message[:1024]}
                job.progress = {**(job.progress or {}), "phase": "failed"}
                job.finished_at = datetime.now(UTC)
            db.commit()
    finally:
        db.close()
        _workers.pop(job_id, None)


def _finish_cancelled(job: Job) -> None:
    """统一记录用户取消的终态。"""
    job.status = "cancelled"
    job.error_code = "E_JOB_CANCELLED"
    job.error_detail = {"message": "重新分析任务已取消"}
    job.progress = {**(job.progress or {}), "phase": "cancelled"}
    job.finished_at = datetime.now(UTC)


def wait_for_reanalysis_tasks(timeout_s: float = 120.0) -> None:
    """测试辅助：等待重新分析任务线程结束。"""
    for thread in list(_workers.values()):
        thread.join(timeout=timeout_s)
