"""登录全链路集成测试。

需要 PostgreSQL（compose 里的 asr_eval_test 库）；数据库不可用时整组跳过。
"""

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration
SAME_SITE_ORIGIN = "http://localhost:5173"


def test_login_me_refresh_logout(client: TestClient) -> None:
    client.cookies.clear()
    # 错误密码：不区分用户不存在
    bad = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "wrong-password"},
    )
    assert bad.status_code == 401
    assert bad.json()["code"] == "E_AUTH_INVALID"

    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert login.status_code == 200
    body = login.json()
    assert body["token_type"] == "Bearer"
    assert body["user"]["role"] == "admin"
    assert "device:ops" in body["user"]["permissions"]
    assert client.cookies.get("asr_refresh")

    token = body["access_token"]
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "admin"
    assert me.json()["auth_method"] == "local"

    missing_origin = client.post("/api/v1/auth/refresh")
    assert missing_origin.status_code == 403
    assert missing_origin.json()["code"] == "E_ORIGIN_INVALID"
    foreign_origin = client.post(
        "/api/v1/auth/refresh", headers={"Origin": "https://attacker.example"}
    )
    assert foreign_origin.status_code == 403

    refreshed = client.post("/api/v1/auth/refresh", headers={"Origin": SAME_SITE_ORIGIN})
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"] != token
    refreshed_me = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
    )
    assert refreshed_me.json()["auth_method"] == "local"
    refreshed_cookie = client.cookies.get(
        "asr_refresh", domain="testserver.local", path="/api/v1/auth"
    )
    assert refreshed_cookie

    foreign_logout = client.post(
        "/api/v1/auth/logout",
        headers={
            "Authorization": f"Bearer {refreshed.json()['access_token']}",
            "Origin": "https://attacker.example",
        },
    )
    assert foreign_logout.status_code == 403

    logout = client.post(
        "/api/v1/auth/logout",
        headers={
            "Authorization": f"Bearer {refreshed.json()['access_token']}",
            "Origin": SAME_SITE_ORIGIN,
        },
    )
    assert logout.status_code == 204

    # 即使客户端回放登出前保存的 Cookie，撤销后的会话也不能继续刷新。
    client.cookies.set(
        "asr_refresh",
        refreshed_cookie,
        domain="testserver.local",
        path="/api/v1/auth",
    )
    again = client.post("/api/v1/auth/refresh", headers={"Origin": SAME_SITE_ORIGIN})
    assert again.status_code == 401

    relogin = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    audit_rows = client.get(
        "/api/v1/audit-logs",
        headers={"Authorization": f"Bearer {relogin.json()['access_token']}"},
    )
    assert audit_rows.status_code == 200
    assert any(row["action"] == "auth.logout" for row in audit_rows.json()["items"])
