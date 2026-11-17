"""refresh 轮换：一次性、复用检测、30s 幂等宽限（M7 §2.2 / D-M7-3 / D-M7-18）。

需要 PostgreSQL（compose 里的 asr_eval_test 库）；数据库不可用时整组跳过。
"""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import hash_refresh_token
from app.domain.models import RefreshGrace

pytestmark = pytest.mark.integration
SAME_SITE_ORIGIN = "http://localhost:5173"


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def test_grace_window_returns_same_new_token(client: TestClient) -> None:
    """轮换后 30s 内回放旧 token，幂等返回同一个新 refresh token。"""
    client.cookies.clear()
    _login(client)
    old_refresh = client.cookies.get("asr_refresh", domain="testserver.local", path="/api/v1/auth")
    assert old_refresh
    first = client.post("/api/v1/auth/refresh", headers={"Origin": SAME_SITE_ORIGIN})
    assert first.status_code == 200
    new_refresh = client.cookies.get("asr_refresh", domain="testserver.local", path="/api/v1/auth")
    assert new_refresh and new_refresh != old_refresh

    client.cookies.set(
        "asr_refresh",
        old_refresh,
        domain="testserver.local",
        path="/api/v1/auth",
    )
    second = client.post("/api/v1/auth/refresh", headers={"Origin": SAME_SITE_ORIGIN})
    assert second.status_code == 200
    assert (
        client.cookies.get("asr_refresh", domain="testserver.local", path="/api/v1/auth")
        == new_refresh
    )


def test_three_tabs_refresh_concurrently_without_session_reuse(client: TestClient) -> None:
    """多个浏览器标签同时轮换同一旧 cookie 时均获得同一个新会话 token。"""
    client.cookies.clear()
    _login(client)
    old_refresh = client.cookies.get("asr_refresh", domain="testserver.local", path="/api/v1/auth")
    assert old_refresh
    client.cookies.clear()

    async def refresh_tabs() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=client.app)
        tabs = [
            httpx.AsyncClient(transport=transport, base_url="http://testserver") for _ in range(3)
        ]
        for tab in tabs:
            tab.cookies.set("asr_refresh", old_refresh, path="/api/v1/auth")
        try:
            return list(
                await asyncio.gather(
                    *(
                        tab.post(
                            "/api/v1/auth/refresh",
                            headers={"Origin": SAME_SITE_ORIGIN},
                        )
                        for tab in tabs
                    )
                )
            )
        finally:
            await asyncio.gather(*(tab.aclose() for tab in tabs))

    responses = asyncio.run(refresh_tabs())
    assert [response.status_code for response in responses] == [200, 200, 200]
    refresh_tokens = {response.cookies.get("asr_refresh") for response in responses}
    assert len(refresh_tokens) == 1
    for response in responses:
        token = response.json()["access_token"]
        assert (
            client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code
            == 200
        )


def test_reuse_of_stale_token_revokes_family(client: TestClient) -> None:
    """宽限期外回放旧 token 会撤销整族会话并写入审计。"""
    client.cookies.clear()
    _login(client)
    old_refresh = client.cookies.get("asr_refresh", domain="testserver.local", path="/api/v1/auth")
    assert old_refresh
    first = client.post("/api/v1/auth/refresh", headers={"Origin": SAME_SITE_ORIGIN})
    assert first.status_code == 200

    session_factory = client.app.state.evaluation_session_factory
    with session_factory() as db:
        grace = db.scalar(
            select(RefreshGrace).where(RefreshGrace.old_hash == hash_refresh_token(old_refresh))
        )
        assert grace is not None
        grace.created_at = datetime.now(UTC) - timedelta(seconds=31)
        db.commit()

    client.cookies.set(
        "asr_refresh",
        old_refresh,
        domain="testserver.local",
        path="/api/v1/auth",
    )
    reused = client.post("/api/v1/auth/refresh", headers={"Origin": SAME_SITE_ORIGIN})
    assert reused.status_code == 401
    assert reused.json()["code"] == "E_REFRESH_REUSE"
    denied_access = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {first.json()['access_token']}"},
    )
    assert denied_access.status_code == 401

    _login(client)
    token = client.post("/api/v1/auth/refresh", headers={"Origin": SAME_SITE_ORIGIN}).json()[
        "access_token"
    ]
    rows = client.get("/api/v1/audit-logs", headers={"Authorization": f"Bearer {token}"})
    assert rows.status_code == 200
    assert any(row["action"] == "auth.refresh_reuse" for row in rows.json()["items"])


def test_login_failure_is_audited(client: TestClient) -> None:
    """登录失败落审计（M7 §2.1）。"""
    client.cookies.clear()
    bad = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "wrong-password"},
    )
    assert bad.status_code == 401

    _login(client)
    token = client.post("/api/v1/auth/refresh", headers={"Origin": SAME_SITE_ORIGIN}).json()[
        "access_token"
    ]
    rows = client.get("/api/v1/audit-logs", headers={"Authorization": f"Bearer {token}"})
    assert rows.status_code == 200


def test_validation_error_shape(client: TestClient) -> None:
    """校验错误走统一错误体（M7 §4.2）。"""
    client.cookies.clear()
    bad = client.post("/api/v1/auth/login", json={})
    assert bad.status_code == 422
    body = bad.json()
    assert body["code"] == "E_VALIDATION"
    assert "trace_id" in body


def test_request_id_echoed(client: TestClient) -> None:
    """X-Request-Id 必须回显（M7 §4.7）。"""
    client.cookies.clear()
    token = _login(client)
    response = client.get(
        "/api/v1/auth/me",
        headers={"X-Request-Id": "trace-check-123", "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.headers.get("X-Request-Id") == "trace-check-123"
