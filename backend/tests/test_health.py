"""健康检查的最小契约测试。"""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import app.api.health as health_api
from app.main import create_app

client = TestClient(create_app())


def test_healthz() -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_version_contains_python_and_schema_version(monkeypatch) -> None:
    connection = MagicMock()
    connection.execute.return_value.scalar.return_value = "0038_run_resource_sample"
    engine = MagicMock()
    engine.connect.return_value = nullcontext(connection)
    monkeypatch.setattr(health_api, "get_engine", lambda: engine)

    resp = client.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"]
    assert body["python"].startswith("3.13")
    assert body["schema_version"] == "0038_run_resource_sample"
    assert body["schema_head"] == "0038_run_resource_sample"


def test_readyz_reports_disk_and_optional_nfs(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        health_api,
        "get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path, database_url="configured", nfs_root=None),
    )
    monkeypatch.setattr(health_api, "_database_status", lambda: "ok")
    monkeypatch.setattr(
        health_api,
        "_disk_status",
        lambda _path: {"status": "ok", "free_bytes": 1000, "used_percent": 20.0},
    )

    resp = client.get("/readyz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["nfs"] == {"status": "not_configured"}
    assert body["checks"]["storage"]["used_percent"] == 20.0
