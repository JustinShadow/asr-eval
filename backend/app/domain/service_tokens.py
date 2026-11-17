"""受限 CI service token 的签发、校验与撤销（M7 §2.5）。"""

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domain import audit
from app.domain.models import ServiceToken

ALLOWED_SCOPES = frozenset(
    {"run:create", "run:read", "evaluation:read", "report:create", "report:read"}
)


def create_service_token(
    db: Session,
    *,
    name: str,
    scopes: list[str],
    expires_at: datetime,
    actor: str,
) -> tuple[str, ServiceToken]:
    """创建一次性明文 token；数据库只写入 secret 的 SHA-256。"""
    if not scopes or set(scopes) - ALLOWED_SCOPES:
        raise ApiError("E_VALIDATION", "service token scopes 不合法", status_code=422)
    if expires_at.tzinfo is None or expires_at <= datetime.now(UTC):
        raise ApiError("E_VALIDATION", "service token 到期时间必须是未来 UTC 时间", status_code=422)
    token_id = uuid.uuid4()
    secret = secrets.token_urlsafe(32)
    raw_token = f"ase_{token_id.hex}_{secret}"
    row = ServiceToken(
        id=token_id,
        name=name,
        secret_hash=hashlib.sha256(secret.encode()).hexdigest(),
        scopes=sorted(set(scopes)),
        created_by=actor,
        expires_at=expires_at,
    )
    db.add(row)
    audit.record(
        db,
        "admin.service_token.created",
        actor=actor,
        resource=f"service_token:{token_id}",
        detail={"name": name, "scopes": row.scopes, "expires_at": expires_at.isoformat()},
    )
    db.commit()
    db.refresh(row)
    return raw_token, row


def list_service_tokens(db: Session) -> list[dict[str, Any]]:
    """列出 token 元数据，不包含凭证或其哈希。"""
    now = datetime.now(UTC)
    rows = db.scalars(select(ServiceToken).order_by(ServiceToken.created_at.desc())).all()
    return [
        {
            "id": str(row.id),
            "name": row.name,
            "scopes": row.scopes,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "expires_at": row.expires_at.isoformat(),
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
            "active": row.revoked_at is None and row.expires_at > now,
        }
        for row in rows
    ]


def revoke_service_token(db: Session, token_id: uuid.UUID, *, actor: str) -> None:
    """撤销 service token；重复撤销保持幂等。"""
    row = db.get(ServiceToken, token_id)
    if row is None:
        raise ApiError("E_NOT_FOUND", "service token 不存在", status_code=404)
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        audit.record(
            db,
            "admin.service_token.revoked",
            actor=actor,
            resource=f"service_token:{row.id}",
            detail={"name": row.name, "scopes": row.scopes},
        )
    db.commit()


def authenticate_service_token(db: Session, raw_token: str) -> dict[str, Any]:
    """校验 ase_<id>_<secret> 并返回 scope 权限声明。"""
    try:
        prefix, raw_id, secret = raw_token.split("_", 2)
        if prefix != "ase" or not secret:
            raise ValueError("格式不合法")
        token_id = uuid.UUID(hex=raw_id)
    except ValueError as exc:
        raise ApiError("E_TOKEN_INVALID", "无效的 service token", status_code=401) from exc
    row = db.scalar(select(ServiceToken).where(ServiceToken.id == token_id))
    digest = hashlib.sha256(secret.encode()).hexdigest()
    if (
        row is None
        or row.revoked_at is not None
        or row.expires_at <= datetime.now(UTC)
        or not hmac.compare_digest(row.secret_hash, digest)
    ):
        raise ApiError("E_TOKEN_INVALID", "无效的 service token", status_code=401)
    scopes = [scope for scope in row.scopes if scope in ALLOWED_SCOPES]
    return {
        "iss": "asr-eval",
        "aud": "asr-eval-api",
        "sub": f"service:{row.name}",
        "name": row.name,
        "role": "service",
        "perms": scopes,
        "role_perms": scopes,
        "grants": [],
        "sid": f"service:{row.id}",
        "service_token_id": str(row.id),
    }
