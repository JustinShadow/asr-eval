"""从 PostgreSQL 恢复并调度 queued Run 的固定并发 worker。"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.db import get_session_factory
from app.core.errors import ApiError
from app.domain.models import Job, Run, RunTarget
from app.domain.runs import execute_run

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Session]


def ensure_run_job(db: Session, run_id: uuid.UUID) -> Job | None:
    """确保 queued Run 在 PostgreSQL 通用任务表中有持久队列项。"""
    run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
    if run is None or run.status != "queued":
        return None
    active_job = db.scalar(
        select(Job)
        .where(
            Job.kind == "run",
            Job.ref_type == "run",
            Job.ref_id == str(run_id),
            Job.status.in_(("queued", "running")),
        )
        .order_by(Job.created_at.desc(), Job.id.desc())
        .with_for_update()
    )
    if active_job is not None:
        return active_job
    job = Job(
        kind="run",
        ref_type="run",
        ref_id=str(run_id),
        status="queued",
        progress={**(run.progress or {}), "phase": "queued"},
        created_by=run.created_by,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


class RunWorkerPool:
    """有界进程内派发队列；任务状态保存在数据库，进程队列只存 Run ID。"""

    def __init__(self, workers: int, queue_capacity: int, poll_interval_s: float = 1.0) -> None:
        self._worker_count = max(1, workers)
        self._queue: queue.Queue[uuid.UUID | None] = queue.Queue(maxsize=max(1, queue_capacity))
        self._poll_interval_s = max(0.1, poll_interval_s)
        self._lock = threading.Lock()
        self._queued: set[uuid.UUID] = set()
        self._active: set[uuid.UUID] = set()
        self._retry_after: dict[uuid.UUID, float] = {}
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._session_factory: SessionFactory = get_session_factory()

    def start(
        self,
        session_factory: SessionFactory | sessionmaker[Session] | None = None,
        *,
        recover: bool = False,
    ) -> None:
        """启动固定数量 worker，并可恢复进程退出时遗留的执行。"""
        with self._lock:
            if session_factory is not None:
                self._session_factory = session_factory
            if self._threads:
                return
            self._stop.clear()
            self._threads = [
                threading.Thread(
                    target=self._worker,
                    name=f"run-worker-{index + 1}",
                    daemon=True,
                )
                for index in range(self._worker_count)
            ]
            for thread in self._threads:
                thread.start()
        if recover:
            self._recover_pending()

    def enqueue(
        self,
        run_id: uuid.UUID,
        session_factory: SessionFactory | sessionmaker[Session] | None = None,
    ) -> bool:
        """尽力派发；队列满时 Run 仍留在数据库，worker 会再次扫描。"""
        self.start(session_factory)
        with self._lock:
            if run_id in self._queued or run_id in self._active:
                return True
            try:
                self._queue.put_nowait(run_id)
            except queue.Full:
                return False
            self._queued.add(run_id)
            return True

    def shutdown(self, timeout_s: float = 120.0) -> None:
        """停止接收任务，等待当前设备会话按协议正常收尾。"""
        with self._lock:
            threads = list(self._threads)
            self._threads = []
            self._stop.set()
        while True:
            try:
                pending_id = self._queue.get_nowait()
            except queue.Empty:
                break
            if pending_id is not None:
                with self._lock:
                    self._queued.discard(pending_id)
            self._queue.task_done()
        deadline = time.monotonic() + timeout_s
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            self._queued.clear()
            self._active.clear()
            self._retry_after.clear()

    def _recover_pending(self) -> None:
        """恢复中断的 Run/Job，并为旧的 queued Run 补齐持久任务项。"""
        factory = self._session_factory
        with factory() as db:
            interrupted = db.scalars(select(Run).where(Run.status == "running")).all()
            interrupted_ids = [run.id for run in interrupted]
            for run in interrupted:
                run.status = "queued"
                run.finished_at = None
                run.progress = {**(run.progress or {}), "phase": "recovered"}
            if interrupted_ids:
                targets = db.scalars(
                    select(RunTarget).where(
                        RunTarget.run_id.in_(interrupted_ids), RunTarget.status == "running"
                    )
                ).all()
                for target in targets:
                    target.status = "pending"
                    target.finished_at = None
                    target.stats = {**(target.stats or {}), "recovered": True}
            cancelled_ids = [
                run.id for run in db.scalars(select(Run).where(Run.status == "cancelling")).all()
            ]
            for run in db.scalars(select(Run).where(Run.id.in_(cancelled_ids))).all():
                run.status = "cancelled"
                run.finished_at = datetime.now(UTC)
            if cancelled_ids:
                for target in db.scalars(
                    select(RunTarget).where(
                        RunTarget.run_id.in_(cancelled_ids), RunTarget.status == "running"
                    )
                ).all():
                    target.status = "cancelled"
                    target.finished_at = datetime.now(UTC)
            run_jobs = db.scalars(select(Job).where(Job.kind == "run", Job.ref_type == "run")).all()
            for job in run_jobs:
                previous_state = (
                    job.status,
                    job.started_at,
                    job.finished_at,
                    job.error_code,
                    job.progress,
                )
                run_id = _job_run_id(job)
                matched_run = db.get(Run, run_id) if run_id is not None else None
                if matched_run is None:
                    job.status = "failed"
                    job.error_code = "E_RUN_NOT_FOUND"
                    job.finished_at = datetime.now(UTC)
                elif matched_run.status == "queued":
                    job.status = "queued"
                    job.started_at = None
                    job.finished_at = None
                    job.error_code = None
                    job.progress = {**(matched_run.progress or {}), "phase": "recovered"}
                elif matched_run.status in {"completed", "partial"}:
                    job.status = "succeeded"
                    job.finished_at = matched_run.finished_at or datetime.now(UTC)
                elif matched_run.status == "cancelled":
                    job.status = "cancelled"
                    job.finished_at = matched_run.finished_at or datetime.now(UTC)
                elif matched_run.status in {"failed", "aborted_condition_changed"}:
                    job.status = "failed"
                    job.finished_at = matched_run.finished_at or datetime.now(UTC)
                current_state = (
                    job.status,
                    job.started_at,
                    job.finished_at,
                    job.error_code,
                    job.progress,
                )
                if current_state != previous_state:
                    job.version += 1
            existing_job_ids = {
                job.ref_id
                for job in run_jobs
                if job.status in {"queued", "running"} and job.ref_type == "run"
            }
            for run in db.scalars(select(Run).where(Run.status == "queued")).all():
                if str(run.id) not in existing_job_ids:
                    db.add(
                        Job(
                            kind="run",
                            ref_type="run",
                            ref_id=str(run.id),
                            status="queued",
                            progress={**(run.progress or {}), "phase": "recovered"},
                            created_by=run.created_by,
                        )
                    )
            db.commit()
        self._dispatch_pending()

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                run_id = self._queue.get(timeout=self._poll_interval_s)
            except queue.Empty:
                if self._stop.is_set():
                    return
                self._dispatch_pending()
                continue
            if run_id is None:
                self._queue.task_done()
                return
            if self._stop.is_set():
                self._queue.task_done()
                return
            with self._lock:
                self._queued.discard(run_id)
                self._active.add(run_id)
            try:
                status = self._process(run_id)
                if status == "queued":
                    with self._lock:
                        self._retry_after[run_id] = time.monotonic() + self._poll_interval_s
            finally:
                with self._lock:
                    self._active.discard(run_id)
                self._queue.task_done()
                self._dispatch_pending()

    def _process(self, run_id: uuid.UUID) -> str | None:
        factory = self._session_factory
        try:
            with factory() as db:
                job = db.scalar(
                    select(Job)
                    .where(
                        Job.kind == "run",
                        Job.ref_type == "run",
                        Job.ref_id == str(run_id),
                        Job.status == "queued",
                    )
                    .order_by(Job.created_at, Job.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if job is None:
                    return None
                job.status = "running"
                job.started_at = datetime.now(UTC)
                job.version += 1
                db.commit()
            with factory() as db:
                run = asyncio.run(execute_run(db, run_id))
                self._sync_job(run_id, run.status)
                return run.status
        except ApiError as exc:
            if exc.code == "E_CONFLICT":
                self._sync_job(run_id, self._run_status(run_id) or "failed")
                return None
            logger.exception("Run 执行失败：%s", run_id)
            self._mark_run_failed(run_id, exc.code)
            return None
        except Exception as exc:
            logger.exception("Run 执行异常：%s", run_id)
            self._mark_run_failed(run_id, type(exc).__name__)
            return None

    def _run_status(self, run_id: uuid.UUID) -> str | None:
        """读取 Run 当前状态，处理并发 worker 的重复派发。"""
        with self._session_factory() as db:
            return db.scalar(select(Run.status).where(Run.id == run_id))

    def _sync_job(self, run_id: uuid.UUID, run_status: str) -> None:
        """把 Run 执行状态映射回通用异步任务。"""
        with self._session_factory() as db:
            jobs = db.scalars(
                select(Job)
                .where(
                    Job.kind == "run",
                    Job.ref_type == "run",
                    Job.ref_id == str(run_id),
                    Job.status.in_(("queued", "running")),
                )
                .with_for_update()
            ).all()
            run = db.get(Run, run_id)
            if run is None:
                return
            for job in jobs:
                job.progress = {**(run.progress or {}), "phase": run_status}
                if run_status == "queued":
                    job.status = "queued"
                    job.started_at = None
                    job.finished_at = None
                elif run_status in {"running", "probing", "preparing", "cancelling"}:
                    job.status = "running"
                elif run_status in {"completed", "partial"}:
                    job.status = "succeeded"
                    job.result = {"run_id": str(run_id), "run_status": run_status}
                    job.finished_at = run.finished_at or datetime.now(UTC)
                elif run_status == "cancelled":
                    job.status = "cancelled"
                    job.finished_at = run.finished_at or datetime.now(UTC)
                else:
                    job.status = "failed"
                    job.error_code = "E_RUN_FAILED"
                    job.error_detail = {"run_status": run_status}
                    job.finished_at = run.finished_at or datetime.now(UTC)
                job.version += 1
            db.commit()

    def _mark_run_failed(self, run_id: uuid.UUID, reason: str) -> None:
        """把未处理异常写入 Run 与异步任务的终态。"""
        with self._session_factory() as db:
            run = db.get(Run, run_id)
            if run is not None and run.status in {"queued", "running", "cancelling"}:
                run.status = "cancelled" if run.status == "cancelling" else "failed"
                run.finished_at = datetime.now(UTC)
                phase = "cancelled" if run.status == "cancelled" else "failed"
                run.progress = {**(run.progress or {}), "phase": phase, "error": reason}
                if run.status == "cancelled":
                    running_targets = db.scalars(
                        select(RunTarget).where(
                            RunTarget.run_id == run.id, RunTarget.status == "running"
                        )
                    ).all()
                    for target in running_targets:
                        target.status = "cancelled"
                        target.finished_at = run.finished_at
                db.commit()
        status = self._run_status(run_id) or "failed"
        self._sync_job(run_id, status)

    def _dispatch_pending(self) -> None:
        """按创建时间扫描 PostgreSQL 队列，避免仅依赖易失的内存队列。"""
        if self._stop.is_set():
            return
        factory = self._session_factory
        try:
            with factory() as db:
                run_ids = db.scalars(
                    select(Job.ref_id)
                    .where(
                        Job.kind == "run",
                        Job.ref_type == "run",
                        Job.status == "queued",
                    )
                    .order_by(Job.created_at, Job.id)
                    .limit(self._queue.maxsize)
                ).all()
        except Exception:
            logger.exception("读取待执行 Run 队列失败")
            return
        now = time.monotonic()
        for raw_run_id in run_ids:
            try:
                run_id = uuid.UUID(raw_run_id)
            except (ValueError, TypeError):
                logger.error("Run Job 引用了无效 Run ID：%s", raw_run_id)
                continue
            with self._lock:
                if run_id in self._queued or run_id in self._active:
                    continue
                if self._retry_after.get(run_id, 0.0) > now:
                    continue
                try:
                    self._queue.put_nowait(run_id)
                except queue.Full:
                    return
                self._queued.add(run_id)
                self._retry_after.pop(run_id, None)


_pool: RunWorkerPool | None = None
_pool_lock = threading.Lock()


def get_run_worker() -> RunWorkerPool:
    """按当前配置获取 Run worker 池。"""
    global _pool
    with _pool_lock:
        if _pool is None:
            settings = get_settings()
            _pool = RunWorkerPool(
                workers=settings.runner_workers,
                queue_capacity=settings.runner_queue_capacity,
                poll_interval_s=settings.runner_poll_interval_s,
            )
        return _pool


def _job_run_id(job: Job) -> uuid.UUID | None:
    """安全解析通用 Job 中引用的 Run UUID。"""
    try:
        return uuid.UUID(job.ref_id)
    except (ValueError, TypeError):
        return None
