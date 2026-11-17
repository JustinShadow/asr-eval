"""统一错误响应。

响应体固定为 {code, message, detail, trace_id}，见 M7 §4.3；
trace_id 优先取中间件写入的 request.state.request_id（与响应头 X-Request-Id 一致）。
"""

import uuid
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """业务异常；由全局处理器转成统一 JSON。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        detail: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail or {}
        self.headers = headers or {}


def _trace_id(request: Request) -> str:
    """请求 ID：中间件已生成则复用，保证错误体与响应头可对齐日志。"""
    return getattr(request.state, "request_id", None) or uuid.uuid4().hex


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """把 ApiError 序列化为统一错误体。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": exc.detail,
            "trace_id": _trace_id(request),
        },
        headers=exc.headers,
    )


async def validation_error_handler(request: Request, exc: Any) -> JSONResponse:
    """请求体/参数校验失败也走统一错误体（M7 §4.2），不能裸 422。"""
    raw_errors: list[dict[str, Any]] = list(getattr(exc, "errors", lambda: [])())
    errors: list[dict[str, Any]] = raw_errors
    return JSONResponse(
        status_code=422,
        content={
            "code": "E_VALIDATION",
            "message": "请求参数不合法",
            "detail": {"errors": errors},
            "trace_id": _trace_id(request),
        },
    )


async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """未捕获异常兜底为 500 统一错误体；trace_id 用于日志聚合。"""
    import logging

    logging.getLogger(__name__).exception("unhandled error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "code": "E_INTERNAL",
            "message": "服务内部错误",
            "detail": {},
            "trace_id": _trace_id(request),
        },
    )
