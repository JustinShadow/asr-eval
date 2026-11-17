"""用户有效权限、授权范围与会话撤销。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models import AppUser, UserGrant, UserSession
from app.domain.permissions import permissions_for


@dataclass(frozen=True)
class EffectiveAccess:
    """签发令牌和展示当前用户权限所需的数据。"""

    permissions: list[str]
    role_permissions: list[str]
    grants: list[dict[str, object]]


def effective_access(db: Session, user: AppUser) -> EffectiveAccess:
    """角色权限与未过期 grant 合并；scope 保留给端点做对象级校验。"""
    now = datetime.now(UTC)
    role_permissions = permissions_for(user.role)
    rows = db.scalars(
        select(UserGrant)
        .where(
            UserGrant.username == user.username,
            UserGrant.revoked_at.is_(None),
            (UserGrant.expires_at.is_(None)) | (UserGrant.expires_at > now),
        )
        .order_by(UserGrant.granted_at, UserGrant.id)
    ).all()
    grants: list[dict[str, object]] = []
    permissions = set(role_permissions)
    for grant in rows:
        permissions.add(grant.permission)
        grants.append(
            {
                "permission": grant.permission,
                "scope": dict(grant.scope or {}),
                "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
            }
        )
    return EffectiveAccess(
        permissions=sorted(permissions),
        role_permissions=role_permissions,
        grants=grants,
    )


def claim_allows_permission(
    claims: dict[str, Any], permission: str, *, path_params: dict[str, Any]
) -> bool:
    """检查角色权限或 grant；数据集范围只匹配 URL 路径，不读可伪造的 query 参数。"""
    role_permissions = claims.get("role_perms", claims.get("perms", []))
    if isinstance(role_permissions, list) and permission in role_permissions:
        return True

    grants = claims.get("grants", [])
    if not isinstance(grants, list):
        return False
    for grant in grants:
        if not isinstance(grant, dict) or grant.get("permission") != permission:
            continue
        expires_at = grant.get("expires_at")
        if expires_at is not None:
            if not isinstance(expires_at, str):
                continue
            try:
                expiration = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if expiration.tzinfo is None or expiration <= datetime.now(UTC):
                continue
        scope = grant.get("scope", {})
        if not isinstance(scope, dict):
            continue
        if not scope:
            return True
        dataset_id = scope.get("dataset_id")
        route_dataset_id = path_params.get("dataset_id")
        if (
            dataset_id is not None
            and route_dataset_id is not None
            and str(dataset_id) == str(route_dataset_id)
        ):
            return True
    return False


def revoke_user_sessions(db: Session, username: str) -> int:
    """立即撤销用户全部会话；提交由权限变更的调用方负责。"""
    now = datetime.now(UTC)
    sessions = db.scalars(
        select(UserSession).where(
            UserSession.username == username,
            UserSession.revoked_at.is_(None),
        )
    ).all()
    for session in sessions:
        session.revoked_at = now
    return len(sessions)
