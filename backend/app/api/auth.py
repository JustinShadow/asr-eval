"""认证接口：登录、当前用户、刷新、登出。

契约见 docs/m7-api-auth-design.md §2.3。
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

import jwt
from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import ApiError
from app.core.rate_limit import rate_limiter, require_limit
from app.core.security import create_access_token, decode_access_token
from app.domain import audit
from app.domain.auth import (
    authenticate,
    create_session,
    create_stream_ticket,
    revoke_session,
    rotate_session,
    session_is_active,
)
from app.domain.authorization import effective_access
from app.domain.models import AppUser, UserSession
from app.domain.permissions import permissions_for
from app.domain.service_tokens import authenticate_service_token

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    """登录请求体；未传认证方式时沿用服务配置。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    auth_mode: Literal["ldap", "local"] | None = None


def _user_payload(user: AppUser, permissions: list[str] | None = None) -> dict[str, Any]:
    """前端只判断权限，不判断角色。"""
    return {
        "id": user.username,
        "username": user.username,
        "display_name": user.display_name or user.username,
        "role": user.role,
        "permissions": permissions if permissions is not None else permissions_for(user.role),
    }


def _set_refresh_cookie(response: Response, token: str) -> None:
    """按 HTTP/HTTPS 设置 refresh cookie。"""
    settings = get_settings()
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=token,
        max_age=settings.refresh_ttl_days * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/api/v1/auth",
    )


def _clear_refresh_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path="/api/v1/auth",
    )


def _client_ip(request: Request) -> str | None:
    """开发阶段直接取连接地址；接入反向代理后再读 X-Forwarded-For。"""
    return request.client.host if request.client else None


def _normalize_origin(value: str | None) -> str | None:
    """规范化站点源并拒绝携带路径、凭据或查询串的值。"""
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if (
            port is not None
            and not (parsed.scheme.lower() == "http" and port == 80)
            and not (parsed.scheme.lower() == "https" and port == 443)
        ):
            host = f"{host}:{port}"
        return f"{parsed.scheme.lower()}://{host}"
    except ValueError:
        return None


def _validate_auth_origin(request: Request) -> None:
    """refresh/logout 仅接受同源请求；开发环境额外允许本机 Vite 源（M7 §2.2）。"""
    settings = get_settings()
    actual_origin = _normalize_origin(request.headers.get("origin"))
    expected_origin = _normalize_origin(settings.app_origin) or _normalize_origin(
        f"{request.url.scheme}://{request.url.netloc}"
    )
    allowed_origins = {expected_origin} if expected_origin else set()
    if settings.app_env == "dev" and settings.app_origin is None:
        allowed_origins.update({"http://localhost:5173", "http://127.0.0.1:5173"})
    if actual_origin is None or actual_origin not in allowed_origins:
        raise ApiError("E_ORIGIN_INVALID", "请求来源不受信任", status_code=403)


def current_claims(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """解析 Bearer token 并校验会话有效性；失败统一 401。

    为什么查库：JWT 本身无法表达"已登出/已撤销"，
    access token 撤销必须立即生效（M7 §2.2），所以校验 sid 对应会话状态。
    """
    if credentials is None:
        raise ApiError("E_TOKEN_INVALID", "未登录", status_code=401)
    if credentials.credentials.startswith("ase_"):
        claims = authenticate_service_token(db, credentials.credentials)
        require_limit(rate_limiter.consume(f"user:{claims['sub']}", 600, 60))
        return claims
    try:
        claims = decode_access_token(credentials.credentials)
    except jwt.ExpiredSignatureError as exc:
        raise ApiError("E_TOKEN_EXPIRED", "登录已过期", status_code=401) from exc
    except jwt.PyJWTError as exc:
        raise ApiError("E_TOKEN_INVALID", "无效的登录凭证", status_code=401) from exc
    if not session_is_active(db, claims.get("sid")):
        raise ApiError("E_TOKEN_INVALID", "会话已失效，请重新登录", status_code=401)
    require_limit(rate_limiter.consume(f"user:{claims['sub']}", 600, 60))
    return claims


# SSE 路由允许一次性 ticket 替代 Bearer，但仍是受保护的用户资源路由。
current_claims.__dict__["requires_authentication"] = True


@router.post("/login")
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """按请求或服务默认认证方式登录，返回 access token 并设置 refresh cookie。

    登录成败都落审计（M7 §2.1）；成功审计与建会话同事务提交。
    """
    username_key = body.username.strip().casefold()
    client_ip = _client_ip(request) or "unknown"
    require_limit(
        rate_limiter.consume_many(
            [
                (f"login-ip:{client_ip}", 5, 60),
                (f"login-user:{username_key}", 10, 60),
            ]
        ),
        message="登录尝试过于频繁",
    )
    try:
        user = authenticate(
            db,
            body.username,
            body.password,
            auth_mode=body.auth_mode,
        )
    except ApiError:
        audit.record(
            db,
            "auth.login_failed",
            actor=body.username,
            ip=_client_ip(request),
        )
        db.commit()
        raise
    audit.record(db, "auth.login", actor=user.username, ip=_client_ip(request))
    access = effective_access(db, user)
    raw_refresh, session = create_session(
        db,
        user,
        user_agent=request.headers.get("User-Agent"),
        ip=_client_ip(request),
    )
    token, expires_in = create_access_token(
        username=user.username,
        role=user.role,
        auth_method=session.auth_method,
        permissions=access.permissions,
        session_id=str(session.id),
        role_permissions=access.role_permissions,
        grants=access.grants,
        ldap_groups=user.ldap_groups,
    )
    _set_refresh_cookie(response, raw_refresh)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "user": _user_payload(user, access.permissions),
    }


@router.get("/me")
def me(
    claims: dict[str, Any] = Depends(current_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """返回当前 token 的用户与权限。"""
    user = db.get(AppUser, str(claims.get("sub", "")))
    permissions = claims.get("perms", [])
    ldap_groups = claims.get("ldap_groups", [])
    auth_method = "service_token" if user is None else claims.get("auth_method")
    if user is not None:
        access = effective_access(db, user)
        permissions = access.permissions
        ldap_groups = user.ldap_groups
        if auth_method not in {"ldap", "local", "break_glass"}:
            session = db.get(UserSession, str(claims.get("sid", "")))
            auth_method = session.auth_method if session is not None else "local"
    return {
        "user": {
            "username": claims["sub"],
            "display_name": claims.get("name", claims["sub"]),
            "role": claims["role"],
        },
        "permissions": permissions,
        "ldap_groups": ldap_groups,
        "auth_method": auth_method,
    }


@router.get("/sessions")
def list_sessions(
    claims: dict[str, Any] = Depends(current_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """列出当前用户未过期且未撤销的会话，不返回 refresh 凭证。"""
    now = datetime.now(UTC)
    rows = db.scalars(
        select(UserSession)
        .where(
            UserSession.username == str(claims.get("sub", "")),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
        .order_by(UserSession.created_at.desc())
    ).all()
    return {
        "items": [
            {
                "id": str(row.id),
                "user_agent": row.user_agent,
                "ip": row.ip,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
                "expires_at": row.expires_at.isoformat(),
                "current": str(row.id) == str(claims.get("sid", "")),
            }
            for row in rows
        ]
    }


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(
    session_id: uuid.UUID,
    response: Response,
    claims: dict[str, Any] = Depends(current_claims),
    db: Session = Depends(get_db),
) -> None:
    """撤销当前用户指定会话；重复撤销保持幂等。"""
    session = db.get(UserSession, session_id)
    username = str(claims.get("sub", ""))
    if session is None or session.username != username:
        raise ApiError("E_NOT_FOUND", "会话不存在", status_code=404)
    if session.revoked_at is None:
        session.revoked_at = datetime.now(UTC)
        audit.record(
            db,
            "auth.session.revoked",
            actor=username,
            resource=f"session:{session.id}",
            ip=session.ip,
        )
        db.commit()
    if str(session.id) == str(claims.get("sid", "")):
        _clear_refresh_cookie(response)


@router.post("/refresh")
def refresh(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """轮换 refresh token 并签发新的 access token。"""
    _validate_auth_origin(request)
    settings = get_settings()
    raw = request.cookies.get(settings.refresh_cookie_name)
    if not raw:
        raise ApiError("E_REFRESH_INVALID", "登录已过期，请重新登录", status_code=401)

    new_raw, session = rotate_session(db, raw)
    user = db.get(AppUser, session.username)
    if user is None or not user.active:
        raise ApiError("E_AUTH_INVALID", "账号不可用", status_code=401)

    access = effective_access(db, user)
    token, expires_in = create_access_token(
        username=user.username,
        role=user.role,
        auth_method=session.auth_method,
        permissions=access.permissions,
        session_id=str(session.id),
        role_permissions=access.role_permissions,
        grants=access.grants,
        ldap_groups=user.ldap_groups,
    )
    _set_refresh_cookie(response, new_raw)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "user": _user_payload(user, access.permissions),
    }


@router.post("/stream-ticket")
def stream_ticket(
    claims: dict[str, Any] = Depends(current_claims),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """签发一次性 SSE stream ticket（M7 §2.4 / D-M7-5）。

    EventSource 不能设 Authorization 头；ticket 60s 有效、用一次即删，
    只授予读权限。
    """
    ticket, expires_in = create_stream_ticket(db, str(claims.get("sub", "")))
    return {"ticket": ticket, "expires_in": expires_in}


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    claims: dict[str, Any] = Depends(current_claims),
    db: Session = Depends(get_db),
) -> None:
    """撤销当前会话并清除 cookie。"""
    _validate_auth_origin(request)
    revoke_session(db, claims.get("sid", ""))
    _clear_refresh_cookie(response)
