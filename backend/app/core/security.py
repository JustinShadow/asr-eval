"""密码哈希、JWT 与 refresh token。"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from pwdlib import PasswordHash

from app.core.config import get_settings

_password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    """生成 argon2 哈希；仅本地账号使用。"""
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码；哈希格式错误时返回 False，不向调用方泄露细节。"""
    try:
        return _password_hash.verify(password, password_hash)
    except Exception:  # 不同版本的 argon2 异常类型不一致
        return False


def create_access_token(
    *,
    username: str,
    role: str,
    auth_method: str = "local",
    permissions: list[str],
    session_id: str,
    role_permissions: list[str] | None = None,
    grants: list[dict[str, object]] | None = None,
    ldap_groups: list[str] | None = None,
) -> tuple[str, int]:
    """签发 access token，返回 (token, expires_in 秒)。"""
    settings = get_settings()
    now = datetime.now(UTC)
    expires_in = settings.access_ttl_seconds
    payload: dict[str, Any] = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": username,
        "name": username,
        "role": role,
        "auth_method": auth_method,
        "perms": permissions,
        "role_perms": role_permissions if role_permissions is not None else permissions,
        "grants": grants or [],
        "ldap_groups": ldap_groups or [],
        "sid": session_id,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
    }
    token = jwt.encode(payload, settings.jwt_key, algorithm="HS256")
    return token, expires_in


def decode_access_token(token: str) -> dict[str, Any]:
    """校验并解析 access token；失败抛 jwt 异常，由路由转成 401。"""
    settings = get_settings()
    return jwt.decode(
        token,
        settings.jwt_key,
        algorithms=["HS256"],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
    )


def new_refresh_token() -> str:
    """生成不透明 refresh token（只在 Cookie 中出现）。"""
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    """refresh token 只存哈希，数据库泄露时不能直接使用。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
