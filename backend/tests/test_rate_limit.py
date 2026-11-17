"""M7 滑动窗口与 SSE 并发限流测试。"""

from app.core.rate_limit import ConcurrentLimiter, SlidingWindowLimiter


def test_sliding_window_checks_login_ip_and_username_atomically() -> None:
    """登录 IP 或账号任一额度耗尽时都返回正确等待时间。"""
    now = [100.0]
    limiter = SlidingWindowLimiter(clock=lambda: now[0])
    dimensions = [("ip:127.0.0.1", 5, 60), ("user:alice", 10, 60)]

    for _ in range(5):
        assert limiter.consume_many(dimensions) is None
    assert limiter.consume_many(dimensions) == 60
    assert limiter.consume("user:other", 10, 60) is None

    now[0] += 60
    assert limiter.consume_many(dimensions) is None


def test_sliding_window_expires_requests_and_limits_report_generation() -> None:
    """旧窗口过期后恢复额度，报告用户维度按小时单独计数。"""
    now = [0.0]
    limiter = SlidingWindowLimiter(clock=lambda: now[0])

    for _ in range(10):
        assert limiter.consume("report:alice", 10, 3600) is None
    assert limiter.consume("report:alice", 10, 3600) == 3600
    now[0] = 3600
    assert limiter.consume("report:alice", 10, 3600) is None


def test_concurrent_limiter_releases_slots() -> None:
    """每用户最多持有配置数量的 SSE 连接，关闭后返还额度。"""
    limiter = ConcurrentLimiter()
    for _ in range(5):
        assert limiter.acquire("alice", 5)
    assert limiter.acquire("alice", 5) is False
    limiter.release("alice")
    assert limiter.acquire("alice", 5)
