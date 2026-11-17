"""共享的 FastAPI 依赖。"""

from typing import Any

from fastapi import Depends, Request

from app.api.auth import current_claims
from app.core.errors import ApiError
from app.domain.authorization import claim_allows_permission


def require(permission: str):  # type: ignore[no-untyped-def]
    """端点声明所需权限；角色不在这里判断。"""

    def dependency(
        request: Request,
        claims: dict[str, Any] = Depends(current_claims),
    ) -> dict[str, Any]:
        if not claim_allows_permission(claims, permission, path_params=request.path_params):
            raise ApiError("E_FORBIDDEN", "没有执行该操作的权限", status_code=403)
        return claims

    # 为 OpenAPI/CI 静态路由门禁保留权限元数据，避免只靠运行时权限测试发现漏标。
    dependency.__dict__["required_permission"] = permission
    return dependency
