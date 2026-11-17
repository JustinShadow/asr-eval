"""评分 worker 固定并发与百任务排队测试。"""

import threading
import time
import uuid

from app.domain.evaluation_worker import EvaluationWorkerPool


def test_worker_pool_caps_concurrency_and_accepts_one_hundred_jobs() -> None:
    pool = EvaluationWorkerPool(workers=2, queue_capacity=100)
    lock = threading.Lock()
    active = 0
    maximum_active = 0
    completed: set[uuid.UUID] = set()
    finished = threading.Event()
    evaluation_ids = [uuid.uuid4() for _ in range(100)]

    def fake_process(evaluation_id: uuid.UUID) -> None:
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.01)
        with lock:
            active -= 1
            completed.add(evaluation_id)
            if len(completed) == len(evaluation_ids):
                finished.set()

    pool._process = fake_process  # type: ignore[method-assign]
    pool.start()
    try:
        assert all(pool.enqueue(evaluation_id) for evaluation_id in evaluation_ids)
        assert finished.wait(timeout=10)
    finally:
        pool.shutdown()

    assert completed == set(evaluation_ids)
    assert maximum_active == 2
    assert pool._queue.maxsize == 100
