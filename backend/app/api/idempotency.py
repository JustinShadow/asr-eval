"""幂等响应头适配（M7 §4.5）。"""

from fastapi import Response

from app.domain.idempotency import IdempotentResult


def apply_idempotent_response(response: Response, result: IdempotentResult) -> None:
    """回传首次状态码与 Location；重放时标注 Idempotent-Replay。"""
    response.status_code = result.status_code
    for name, value in result.headers.items():
        response.headers[name] = value
    if result.replayed:
        response.headers["Idempotent-Replay"] = "true"
