"""系统状态接口集成测试。"""

from fastapi.testclient import TestClient

from app.core.config import Settings


def test_admin_system_status_reports_dependencies_and_queues(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    """管理员状态页读取依赖、版本、队列；未配置 NFS 明确显示而不伪报正常。"""
    settings = Settings(data_dir=tmp_path, import_allowed_roots=[str(tmp_path)])
    monkeypatch.setattr("app.api.system.get_settings", lambda: settings)
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = client.get("/api/v1/system/status", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["dependencies"]["database"]["status"] == "ok"
    assert payload["dependencies"]["local_storage"]["status"] == "ok"
    assert payload["dependencies"]["nfs"]["status"] == "not_configured"
    assert payload["version"]["app"] == settings.app_version
    assert payload["version"]["schema"] == "unknown"
    assert set(payload["queues"]) == {"jobs", "runs", "evaluations"}


def test_admin_system_status_checks_only_explicit_nfs_root(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    """M9 §3.1：本地导入白名单不能被误报为 NFS 挂载。"""
    settings = Settings(data_dir=tmp_path, import_allowed_roots=[str(tmp_path)])
    monkeypatch.setattr("app.api.system.get_settings", lambda: settings)
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = client.get("/api/v1/system/status", headers=headers)

    assert response.status_code == 200
    assert response.json()["dependencies"]["nfs"]["status"] == "not_configured"

    settings.nfs_root = tmp_path / "nfs"
    settings.nfs_root.mkdir()
    mounted = client.get("/api/v1/system/status", headers=headers)
    assert mounted.json()["dependencies"]["nfs"] == {
        "status": "ok",
        "configured_roots": 1,
        "available_roots": 1,
    }
