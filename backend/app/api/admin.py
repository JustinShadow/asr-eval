"""用户、按人授权与审计管理接口。"""

import base64
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require
from app.core.db import get_db
from app.core.errors import ApiError
from app.domain.admin import (
    create_grant,
    list_audit_logs,
    list_users,
    revoke_grant,
    update_user,
    user_dict,
)
from app.domain.service_tokens import (
    create_service_token,
    list_service_tokens,
    revoke_service_token,
)

router = APIRouter(prefix="/api/v1", tags=["admin"])


class UserPatch(BaseModel):
    """管理员修改用户角色或启用状态。"""

    role: str | None = Field(default=None, max_length=16)
    active: bool | None = None


class GrantCreate(BaseModel):
    """按人授予临时权限。"""

    permission: str = Field(min_length=1, max_length=64)
    scope: dict[str, object] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=512)
    expires_at: datetime | None = None


class ServiceTokenCreate(BaseModel):
    """CI service token 的创建参数。"""

    name: str = Field(min_length=1, max_length=128)
    scopes: list[str] = Field(min_length=1)
    expires_at: datetime


def _encode_audit_cursor(value: int) -> str:
    """将审计自增游标转成不透明字符串。"""
    return base64.urlsafe_b64encode(str(value).encode()).decode().rstrip("=")


def _decode_audit_cursor(value: str) -> int:
    """校验审计游标；非法值统一返回 400。"""
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        cursor = int(decoded)
        if cursor < 1:
            raise ValueError("游标必须为正数")
        return cursor
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError("E_VALIDATION", "非法游标", status_code=400) from exc


@router.get("/admin/users")
def get_users(
    _: dict[str, Any] = Depends(require("admin:users")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """列出用户、角色来源、授权与有效权限预览。"""
    return {"items": list_users(db)}


@router.patch("/admin/users/{username}")
def patch_user(
    username: str,
    body: UserPatch,
    claims: dict[str, Any] = Depends(require("admin:users")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """手工更改角色/启用状态，并撤销用户现有会话。"""
    user = update_user(
        db,
        username,
        role=body.role,
        active=body.active,
        actor=str(claims.get("sub", "")),
    )
    return user_dict(db, user)


@router.post("/admin/users/{username}/grants", status_code=201)
def post_grant(
    username: str,
    body: GrantCreate,
    claims: dict[str, Any] = Depends(require("admin:users")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """新增按人授权；授予权限和撤销旧会话在同一事务提交。"""
    grant = create_grant(
        db,
        username,
        permission=body.permission,
        scope=body.scope,
        reason=body.reason,
        expires_at=body.expires_at,
        actor=str(claims.get("sub", "")),
    )
    return {
        "id": str(grant.id),
        "username": grant.username,
        "permission": grant.permission,
        "scope": grant.scope,
        "reason": grant.reason,
        "granted_by": grant.granted_by,
        "granted_at": grant.granted_at.isoformat() if grant.granted_at else None,
        "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
    }


@router.delete("/admin/users/{username}/grants/{grant_id}", status_code=204)
def delete_grant(
    username: str,
    grant_id: uuid.UUID,
    claims: dict[str, Any] = Depends(require("admin:users")),
    db: Session = Depends(get_db),
) -> None:
    """撤销按人授权并使旧权限令牌立即失效。"""
    revoke_grant(db, username, grant_id, actor=str(claims.get("sub", "")))


@router.get("/audit-logs")
def get_audit_logs(
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    _: dict[str, Any] = Depends(require("audit:read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """使用不透明 ID 游标读取审计日志。"""
    result = list_audit_logs(
        db,
        limit=limit,
        before_id=_decode_audit_cursor(cursor) if cursor else None,
    )
    next_cursor = result["next_cursor"]
    result["next_cursor"] = _encode_audit_cursor(next_cursor) if next_cursor is not None else None
    return result


@router.post("/admin/service-tokens", status_code=201)
def post_service_token(
    body: ServiceTokenCreate,
    claims: dict[str, Any] = Depends(require("admin:tokens")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """创建 service token；明文仅在本次响应返回。"""
    raw_token, row = create_service_token(
        db,
        name=body.name,
        scopes=body.scopes,
        expires_at=body.expires_at,
        actor=str(claims.get("sub", "")),
    )
    return {
        "id": str(row.id),
        "name": row.name,
        "token": raw_token,
        "scopes": row.scopes,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": row.expires_at.isoformat(),
    }


@router.get("/admin/service-tokens")
def get_service_tokens(
    _: dict[str, Any] = Depends(require("admin:tokens")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """列出 service token 元数据，不返回密钥。"""
    return {"items": list_service_tokens(db)}


@router.delete("/admin/service-tokens/{token_id}", status_code=204)
def delete_service_token(
    token_id: uuid.UUID,
    claims: dict[str, Any] = Depends(require("admin:tokens")),
    db: Session = Depends(get_db),
) -> None:
    """立即撤销 service token 并记录审计。"""
    revoke_service_token(db, token_id, actor=str(claims.get("sub", "")))
