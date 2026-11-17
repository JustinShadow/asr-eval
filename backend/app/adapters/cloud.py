"""OpenAI-compatible cloud endpoint adapter (M2 §5.2)."""

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class CloudProbeOutcome:
    """云端模型列表 Key 校验结果，不保存模型响应内容。"""

    ok: bool
    model_count: int = 0
    error: str | None = None


async def probe_cloud_models(host: str, port: int, api_key: str) -> CloudProbeOutcome:
    """通过 HTTPS GET /v1/models 校验 Bearer Key，失败时不返回响应正文。"""
    safe_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    url = f"https://{safe_host}:{port}/v1/models"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        if response.status_code in {401, 403}:
            return CloudProbeOutcome(False, error="云端 API Key 无效或无权读取模型列表")
        response.raise_for_status()
        payload = response.json()
        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            return CloudProbeOutcome(False, error="/v1/models 响应缺少 data 数组")
        return CloudProbeOutcome(True, model_count=len(models))
    except httpx.TimeoutException:
        return CloudProbeOutcome(False, error="云端 /v1/models 请求超时")
    except httpx.HTTPStatusError as exc:
        return CloudProbeOutcome(
            False, error=f"云端 /v1/models 返回 HTTP {exc.response.status_code}"
        )
    except (httpx.HTTPError, ValueError):
        return CloudProbeOutcome(False, error="云端 /v1/models 请求或响应无效")
