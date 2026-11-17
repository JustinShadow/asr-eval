"""FastAPI 应用入口。

开发阶段直接运行：
    uv run uvicorn app.main:app --reload --port 8080
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, Response

from app.adapters.ssh_tunnel import close_all_tunnels
from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.benchmarks import router as benchmarks_router
from app.api.compare import router as compare_router
from app.api.datasets import router as datasets_router
from app.api.deps import require
from app.api.devices import deployment_router
from app.api.devices import router as devices_router
from app.api.evaluations import router as evaluations_router
from app.api.gates import router as gates_router
from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.meta import router as meta_router
from app.api.overview import router as overview_router
from app.api.presence import router as presence_router
from app.api.reports import router as reports_router
from app.api.runs import router as runs_router
from app.api.speakers import router as speakers_router
from app.api.system import router as system_router
from app.api.user_events import router as user_events_router
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.errors import (
    ApiError,
    api_error_handler,
    internal_error_handler,
    validation_error_handler,
)
from app.domain.evaluation_worker import get_evaluation_worker
from app.domain.run_worker import get_run_worker
from app.domain.user_events import UserEventListener


async def request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """为每个请求生成/透传 X-Request-Id（M7 §4.7），响应头必须回显。"""
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path != "/api/docs":
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'",
        )
    if get_settings().cookie_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


def create_app() -> FastAPI:
    """构建应用；工厂形式便于测试注入。"""
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """启动任务恢复与 PostgreSQL 用户事件监听，退出时排空后台资源。"""
        worker = get_evaluation_worker()
        run_worker = get_run_worker()
        event_listener: UserEventListener = app.state.user_event_listener
        event_listener.start()
        try:
            worker.start(app.state.evaluation_session_factory, recover=True)
            run_worker.start(app.state.evaluation_session_factory, recover=True)
            yield
        finally:
            run_worker.shutdown()
            event_listener.stop()
            worker.shutdown()
            close_all_tunnels()

    app = FastAPI(
        title="ASR 评测平台",
        version=settings.app_version,
        lifespan=lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.state.evaluation_session_factory = get_session_factory()
    app.state.evaluation_engine = get_engine()
    app.state.user_event_listener = UserEventListener(app.state.evaluation_engine)
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    # 校验错误与未捕获异常都必须落到统一错误体（M7 §4.2）
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, internal_error_handler)
    if settings.app_env == "dev":
        # 仅本机 Vite 开发源跨域；staging/prod 使用同源部署。
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "If-Match",
                "If-None-Match",
                "Last-Event-ID",
                "X-Request-Id",
            ],
            expose_headers=["ETag", "Idempotent-Replay", "Location", "Retry-After", "X-Request-Id"],
        )
    app.middleware("http")(request_id_middleware)

    if settings.app_env == "prod":

        @app.get("/api/openapi.json", include_in_schema=False)
        def protected_openapi_schema(
            _: dict[str, Any] = Depends(require("admin:system")),
        ) -> dict[str, Any]:
            """生产环境仅向系统管理员提供 OpenAPI 契约。"""
            return app.openapi()

    else:

        @app.get("/api/openapi.json", include_in_schema=False)
        def openapi_schema() -> dict[str, Any]:
            """开发环境提供 OpenAPI 3.1 契约。"""
            return app.openapi()

        @app.get("/api/docs", include_in_schema=False)
        def api_docs() -> HTMLResponse:
            """开发环境提供 Swagger UI。"""
            return get_swagger_ui_html(
                openapi_url="/api/openapi.json",
                title=f"{app.title} API",
            )

    # 系统端点不带 /api 前缀，方便容器与反向代理探活
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(jobs_router)
    app.include_router(meta_router)
    app.include_router(overview_router)
    app.include_router(devices_router)
    app.include_router(deployment_router)
    app.include_router(speakers_router)
    app.include_router(system_router)
    app.include_router(presence_router)
    app.include_router(datasets_router)
    app.include_router(benchmarks_router)
    app.include_router(compare_router)
    app.include_router(evaluations_router)
    app.include_router(gates_router)
    app.include_router(runs_router)
    app.include_router(reports_router)
    app.include_router(user_events_router)

    return app


app = create_app()
