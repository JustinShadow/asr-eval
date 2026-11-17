"""进程内滑动窗口与 SSE 并发限流（M7 §4.8）。"""

import math
import time
from collections import defaultdict, deque
from collections.abc import Callable
from threading import Lock


class SlidingWindowLimiter:
    """按 key 保存事件时间，支持一次校验并消耗多个限流维度。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def consume(self, key: str, limit: int, window_seconds: float) -> int | None:
        """消耗一次额度；超限时返回建议等待秒数。"""
        return self.consume_many([(key, limit, window_seconds)])

    def consume_many(self, limits: list[tuple[str, int, float]]) -> int | None:
        """所有维度都可用时才一起记账，避免部分额度被失败请求占用。"""
        now = self._clock()
        with self._lock:
            retry_after = 0
            for key, limit, window_seconds in limits:
                events = self._events[key]
                while events and now - events[0] >= window_seconds:
                    events.popleft()
                if len(events) >= limit:
                    retry_after = max(
                        retry_after,
                        math.ceil(window_seconds - (now - events[0])),
                    )
            if retry_after:
                return max(retry_after, 1)
            for key, _limit, _window_seconds in limits:
                self._events[key].append(now)
            return None

    def clear(self) -> None:
        """清空窗口；测试可用来隔离共享的 TestClient 请求。"""
        with self._lock:
            self._events.clear()


class ConcurrentLimiter:
    """限制同一用户同时打开的事件流数量。"""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = Lock()

    def acquire(self, key: str, limit: int) -> bool:
        """额度未满时登记一个活动连接。"""
        with self._lock:
            current = self._counts.get(key, 0)
            if current >= limit:
                return False
            self._counts[key] = current + 1
            return True

    def release(self, key: str) -> None:
        """关闭连接时归还额度；重复清理不会产生负数。"""
        with self._lock:
            current = self._counts.get(key, 0)
            if current <= 1:
                self._counts.pop(key, None)
            else:
                self._counts[key] = current - 1

    def clear(self) -> None:
        """清空活动数；仅供隔离测试。"""
        with self._lock:
            self._counts.clear()


rate_limiter = SlidingWindowLimiter()
sse_connection_limiter = ConcurrentLimiter()


def require_limit(retry_after: int | None, *, message: str = "请求过于频繁") -> None:
    """超限时抛出统一 429，并提供客户端重试间隔。"""
    if retry_after is not None:
        from app.core.errors import ApiError

        raise ApiError(
            "E_RATE_LIMITED",
            message,
            status_code=429,
            detail={"retry_after": retry_after},
            headers={"Retry-After": str(retry_after)},
        )
