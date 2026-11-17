"""cloud /v1/models Key 校验不回显响应体或 API Key。"""

import asyncio

import httpx

from app.adapters import cloud


def test_cloud_probe_uses_https_bearer_and_only_returns_model_count(
    monkeypatch,
) -> None:
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(
        lambda request: seen.append(request) or httpx.Response(200, json={"data": [{"id": "stt"}]})
    )
    original_client = httpx.AsyncClient

    def client_factory(*, timeout: float, follow_redirects: bool) -> httpx.AsyncClient:
        return original_client(
            transport=transport,
            timeout=timeout,
            follow_redirects=follow_redirects,
        )

    monkeypatch.setattr(cloud.httpx, "AsyncClient", client_factory)
    key = "sk-private-test-value"
    result = asyncio.run(cloud.probe_cloud_models("api.example.test", 443, key))

    assert result.ok
    assert result.model_count == 1
    assert result.error is None
    assert len(seen) == 1
    assert seen[0].url == "https://api.example.test:443/v1/models"
    assert seen[0].headers["Authorization"] == f"Bearer {key}"
    assert key not in repr(result)


def test_cloud_probe_does_not_return_auth_response_body(monkeypatch) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(401, text="invalid key: sk-private-test-value")
    )
    original_client = httpx.AsyncClient

    def client_factory(*, timeout: float, follow_redirects: bool) -> httpx.AsyncClient:
        return original_client(
            transport=transport,
            timeout=timeout,
            follow_redirects=follow_redirects,
        )

    monkeypatch.setattr(cloud.httpx, "AsyncClient", client_factory)
    result = asyncio.run(cloud.probe_cloud_models("api.example.test", 443, "sk-private-test-value"))

    assert not result.ok
    assert result.error == "云端 API Key 无效或无权读取模型列表"
    assert "sk-private-test-value" not in repr(result)
