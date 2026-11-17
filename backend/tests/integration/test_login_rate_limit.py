"""M7 登录限流响应集成测试。"""

import pytest
from fastapi.testclient import TestClient

from app.core.rate_limit import rate_limiter

pytestmark = pytest.mark.integration


def test_sixth_login_attempt_is_rate_limited(client: TestClient) -> None:
    """同一来源每分钟第五次后限流并返回 Retry-After。"""
    client.cookies.clear()
    rate_limiter.clear()
    try:
        responses = [
            client.post(
                "/api/v1/auth/login",
                json={"username": "rate-limited-user", "password": "wrong-password"},
            )
            for _ in range(6)
        ]
        assert [response.status_code for response in responses[:5]] == [401] * 5
        assert responses[5].status_code == 429
        assert responses[5].json()["code"] == "E_RATE_LIMITED"
        assert responses[5].headers["Retry-After"] == "60"
    finally:
        rate_limiter.clear()
