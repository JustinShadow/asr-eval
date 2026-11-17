"""M7 安全响应头与开发 CORS 限制。"""

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_security_headers_are_set_on_api_responses(client: TestClient) -> None:
    """API 响应携带基础浏览器安全头。"""
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_development_cors_allows_only_local_frontend_origins(client: TestClient) -> None:
    """开发环境仅允许本机 Vite 来源携带凭据跨域访问。"""
    allowed = client.options(
        "/api/v1/auth/refresh",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization,if-match,if-none-match",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert allowed.headers["Access-Control-Allow-Credentials"] == "true"
    allowed_headers = allowed.headers["Access-Control-Allow-Headers"].lower()
    assert "if-match" in allowed_headers
    assert "if-none-match" in allowed_headers

    exposed = client.get("/healthz", headers={"Origin": "http://localhost:5173"})
    exposed_headers = exposed.headers["Access-Control-Expose-Headers"].lower()
    assert "etag" in exposed_headers
    assert "x-request-id" in exposed_headers

    denied = client.options(
        "/api/v1/auth/refresh",
        headers={
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert denied.status_code == 400
    assert "Access-Control-Allow-Origin" not in denied.headers
