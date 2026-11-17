"""管理员用户、临时授权与审计查询服务（M7 §3.3/§3.4/§9.1）。"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domain import audit
from app.domain.authorization import effective_access, revoke_user_sessions
from app.domain.models import AppUser, AuditLog, Dataset, UserGrant
from app.domain.permissions import VALID_ROLES, permissions_for

GRANT_FORBIDDEN = {"device:ops", "admin:users", "admin:tokens", "admin:system"}
KNOWN_PERMISSIONS = {permission for role in VALID_ROLES for permission in permissions_for(role)}


def user_dict(db: Session, user: AppUser) -> dict[str, Any]:
    """序列化用户并提供当前有效权限预览。"""
    access = effective_access(db, user)
    now = datetime.now(UTC)
    grants = db.scalars(
        select(UserGrant)
        .where(
            UserGrant.username == user.username,
            UserGrant.revoked_at.is_(None),
            or_(UserGrant.expires_at.is_(None), UserGrant.expires_at > now),
        )
        .order_by(UserGrant.granted_at, UserGrant.id)
    ).all()
    return {
        "username": user.username,
        "display_name": user.display_name or user.username,
        "email": user.email,
        "role": user.role,
        "role_source": user.role_source,
        "active": user.active,
        "permissions": access.permissions,
        "grants": [
            {
                "id": str(grant.id),
                "permission": grant.permission,
                "scope": dict(grant.scope or {}),
                "reason": grant.reason,
                "granted_by": grant.granted_by,
                "granted_at": grant.granted_at.isoformat() if grant.granted_at else None,
                "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
            }
            for grant in grants
        ],
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


def list_users(db: Session) -> list[dict[str, Any]]:
    """列出用户及有效权限，团队规模较小时直接返回完整列表。"""
    users = db.scalars(select(AppUser).order_by(AppUser.username)).all()
    return [user_dict(db, user) for user in users]


def update_user(
    db: Session,
    username: str,
    *,
    role: str | None,
    active: bool | None,
    actor: str,
) -> AppUser:
    """手工变更角色/状态并立即撤销旧权限会话。"""
    user = db.get(AppUser, username)
    if user is None:
        raise ApiError("E_NOT_FOUND", "用户不存在", status_code=404)
    if role is None and active is None:
        raise ApiError("E_VALIDATION", "至少提供 role 或 active", status_code=422)
    if role is not None:
        if role not in VALID_ROLES:
            raise ApiError("E_VALIDATION", "未知用户角色", status_code=422)
        user.role = role
        user.role_source = "manual"
    if active is not None:
        user.active = active
    revoke_count = revoke_user_sessions(db, username)
    audit.record(
        db,
        "admin.user.updated",
        actor=actor,
        resource=f"user:{username}",
        detail={
            "role": user.role,
            "role_source": user.role_source,
            "active": user.active,
            "revoked_sessions": revoke_count,
        },
    )
    db.commit()
    db.refresh(user)
    return user


def create_grant(
    db: Session,
    username: str,
    *,
    permission: str,
    scope: dict[str, object],
    reason: str | None,
    expires_at: datetime | None,
    actor: str,
) -> UserGrant:
    """新增临时权限；禁止授予管理和设备运维权限。"""
    user = db.get(AppUser, username)
    if user is None:
        raise ApiError("E_NOT_FOUND", "用户不存在", status_code=404)
    if permission not in KNOWN_PERMISSIONS or permission in GRANT_FORBIDDEN:
        raise ApiError("E_VALIDATION", "该权限不能通过 grant 授予", status_code=422)
    if scope:
        if permission != "dataset:write" or set(scope) != {"dataset_id"}:
            raise ApiError("E_VALIDATION", "仅支持按数据集限定 dataset:write", status_code=422)
        try:
            dataset_id = uuid.UUID(str(scope["dataset_id"]))
        except (TypeError, ValueError) as exc:
            raise ApiError("E_VALIDATION", "dataset_id 必须是 UUID", status_code=422) from exc
        if db.get(Dataset, dataset_id) is None:
            raise ApiError("E_NOT_FOUND", "数据集不存在", status_code=404)
        scope = {"dataset_id": str(dataset_id)}
    now = datetime.now(UTC)
    if expires_at is not None and (expires_at.tzinfo is None or expires_at <= now):
        raise ApiError("E_VALIDATION", "授权到期时间必须是未来 UTC 时间", status_code=422)
    existing = db.scalar(
        select(UserGrant).where(
            UserGrant.username == username,
            UserGrant.permission == permission,
            UserGrant.scope == scope,
            UserGrant.revoked_at.is_(None),
            (UserGrant.expires_at.is_(None)) | (UserGrant.expires_at > now),
        )
    )
    if existing is not None:
        raise ApiError("E_CONFLICT", "相同授权仍然有效", status_code=409)
    grant = UserGrant(
        username=username,
        permission=permission,
        scope=scope,
        reason=reason,
        granted_by=actor,
        expires_at=expires_at,
    )
    db.add(grant)
    revoke_count = revoke_user_sessions(db, username)
    audit.record(
        db,
        "admin.user.grant_created",
        actor=actor,
        resource=f"user:{username}",
        detail={
            "permission": permission,
            "scope": scope,
            "reason": reason,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "revoked_sessions": revoke_count,
        },
    )
    db.commit()
    db.refresh(grant)
    return grant


def revoke_grant(db: Session, username: str, grant_id: uuid.UUID, *, actor: str) -> None:
    """撤销授权并立即撤销该用户所有会话。"""
    grant = db.get(UserGrant, grant_id)
    if grant is None or grant.username != username:
        raise ApiError("E_NOT_FOUND", "授权记录不存在", status_code=404)
    if grant.revoked_at is None:
        grant.revoked_at = datetime.now(UTC)
        revoke_count = revoke_user_sessions(db, username)
        audit.record(
            db,
            "admin.user.grant_revoked",
            actor=actor,
            resource=f"user:{username}",
            detail={
                "grant_id": str(grant.id),
                "permission": grant.permission,
                "revoked_sessions": revoke_count,
            },
        )
    db.commit()


def list_audit_logs(
    db: Session, *, limit: int = 50, before_id: int | None = None
) -> dict[str, Any]:
    """按自增 ID 游标倒序列出审计记录。"""
    statement = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit + 1)
    if before_id is not None:
        statement = statement.where(AuditLog.id < before_id)
    rows = db.scalars(statement).all()
    has_more = len(rows) > limit
    visible = rows[:limit]
    return {
        "items": [
            {
                "id": row.id,
                "at": row.created_at.isoformat() if row.created_at else None,
                "actor": row.actor,
                "action": row.action,
                "target": row.resource,
                "detail": row.detail,
                "ip": row.ip,
            }
            for row in visible
        ],
        "next_cursor": visible[-1].id if has_more and visible else None,
        "has_more": has_more,
    }
