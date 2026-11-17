"""M7 路由权限声明与角色矩阵的静态门禁。"""

from fastapi.routing import APIRoute

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.benchmarks import router as benchmarks_router
from app.api.compare import router as compare_router
from app.api.datasets import router as datasets_router
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
from app.domain.permissions import ROLE_PERMISSIONS, permissions_for

ROUTERS = (
    admin_router,
    auth_router,
    benchmarks_router,
    compare_router,
    datasets_router,
    devices_router,
    deployment_router,
    evaluations_router,
    gates_router,
    health_router,
    jobs_router,
    meta_router,
    overview_router,
    presence_router,
    user_events_router,
    reports_router,
    runs_router,
    speakers_router,
    system_router,
)

PUBLIC_ROUTES = {
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/refresh"),
    ("GET", "/healthz"),
    ("GET", "/readyz"),
    ("GET", "/version"),
}
MAIN_PUBLIC_ROUTES = PUBLIC_ROUTES | {
    ("GET", "/api/openapi.json"),
    ("GET", "/api/docs"),
}

AUTH_ONLY_ROUTES = {
    ("DELETE", "/api/v1/auth/sessions/{session_id}"),
    ("POST", "/api/v1/auth/stream-ticket"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/jobs/{job_id}/cancel"),
    ("POST", "/api/v1/presence"),
    ("DELETE", "/api/v1/presence"),
}

WRITE_PERMISSION_ROLES = {
    "run:create": {"tester", "engineer", "admin"},
    "run:cancel": {"tester", "engineer", "admin"},
    "evaluation:create": {"tester", "engineer", "admin"},
    "evaluation:cancel": {"tester", "engineer", "admin"},
    "report:create": {"tester", "engineer", "admin"},
    "report:delete": {"tester", "engineer", "admin"},
    "dataset:write": {"engineer", "admin"},
    "dataset:delete": {"admin"},
    "run:delete": {"admin"},
    "benchmark:delete": {"admin"},
    "evaluation:delete": {"admin"},
    "metric_profile:delete": {"admin"},
    "device:write": {"engineer", "admin"},
    "device:delete": {"admin"},
    "device:ops": {"admin"},
    "benchmark:write": {"engineer", "admin"},
    "metric_profile:write": {"engineer", "admin"},
    "speaker:write": {"engineer", "admin"},
    "admin:users": {"admin"},
    "admin:tokens": {"admin"},
    "admin:system": {"admin"},
}

WRITE_ROUTE_PERMISSIONS = {
    ("POST", "/api/v1/metric-profiles"): "metric_profile:write",
    ("DELETE", "/api/v1/metric-profiles/{profile_id}"): "metric_profile:delete",
    ("PATCH", "/api/v1/metric-profiles/{profile_id}"): "metric_profile:write",
    ("POST", "/api/v1/metric-profiles/{profile_id}/versions"): "metric_profile:write",
    ("POST", "/api/v1/benchmarks"): "benchmark:write",
    ("PATCH", "/api/v1/benchmarks/{benchmark_id}"): "benchmark:write",
    ("DELETE", "/api/v1/benchmarks/{benchmark_id}"): "benchmark:delete",
    ("POST", "/api/v1/benchmarks/{benchmark_id}/archive"): "benchmark:write",
    ("POST", "/api/v1/datasets"): "dataset:write",
    ("DELETE", "/api/v1/datasets/{dataset_id}"): "dataset:delete",
    ("POST", "/api/v1/datasets/{dataset_id}/versions"): "dataset:write",
    ("POST", "/api/v1/datasets/{dataset_id}/preview"): "dataset:write",
    ("DELETE", "/api/v1/dataset-versions/{version_id}"): "dataset:delete",
    ("POST", "/api/v1/dataset-versions/{version_id}/reanalyze"): "dataset:write",
    ("POST", "/api/v1/devices"): "device:write",
    ("PATCH", "/api/v1/devices/{device_id}"): "device:write",
    ("PUT", "/api/v1/devices/{device_id}/credential"): "device:write",
    ("DELETE", "/api/v1/devices/{device_id}/credential"): "device:write",
    ("DELETE", "/api/v1/devices/{device_id}"): "device:delete",
    ("POST", "/api/v1/devices/{device_id}/probe"): "device:write",
    ("POST", "/api/v1/devices/{device_id}/voiceprint-check"): "device:write",
    ("POST", "/api/v1/devices/{device_id}/ops/{action}"): "device:ops",
    ("POST", "/api/v1/runs"): "run:create",
    ("POST", "/api/v1/runs/groups"): "run:create",
    ("POST", "/api/v1/runs/preview"): "run:create",
    ("POST", "/api/v1/runs/{run_id}/cancel"): "run:cancel",
    ("POST", "/api/v1/runs/{run_id}/resume"): "run:cancel",
    ("DELETE", "/api/v1/runs/{run_id}"): "run:delete",
    ("POST", "/api/v1/runs/{run_id}/evaluations"): "evaluation:create",
    ("POST", "/api/v1/evaluations/{evaluation_id}/cancel"): "evaluation:cancel",
    ("POST", "/api/v1/evaluations/{evaluation_id}/resume"): "evaluation:create",
    ("DELETE", "/api/v1/evaluations/{evaluation_id}"): "evaluation:delete",
    ("PUT", "/api/v1/evaluations/{evaluation_id}/cases/{case_id}/tag"): "evaluation:create",
    (
        "DELETE",
        "/api/v1/evaluations/{evaluation_id}/cases/{case_id}/tag",
    ): "evaluation:create",
    ("POST", "/api/v1/reports"): "report:create",
    ("DELETE", "/api/v1/reports/{report_id}"): "report:delete",
    ("POST", "/api/v1/speakers"): "speaker:write",
    ("POST", "/api/v1/speakers/{speaker_id}/enrollments"): "speaker:write",
    ("POST", "/api/v1/speakers/{speaker_id}/extract"): "speaker:write",
    ("PATCH", "/api/v1/admin/users/{username}"): "admin:users",
    ("POST", "/api/v1/admin/users/{username}/grants"): "admin:users",
    ("DELETE", "/api/v1/admin/users/{username}/grants/{grant_id}"): "admin:users",
    ("POST", "/api/v1/admin/service-tokens"): "admin:tokens",
    ("DELETE", "/api/v1/admin/service-tokens/{token_id}"): "admin:tokens",
    ("POST", "/api/v1/gate-policies"): "admin:system",
    ("PATCH", "/api/v1/gate-policies/{policy_id}"): "admin:system",
}

READ_ROUTE_PERMISSIONS = {
    ("GET", "/api/v1/overview/summary"): "run:read",
    ("GET", "/api/v1/overview/regressions"): "run:read",
    ("GET", "/api/v1/dataset-versions/{version_id}/capabilities"): "dataset:read",
    ("GET", "/api/v1/deployments/{deployment_id}"): "device:read",
    ("GET", "/api/v1/devices/{device_id}/credential"): "device:read",
    ("POST", "/api/v1/metric-profiles/validate"): "metric_profile:read",
    ("POST", "/api/v1/benchmarks/preview"): "benchmark:read",
    ("POST", "/api/v1/compare"): "evaluation:read",
    ("GET", "/api/v1/speakers"): "speaker:read",
    ("GET", "/api/v1/speakers/{speaker_id}"): "speaker:read",
    ("GET", "/api/v1/speakers/{speaker_id}/enrollments"): "speaker:read",
    ("GET", "/api/v1/speakers/{speaker_id}/enrollments/{enrollment_id}/audio"): "speaker:read",
    ("GET", "/api/v1/speakers/{speaker_id}/vectors"): "speaker:read",
    ("GET", "/api/v1/system/status"): "admin:system",
}


def _route_dependencies(route: APIRoute) -> list[object]:
    """展开当前路由的直接依赖；所需权限依赖由 require() 直接声明。"""
    return [dependency.call for dependency in route.dependant.dependencies]


def test_api_routes_declare_permission_or_authentication() -> None:
    """所有 API 路由必须公开白名单、声明权限或显式要求登录。"""
    missing: list[str] = []
    for router in ROUTERS:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods or set():
                key = (method, route.path)
                if key in PUBLIC_ROUTES:
                    continue
                dependencies = _route_dependencies(route)
                is_protected = any(
                    getattr(dependency, "required_permission", None)
                    or getattr(dependency, "requires_authentication", False)
                    for dependency in dependencies
                ) or getattr(route.endpoint, "requires_authentication", False)
                if not is_protected:
                    missing.append(f"{method} {route.path}")

    assert not missing, "以下 API 路由缺少权限声明或登录校验：" + ", ".join(sorted(missing))


def test_mounted_application_routes_have_auth_policy() -> None:
    """应用实际挂载的所有路由必须声明权限、要求登录或进入公开白名单。"""
    from app.main import create_app

    missing: list[str] = []
    for route in create_app().routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or set():
            if (method, route.path) in MAIN_PUBLIC_ROUTES:
                continue
            dependencies = _route_dependencies(route)
            is_protected = any(
                getattr(dependency, "required_permission", None)
                or getattr(dependency, "requires_authentication", False)
                for dependency in dependencies
            ) or getattr(route.endpoint, "requires_authentication", False)
            if not is_protected:
                missing.append(f"{method} {route.path}")

    assert not missing, "以下挂载路由缺少权限声明或登录校验：" + ", ".join(sorted(missing))


def test_meta_enum_route_uses_design_permission() -> None:
    """枚举接口固定使用 M7 §5.1 声明的 meta:read 权限。"""
    route = next(
        route
        for route in meta_router.routes
        if isinstance(route, APIRoute) and route.path == "/api/v1/meta/enums"
    )
    permissions = [
        getattr(dependency.call, "required_permission", None)
        for dependency in route.dependant.dependencies
    ]
    assert "meta:read" in permissions


def test_role_matrix_covers_every_write_permission() -> None:
    """四档角色对全部写权限的授权必须与 M7 §3.2 一致。"""
    assert set(ROLE_PERMISSIONS) == {"viewer", "tester", "engineer", "admin"}
    assert set(WRITE_PERMISSION_ROLES) == set(WRITE_ROUTE_PERMISSIONS.values())
    for permission, allowed_roles in WRITE_PERMISSION_ROLES.items():
        for role in ROLE_PERMISSIONS:
            assert (permission in permissions_for(role)) is (role in allowed_roles), (
                f"{role} 对 {permission} 的授权与 M7 §3.2 不一致"
            )


def test_write_routes_match_design_permissions() -> None:
    """逐路由核对写操作权限，避免只测角色权限表却漏改端点声明。"""
    actual: dict[tuple[str, str], str] = {}
    for router in ROUTERS:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods or set():
                if (method, route.path) not in WRITE_ROUTE_PERMISSIONS:
                    continue
                permissions = [
                    getattr(dependency.call, "required_permission", None)
                    for dependency in route.dependant.dependencies
                ]
                actual[(method, route.path)] = next(
                    (permission for permission in permissions if permission), ""
                )

    assert actual == WRITE_ROUTE_PERMISSIONS


def test_read_routes_match_design_permissions() -> None:
    """关键读取端点必须声明 M7 矩阵中对应的精确权限。"""
    actual: dict[tuple[str, str], str] = {}
    for router in ROUTERS:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods or set():
                key = (method, route.path)
                if key not in READ_ROUTE_PERMISSIONS:
                    continue
                permissions = [
                    getattr(dependency.call, "required_permission", None)
                    for dependency in route.dependant.dependencies
                ]
                actual[key] = next((permission for permission in permissions if permission), "")

    assert actual == READ_ROUTE_PERMISSIONS


def test_every_mutation_route_has_an_exact_permission_or_auth_policy() -> None:
    """所有变更方法必须出现在权限矩阵、公开白名单或登录白名单中。"""
    mutation_routes = {
        (method, route.path)
        for router in ROUTERS
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or set()
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    expected = (
        set(WRITE_ROUTE_PERMISSIONS)
        | {
            route
            for route in READ_ROUTE_PERMISSIONS
            if route[0] in {"POST", "PUT", "PATCH", "DELETE"}
        }
        | {route for route in PUBLIC_ROUTES if route[0] in {"POST", "PUT", "PATCH", "DELETE"}}
        | AUTH_ONLY_ROUTES
    )
    assert mutation_routes == expected, (
        f"端点权限矩阵与路由不一致：未登记={sorted(mutation_routes - expected)}；"
        f"多余登记={sorted(expected - mutation_routes)}"
    )
