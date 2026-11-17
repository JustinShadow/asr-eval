"""进程内 SSE 事件历史与断线续传（M7 §7.2–7.3）。"""

import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class SSEEvent:
    """可重放的单条 SSE 消息。"""

    event_id: int
    event: str
    data: str
    created_at: float


@dataclass(frozen=True)
class SSEReplay:
    """Last-Event-ID 的补发结果；历史不连续时要求客户端重同步。"""

    events: list[SSEEvent]
    latest: SSEEvent | None
    resync: bool


class SSEEventBuffer:
    """按资源保留有限事件历史，支持进程内重连补发。"""

    def __init__(
        self,
        *,
        max_events: int = 500,
        retention_seconds: float = 600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_events = max_events
        self._retention_seconds = retention_seconds
        self._clock = clock
        self._events: dict[str, deque[SSEEvent]] = {}
        self._next_ids: dict[str, int] = {}
        self._lock = Lock()

    def publish(self, stream: str, event: str, payload: dict[str, Any]) -> SSEEvent:
        """序列化单行数据、分配资源内单调递增 ID 并写入环形历史。"""
        now = self._clock()
        with self._lock:
            events = self._events.setdefault(stream, deque())
            self._prune(events, now)
            event_id = self._next_ids.get(stream, 0) + 1
            self._next_ids[stream] = event_id
            message = SSEEvent(
                event_id=event_id,
                event=event,
                data=json.dumps(payload, ensure_ascii=False),
                created_at=now,
            )
            events.append(message)
            while len(events) > self._max_events:
                events.popleft()
            return message

    def replay(self, stream: str, last_event_id: str | None) -> SSEReplay:
        """补发游标后的事件；游标非法、过期或缓冲断档时要求 resync。"""
        now = self._clock()
        with self._lock:
            events = self._events.setdefault(stream, deque())
            self._prune(events, now)
            latest = events[-1] if events else None
            if last_event_id is None:
                return SSEReplay(events=[], latest=latest, resync=False)
            try:
                cursor = int(last_event_id)
            except ValueError:
                return SSEReplay(events=[], latest=latest, resync=True)
            if cursor < 0 or latest is None or cursor > latest.event_id:
                return SSEReplay(events=[], latest=latest, resync=True)
            if cursor < events[0].event_id - 1:
                return SSEReplay(events=[], latest=latest, resync=True)
            return SSEReplay(
                events=[event for event in events if event.event_id > cursor],
                latest=latest,
                resync=False,
            )

    def _prune(self, events: deque[SSEEvent], now: float) -> None:
        """移除超过十分钟的历史；调用方已持有线程锁。"""
        while events and now - events[0].created_at > self._retention_seconds:
            events.popleft()


def format_sse(event: SSEEvent) -> str:
    """序列化符合 EventSource 规范的单行 JSON 帧。"""
    return f"id: {event.event_id}\nevent: {event.event}\ndata: {event.data}\n\n"


sse_event_buffer = SSEEventBuffer()
