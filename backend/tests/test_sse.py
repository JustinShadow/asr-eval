"""SSE 历史、游标续传与缓冲断档测试。"""

from app.core.sse import SSEEventBuffer, format_sse


def test_sse_buffer_replays_after_last_event_id() -> None:
    """Last-Event-ID 后续事件按原 ID 和数据补发。"""
    buffer = SSEEventBuffer()
    first = buffer.publish("run:1", "run.running", {"done": 1})
    second = buffer.publish("run:1", "run.progress", {"done": 2})

    replay = buffer.replay("run:1", str(first.event_id))

    assert replay.resync is False
    assert replay.events == [second]
    assert format_sse(second) == ('id: 2\nevent: run.progress\ndata: {"done": 2}\n\n')


def test_sse_buffer_signals_resync_after_overflow_or_process_restart() -> None:
    """超过保留窗口或缓冲为空时不伪造连续历史。"""
    buffer = SSEEventBuffer(max_events=2)
    for value in range(3):
        buffer.publish("evaluation:1", "evaluation.progress", {"done": value})

    overflow = buffer.replay("evaluation:1", "0")
    restarted = SSEEventBuffer().replay("evaluation:1", "3")

    assert overflow.resync is True
    assert overflow.events == []
    assert restarted.resync is True


def test_sse_buffer_expires_history_after_ten_minutes() -> None:
    """历史超过保留期后要求前端重新读取资源快照。"""
    now = [0.0]
    buffer = SSEEventBuffer(retention_seconds=600, clock=lambda: now[0])
    event = buffer.publish("run:2", "run.running", {"done": 1})
    now[0] = 601.0

    replay = buffer.replay("run:2", str(event.event_id))

    assert replay.resync is True
    assert replay.events == []
