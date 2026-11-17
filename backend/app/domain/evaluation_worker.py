"""Evaluation 有界评分队列与固定并发 worker。"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.db import get_session_factory
from app.domain.models import Evaluation, Run
from app.domain.runs import process_evaluation
from app.domain.user_events import notify_resource_change

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Session]


class EvaluationWorkerPool:
    """有界任务队列；线程只拿 Evaluation ID，每个任务单独创建数据库会话。"""

    def __init__(self, workers: int, queue_capacity: int) -> None:
        self._worker_count = max(1, workers)
        self._queue: queue.Queue[uuid.UUID | None] = queue.Queue(maxsize=max(1, queue_capacity))
        self._lock = threading.Lock()
        self._queued: set[uuid.UUID] = set()
        self._active: set[uuid.UUID] = set()
        self._threads: list[threading.Thread] = []
        self._session_factory: SessionFactory = get_session_factory()

    def start(
        self,
        session_factory: SessionFactory | sessionmaker[Session] | None = None,
        *,
        recover: bool = False,
    ) -> None:
        """启动固定数量 worker；首次启动时可恢复上次进程留下的任务。"""
        with self._lock:
            if session_factory is not None:
                self._session_factory = session_factory
            if self._threads:
                return
            self._threads = [
                threading.Thread(
                    target=self._worker,
                    name=f"evaluation-worker-{index + 1}",
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
        evaluation_id: uuid.UUID,
        session_factory: SessionFactory | sessionmaker[Session] | None = None,
    ) -> bool:
        """尽力放入有界队列；队列满时任务仍留在数据库等待后续调度。"""
        self.start(session_factory)
        with self._lock:
            if evaluation_id in self._queued or evaluation_id in self._active:
                return True
            try:
                self._queue.put_nowait(evaluation_id)
            except queue.Full:
                return False
            self._queued.add(evaluation_id)
            return True

    def shutdown(self) -> None:
        """优雅排空队列并停止线程，供应用退出时调用。"""
        with self._lock:
            threads = list(self._threads)
            self._threads = []
        for _thread in threads:
            self._queue.put(None)
        for thread in threads:
            thread.join(timeout=30)

    def _recover_pending(self) -> None:
        factory = self._session_factory
        with factory() as db:
            rows = db.scalars(
                select(Evaluation)
                .where(Evaluation.status.in_(["queued", "scoring", "cancelling"]))
                .order_by(Evaluation.created_at, Evaluation.id)
            ).all()
            queued_ids: list[uuid.UUID] = []
            for evaluation in rows:
                if evaluation.status == "cancelling":
                    evaluation.status = "cancelled"
                    evaluation.finished_at = datetime.now(UTC)
                    evaluation.progress = {
                        **evaluation.progress,
                        "phase": "cancelled",
                    }
                else:
                    evaluation.status = "queued"
                    queued_ids.append(evaluation.id)
            db.commit()
        for evaluation_id in queued_ids:
            self.enqueue(evaluation_id)

    def _worker(self) -> None:
        while True:
            evaluation_id = self._queue.get()
            if evaluation_id is None:
                self._queue.task_done()
                return
            with self._lock:
                self._queued.discard(evaluation_id)
                self._active.add(evaluation_id)
            try:
                self._process(evaluation_id)
            finally:
                with self._lock:
                    self._active.discard(evaluation_id)
                self._queue.task_done()
                self._dispatch_pending()

    def _process(self, evaluation_id: uuid.UUID) -> None:
        factory = self._session_factory
        with factory() as db:
            claimed_id = db.scalar(
                update(Evaluation)
                .where(Evaluation.id == evaluation_id, Evaluation.status == "queued")
                .values(status="scoring", version=Evaluation.version + 1)
                .returning(Evaluation.id)
            )
            if claimed_id is not None:
                actor = db.scalar(
                    select(Run.created_by)
                    .join(Evaluation, Evaluation.run_id == Run.id)
                    .where(Evaluation.id == evaluation_id)
                )
                evaluation = db.get(Evaluation, evaluation_id)
                version = evaluation.version if evaluation is not None else 1
                notify_resource_change(
                    db,
                    resource_type="evaluation",
                    resource_id=str(evaluation_id),
                    version=version,
                    actor=actor,
                    label="评分任务",
                )
            db.commit()
            if claimed_id is None:
                return
            try:
                process_evaluation(db, evaluation_id)
            except Exception as exc:
                db.rollback()
                evaluation = db.get(Evaluation, evaluation_id)
                if evaluation is None:
                    return
                if evaluation.status == "cancelling":
                    evaluation.status = "cancelled"
                    evaluation.progress = {**evaluation.progress, "phase": "cancelled"}
                else:
                    evaluation.status = "failed"
                    evaluation.metrics = {
                        **evaluation.metrics,
                        "error": {
                            "code": "scoring_failed",
                            "type": type(exc).__name__,
                            "message": "评分失败，可在修复问题后续跑",
                        },
                    }
                    logger.exception("Evaluation 评分失败：%s", evaluation_id)
                evaluation.finished_at = datetime.now(UTC)
                db.commit()

    def _dispatch_pending(self) -> None:
        factory = self._session_factory
        try:
            with factory() as db:
                evaluation_ids = db.scalars(
                    select(Evaluation.id)
                    .where(Evaluation.status == "queued")
                    .order_by(Evaluation.created_at, Evaluation.id)
                    .limit(self._queue.maxsize)
                ).all()
        except Exception:
            logger.exception("读取待评分 Evaluation 队列失败")
            return
        for evaluation_id in evaluation_ids:
            self.enqueue(evaluation_id)


_pool: EvaluationWorkerPool | None = None
_pool_lock = threading.Lock()


def get_evaluation_worker() -> EvaluationWorkerPool:
    """读取按当前应用配置创建的进程内评分池。"""
    global _pool
    with _pool_lock:
        if _pool is None:
            settings = get_settings()
            _pool = EvaluationWorkerPool(
                workers=settings.scoring_workers,
                queue_capacity=settings.scoring_queue_capacity,
            )
        return _pool
