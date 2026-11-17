"""管理员角色、grant 叠加、过期与会话撤销集成测试。"""

import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.domain.models import AppUser, Dataset

TEST_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://asr:asr@127.0.0.1:5432/asr_eval_test",
)

pytestmark = pytest.mark.integration


def _create_user_and_dataset(username: str) -> uuid.UUID:
    """为集成用例准备只读用户和可授权数据集。"""
    engine = create_engine(TEST_URL, pool_pre_ping=True)
    dataset_id = uuid.uuid4()
    try:
        with Session(engine) as db:
            db.add(
                AppUser(
                    username=username,
                    display_name="测试执行人",
                    role="tester",
                    role_source="ldap",
                    password_hash=hash_password("tester-password"),
                    active=True,
                )
            )
            db.add(
                Dataset(
                    id=dataset_id,
                    name=f"grant-{uuid.uuid4().hex[:10]}",
                    source_type="aishell1",
                )
            )
            db.commit()
    finally:
        engine.dispose()
    return dataset_id


def test_scoped_grant_revokes_old_session_and_limits_dataset_write(
    client: TestClient, tmp_path
) -> None:
    username = f"tester-{uuid.uuid4().hex[:10]}"
    dataset_id = _create_user_and_dataset(username)
    admin_login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    admin_token = admin_login.json()["access_token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    tester_login = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "tester-password"},
    )
    assert tester_login.status_code == 200
    old_tester_token = tester_login.json()["access_token"]

    grant_response = client.post(
        f"/api/v1/admin/users/{username}/grants",
        headers=admin_headers,
        json={
            "permission": "dataset:write",
            "scope": {"dataset_id": str(dataset_id)},
            "reason": "维护指定数据集",
            "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    )
    assert grant_response.status_code == 201
    assert grant_response.json()["scope"] == {"dataset_id": str(dataset_id)}
    admin_user = next(
        item
        for item in client.get("/api/v1/admin/users", headers=admin_headers).json()["items"]
        if item["username"] == username
    )
    assert admin_user["grants"][0]["id"] == grant_response.json()["id"]
    assert (
        client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {old_tester_token}"},
        ).status_code
        == 401
    )

    new_tester_login = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "tester-password"},
    )
    tester_token = new_tester_login.json()["access_token"]
    tester_headers = {"Authorization": f"Bearer {tester_token}"}
    me = client.get("/api/v1/auth/me", headers=tester_headers)
    assert "dataset:write" in me.json()["permissions"]

    in_scope = client.post(
        f"/api/v1/datasets/{dataset_id}/preview",
        headers=tester_headers,
        json={"root": str(tmp_path), "limit": 1},
    )
    assert in_scope.status_code != 403

    revoked = client.delete(
        f"/api/v1/admin/users/{username}/grants/{grant_response.json()['id']}",
        headers=admin_headers,
    )
    assert revoked.status_code == 204
    expiring_grant = client.post(
        f"/api/v1/admin/users/{username}/grants",
        headers=admin_headers,
        json={
            "permission": "dataset:write",
            "scope": {"dataset_id": str(dataset_id)},
            "reason": "验证授权到期",
            "expires_at": (datetime.now(UTC) + timedelta(seconds=3)).isoformat(),
        },
    )
    assert expiring_grant.status_code == 201
    expiring_login = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "tester-password"},
    )
    expiring_headers = {"Authorization": f"Bearer {expiring_login.json()['access_token']}"}
    time.sleep(3.1)
    expired_me = client.get("/api/v1/auth/me", headers=expiring_headers)
    assert "dataset:write" not in expired_me.json()["permissions"]
    expired_action = client.post(
        f"/api/v1/datasets/{dataset_id}/preview",
        headers=expiring_headers,
        json={"root": str(tmp_path), "limit": 1},
    )
    assert expired_action.status_code == 403

    out_of_scope = client.post(
        f"/api/v1/datasets/{uuid.uuid4()}/preview",
        headers=expiring_headers,
        json={"root": str(tmp_path), "limit": 1},
    )
    assert out_of_scope.status_code == 403

    forged_scope = client.post(
        "/api/v1/datasets",
        params={"dataset_id": str(dataset_id)},
        headers=expiring_headers,
        json={"name": f"forbidden-{uuid.uuid4().hex[:8]}", "source_type": "aishell1"},
    )
    assert forged_scope.status_code == 403

    forbidden_grant = client.post(
        f"/api/v1/admin/users/{username}/grants",
        headers=admin_headers,
        json={"permission": "device:ops"},
    )
    assert forbidden_grant.status_code == 422
    assert forbidden_grant.json()["code"] == "E_VALIDATION"

    revoked_expiring_grant = client.delete(
        f"/api/v1/admin/users/{username}/grants/{expiring_grant.json()['id']}",
        headers=admin_headers,
    )
    assert revoked_expiring_grant.status_code == 204
    assert client.get("/api/v1/auth/me", headers=expiring_headers).status_code == 401


def test_admin_can_change_role_and_list_sessions(client: TestClient) -> None:
    username = f"role-{uuid.uuid4().hex[:10]}"
    _create_user_and_dataset(username)
    admin_login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    user_login = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "tester-password"},
    )
    user_token = user_login.json()["access_token"]
    user_headers = {"Authorization": f"Bearer {user_token}"}

    sessions = client.get("/api/v1/auth/sessions", headers=user_headers)
    assert sessions.status_code == 200
    assert len(sessions.json()["items"]) == 1
    session_id = sessions.json()["items"][0]["id"]
    assert sessions.json()["items"][0]["current"] is True

    changed = client.patch(
        f"/api/v1/admin/users/{username}",
        headers=admin_headers,
        json={"role": "engineer"},
    )
    assert changed.status_code == 200
    assert changed.json()["role"] == "engineer"
    assert changed.json()["role_source"] == "manual"
    assert client.get("/api/v1/auth/me", headers=user_headers).status_code == 401

    new_login = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "tester-password"},
    )
    new_headers = {"Authorization": f"Bearer {new_login.json()['access_token']}"}
    revoked = client.delete(f"/api/v1/auth/sessions/{session_id}", headers=new_headers)
    assert revoked.status_code == 204
