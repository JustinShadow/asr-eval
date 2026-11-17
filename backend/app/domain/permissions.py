"""角色与权限。

权限字符串与 docs/m7-api-auth-design.md §3.2 一致；
端点只声明权限，不判断角色。
"""

from typing import Final

READ_PERMISSIONS: Final[tuple[str, ...]] = (
    "meta:read",
    "dataset:read",
    "device:read",
    "benchmark:read",
    "metric_profile:read",
    "run:read",
    "evaluation:read",
    "report:read",
    "speaker:read",
)

ROLE_PERMISSIONS: Final[dict[str, frozenset[str]]] = {
    "viewer": frozenset(READ_PERMISSIONS),
    "tester": frozenset(
        {
            *READ_PERMISSIONS,
            "run:create",
            "run:cancel",
            "evaluation:create",
            "evaluation:cancel",
            "report:create",
            "report:delete",
        }
    ),
    "engineer": frozenset(
        {
            *READ_PERMISSIONS,
            "run:create",
            "run:cancel",
            "evaluation:create",
            "evaluation:cancel",
            "report:create",
            "report:delete",
            "dataset:write",
            "device:write",
            "benchmark:write",
            "metric_profile:write",
            "speaker:write",
        }
    ),
    "admin": frozenset(
        {
            *READ_PERMISSIONS,
            "run:create",
            "run:cancel",
            "evaluation:create",
            "evaluation:cancel",
            "report:create",
            "report:delete",
            "dataset:write",
            "dataset:delete",
            "device:write",
            "device:delete",
            "device:ops",
            "run:delete",
            "benchmark:write",
            "benchmark:delete",
            "evaluation:delete",
            "metric_profile:delete",
            "metric_profile:write",
            "speaker:write",
            "audit:read",
            "admin:users",
            "admin:tokens",
            "admin:system",
        }
    ),
}

VALID_ROLES: Final[frozenset[str]] = frozenset(ROLE_PERMISSIONS)


def permissions_for(role: str) -> list[str]:
    """返回角色对应的权限列表；未知角色按只读处理。"""
    return sorted(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["viewer"]))
