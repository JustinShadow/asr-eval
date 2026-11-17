"""Run 队列的有界派发与重复入队保护。"""

import threading
import uuid

import pytest

from app.domain.run_worker import RunWorkerPool


def test_run_worker_queue_is_bounded_and_deduplicates_active_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """活动 Run 不重复入队；满队列拒绝暂存但保留数据库重扫能力。"""
    pool = RunWorkerPool(workers=1, queue_capacity=1, poll_interval_s=0.1)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    processed: list[uuid.UUID] = []

    def process(run_id: uuid.UUID) -> str:
        started.set()
        assert release.wait(timeout=2)
        processed.append(run_id)
        if len(processed) == 2:
            finished.set()
        return "completed"

    monkeypatch.setattr(pool, "_process", process)
    monkeypatch.setattr(pool, "_dispatch_pending", lambda: None)
    pool.start(lambda: None)

    first, second, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert pool.enqueue(first)
    assert started.wait(timeout=2)
    assert pool.enqueue(first)  # 正在执行的 Run 重复入队是幂等成功。
    assert pool.enqueue(second)
    assert not pool.enqueue(third)  # 满队列时由数据库扫描补派。

    release.set()
    assert finished.wait(timeout=2)
    pool.shutdown(timeout_s=2)

    assert processed == [first, second]
