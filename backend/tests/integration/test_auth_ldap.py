"""LDAP 登录、角色映射与 break-glass 行为集成测试。"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.ldap import LDAPBackendUnavailable, LDAPIdentity, LDAPInvalidCredentials
from app.core.security import hash_password
from app.domain import auth as auth_domain

pytestmark = pytest.mark.integration


def test_ldap_login_creates_user_and_maps_highest_role(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "ldap")
    monkeypatch.setattr(
        settings,
        "ldap_role_groups",
        {"tester": ["voice-testers"], "engineer": ["platform-engineering"]},
    )
    monkeypatch.setattr(
        auth_domain,
        "authenticate_ldap",
        lambda _username, _password: LDAPIdentity(
            dn="uid=ldap-user,ou=people,dc=example,dc=org",
            display_name="LDAP 用户",
            email="ldap-user@example.org",
            groups=["CN=voice-testers,OU=Groups", "platform-engineering"],
        ),
    )

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "ldap-user", "password": "directory-password"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["role"] == "engineer"
    me = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {response.json()['access_token']}"},
    )
    assert me.json()["auth_method"] == "ldap"
    assert me.json()["ldap_groups"] == [
        "CN=voice-testers,OU=Groups",
        "platform-engineering",
    ]


def test_break_glass_only_works_when_ldap_backend_is_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "ldap")
    monkeypatch.setattr(settings, "auth_break_glass", True)
    monkeypatch.setattr(settings, "local_admin_user", "rescue-admin")
    monkeypatch.setattr(settings, "local_admin_hash", hash_password("rescue-password"))

    def unavailable(_username: str, _password: str) -> LDAPIdentity:
        raise LDAPBackendUnavailable("测试模拟目录服务不可用")

    monkeypatch.setattr(auth_domain, "authenticate_ldap", unavailable)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "rescue-admin", "password": "rescue-password"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["role"] == "admin"
    me = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {response.json()['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["auth_method"] == "break_glass"
    refreshed = client.post("/api/v1/auth/refresh", headers={"Origin": "http://localhost:5173"})
    assert refreshed.status_code == 200
    refreshed_me = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
    )
    assert refreshed_me.json()["auth_method"] == "break_glass"


def test_ldap_unavailable_reports_break_glass_availability(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "ldap")
    monkeypatch.setattr(settings, "auth_break_glass", False)

    def unavailable(_username: str, _password: str) -> LDAPIdentity:
        raise LDAPBackendUnavailable("测试模拟目录服务不可用")

    monkeypatch.setattr(auth_domain, "authenticate_ldap", unavailable)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "member", "password": "bad-password"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "E_AUTH_BACKEND_DOWN"
    assert response.json()["detail"] == {"break_glass_available": False}


def test_ldap_unavailable_exposes_enabled_break_glass_without_credentials(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "ldap")
    monkeypatch.setattr(settings, "auth_break_glass", True)
    monkeypatch.setattr(settings, "local_admin_user", "rescue-admin")
    monkeypatch.setattr(settings, "local_admin_hash", hash_password("rescue-password"))

    def unavailable(_username: str, _password: str) -> LDAPIdentity:
        raise LDAPBackendUnavailable("测试模拟目录服务不可用")

    monkeypatch.setattr(auth_domain, "authenticate_ldap", unavailable)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "member", "password": "member-password"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {"break_glass_available": True}
    assert "rescue-admin" not in response.text
    assert "rescue-password" not in response.text


def test_login_can_choose_local_when_service_defaults_to_ldap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """请求显式选择本地账号时，即使服务默认 LDAP 也只校验平台密码（M7 §2.1）。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "ldap")

    def unexpected(_username: str, _password: str) -> LDAPIdentity:
        raise AssertionError("选择本地账号时不得访问 LDAP")

    monkeypatch.setattr(auth_domain, "authenticate_ldap", unexpected)
    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": "admin",
            "password": "admin12345",
            "auth_mode": "local",
        },
    )

    assert response.status_code == 200
    me = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {response.json()['access_token']}"},
    )
    assert me.json()["auth_method"] == "local"


def test_login_can_choose_ldap_when_service_defaults_to_local(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """请求显式选择 LDAP 时不得自动降级为本地密码校验（M7 §2.1）。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "local")

    def unavailable(_username: str, _password: str) -> LDAPIdentity:
        raise LDAPBackendUnavailable("测试模拟目录服务不可用")

    monkeypatch.setattr(auth_domain, "authenticate_ldap", unavailable)
    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": "admin",
            "password": "admin12345",
            "auth_mode": "ldap",
        },
    )

    assert response.status_code == 503
    assert response.json()["code"] == "E_AUTH_BACKEND_DOWN"


def test_login_rejects_unknown_auth_mode(client: TestClient) -> None:
    """认证方式只接受 ldap/local，未知值走统一 422 校验错误。"""
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345", "auth_mode": "saml"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "E_VALIDATION"


def test_bad_ldap_password_does_not_fall_back_to_break_glass(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "ldap")
    monkeypatch.setattr(settings, "auth_break_glass", True)
    monkeypatch.setattr(settings, "local_admin_user", "rescue-admin")
    monkeypatch.setattr(settings, "local_admin_hash", hash_password("rescue-password"))

    def invalid(_username: str, _password: str) -> LDAPIdentity:
        raise LDAPInvalidCredentials

    monkeypatch.setattr(auth_domain, "authenticate_ldap", invalid)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "rescue-admin", "password": "wrong-password"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "E_AUTH_INVALID"
