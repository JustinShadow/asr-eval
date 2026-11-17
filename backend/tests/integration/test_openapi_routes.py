"""OpenAPI 与开发环境 Swagger 入口集成测试。"""

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_development_openapi_and_swagger_routes(client: TestClient) -> None:
    """开发环境契约与 Swagger UI 使用设计文档约定的地址。"""
    schema = client.get("/api/openapi.json")
    assert schema.status_code == 200
    assert schema.json()["openapi"] == "3.1.0"

    docs = client.get("/api/docs")
    assert docs.status_code == 200
    assert "/api/openapi.json" in docs.text

    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
