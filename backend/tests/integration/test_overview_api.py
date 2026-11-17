"""M8 总览聚合接口集成验收。"""

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_overview_summary_and_regressions_require_run_read(client: TestClient) -> None:
    """总览指标返回显式样本数；匿名请求不能读取评测聚合。"""
    anonymous = client.get("/api/v1/overview/summary")
    assert anonymous.status_code == 401

    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    summary = client.get("/api/v1/overview/summary", headers=headers)
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["days"] == 7
    assert payload["runs"] >= 0
    assert payload["failed_runs"] >= 0
    assert payload["running_runs"] >= 0
    assert payload["cer_delta_n"] >= 0
    assert payload["average_cer_delta_pp"] is None or isinstance(
        payload["average_cer_delta_pp"], (int, float)
    )
    assert payload["as_of"]

    regressions = client.get("/api/v1/overview/regressions?days=7", headers=headers)
    assert regressions.status_code == 200
    assert regressions.json()["days"] == 7
    assert isinstance(regressions.json()["items"], list)

    invalid_window = client.get("/api/v1/overview/regressions?days=31", headers=headers)
    assert invalid_window.status_code == 422
