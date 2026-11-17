"""登录、刷新、登出。

refresh 契约见 docs/m7-api-auth-design.md §2.2：
- 一次性轮换：每次刷新签发新 token，旧的立即作废；
- 复用检测：旧 token 在宽限期外再次出现 → 整个 token family 失效 + 审计；
- 30s 幂等宽限：多标签页并发刷新时，旧 token 在 30s 内再次出现返回同一个新 token。
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.credentials import credentials_fernet
from app.core.errors import ApiError
from app.core.ldap import (
    LDAPBackendUnavailable,
    LDAPInvalidCredentials,
    authenticate_ldap,
    role_from_groups,
)
from app.core.security import (
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from app.domain import audit
from app.domain.authorization import revoke_user_sessions
from app.domain.models import AppUser, RefreshGrace, UserSession

# 宽限期时长（秒）。为什么 30s：多标签页几乎同时 401 后并发刷新的窗口（M7 D-M7-18）
GRACE_SECONDS = 30
# 宽限行的保留时长；过期的旧哈希查不到即按"无效 token"处理
GRACE_RETENTION = timedelta(hours=1)


def _fernet() -> Fernet:
    """使用 M9 §3.2 的 credentials_key 加密 refresh 宽限 token。"""
    return credentials_fernet()


def _encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def _decrypt_token(token_enc: str) -> str | None:
    try:
        return _fernet().decrypt(token_enc.encode()).decode()
    except Exception:
        # 密钥轮换后旧宽限行解不开，视同无宽限
        return None


def authenticate(
    db: Session,
    username: str,
    password: str,
    *,
    auth_mode: Literal["ldap", "local"] | None = None,
) -> AppUser:
    """按请求指定或服务配置校验 LDAP 或本地账号，并统一未知用户与错密错误。"""
    settings = get_settings()
    selected_mode = auth_mode if auth_mode is not None else settings.auth_mode
    if selected_mode == "ldap":
        try:
            identity = authenticate_ldap(username, password)
        except LDAPInvalidCredentials as exc:
            raise ApiError("E_AUTH_INVALID", "用户名或密码错误", status_code=401) from exc
        except LDAPBackendUnavailable as exc:
            if (
                settings.auth_break_glass
                and username == settings.local_admin_user
                and settings.local_admin_hash
                and verify_password(password, settings.local_admin_hash)
            ):
                user = db.get(AppUser, username)
                if user is None:
                    user = AppUser(
                        username=username,
                        role="admin",
                        role_source="manual",
                        password_hash=settings.local_admin_hash,
                        active=True,
                    )
                    db.add(user)
                if not user.active:
                    raise ApiError("E_AUTH_INVALID", "用户名或密码错误", status_code=401) from exc
                user.role = "admin"
                user.role_source = "manual"
                user.__dict__["ldap_groups"] = []
                user.__dict__["auth_method"] = "break_glass"
                audit.record(db, "auth.break_glass", actor=username)
                return user
            raise ApiError(
                "E_AUTH_BACKEND_DOWN",
                "目录服务暂不可用，请稍后重试",
                status_code=503,
                detail={
                    "break_glass_available": bool(
                        settings.auth_break_glass
                        and settings.local_admin_user
                        and settings.local_admin_hash
                    )
                },
            ) from exc

        role = role_from_groups(identity.groups, settings.ldap_role_groups)
        user = db.get(AppUser, username)
        if user is None:
            user = AppUser(
                username=username,
                display_name=identity.display_name,
                email=identity.email,
                ldap_dn=identity.dn,
                ldap_groups=identity.groups,
                role=role,
                role_source="ldap",
                active=True,
            )
            db.add(user)
        else:
            if not user.active:
                raise ApiError("E_AUTH_INVALID", "用户名或密码错误", status_code=401)
            old_role = user.role
            user.display_name = identity.display_name or user.display_name
            user.email = identity.email or user.email
            user.ldap_dn = identity.dn
            user.ldap_groups = identity.groups
            if user.role_source != "manual":
                user.role = role
                user.role_source = "ldap"
                if old_role != role:
                    revoked_count = revoke_user_sessions(db, username)
                    audit.record(
                        db,
                        "auth.role_mapped",
                        actor=username,
                        resource=f"user:{username}",
                        detail={"role": role, "revoked_sessions": revoked_count},
                    )
        user.__dict__["auth_method"] = "ldap"
        return user

    user = db.get(AppUser, username)
    if (
        user is None
        or not user.active
        or not user.password_hash
        or not verify_password(password, user.password_hash)
    ):
        raise ApiError("E_AUTH_INVALID", "用户名或密码错误", status_code=401)
    user.__dict__["ldap_groups"] = []
    user.__dict__["auth_method"] = "local"
    return user


def create_session(
    db: Session,
    user: AppUser,
    *,
    user_agent: str | None,
    ip: str | None,
) -> tuple[str, UserSession]:
    """创建会话并返回明文 refresh token（只返回一次）。"""
    settings = get_settings()
    raw = new_refresh_token()
    session = UserSession(
        id=uuid.uuid4(),
        username=user.username,
        role=user.role,
        auth_method=getattr(user, "auth_method", "local"),
        refresh_hash=hash_refresh_token(raw),
        family_id=uuid.uuid4(),
        user_agent=(user_agent or "")[:512] or None,
        ip=ip,
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_ttl_days),
    )
    db.add(session)
    user.last_login_at = datetime.now(UTC)
    db.commit()
    return raw, session


def rotate_session(db: Session, raw_refresh: str) -> tuple[str, UserSession]:
    """用旧 refresh 换新 refresh（M7 §2.2）。

    - 当前 token 有效 → 轮换 + 滑动续期 + 写宽限行，同事务提交；
    - 宽限期内旧 token → 幂等返回同一个新 token，不判复用；
    - 宽限期外旧 token → 判定复用：整族失效 + `auth.refresh_reuse` 审计 + 401；
    - 未知的 token → 401 `E_REFRESH_INVALID`。
    """
    now = datetime.now(UTC)
    digest = hash_refresh_token(raw_refresh)

    # 顺手清掉过期宽限行，避免表无限增长
    db.execute(delete(RefreshGrace).where(RefreshGrace.created_at < now - GRACE_RETENTION))

    session = db.scalar(
        select(UserSession).where(UserSession.refresh_hash == digest).with_for_update()
    )
    if session is not None and session.revoked_at is None and session.expires_at > now:
        old_hash = session.refresh_hash
        new_raw = new_refresh_token()
        session.refresh_hash = hash_refresh_token(new_raw)
        # 有效期 7d 滑动：每次刷新重置（M7 §2.2）
        session.expires_at = now + timedelta(days=get_settings().refresh_ttl_days)
        session.last_used_at = now
        db.add(
            RefreshGrace(
                old_hash=old_hash,
                session_id=session.id,
                new_token_enc=_encrypt_token(new_raw),
            )
        )
        db.commit()
        return new_raw, session

    # 当前 token 不匹配：可能是宽限期内的旧 token，也可能是复用攻击
    grace = db.get(RefreshGrace, digest)
    if grace is not None:
        age = (now - grace.created_at).total_seconds()
        if age <= GRACE_SECONDS:
            restored = db.get(UserSession, grace.session_id)
            if restored is not None and restored.revoked_at is None:
                token = _decrypt_token(grace.new_token_enc)
                if token is not None:
                    # 幂等：宽限期内返回同一个新 token（M7 D-M7-18）
                    return token, restored
        else:
            # 复用：宽限期外旧 token 再次出现 → 整族失效 + 审计（M7 D-M7-3）
            target = db.get(UserSession, grace.session_id)
            if target is not None:
                family_rows = db.scalars(
                    select(UserSession).where(UserSession.family_id == target.family_id)
                ).all()
                for row in family_rows:
                    row.revoked_at = now
                audit.record(
                    db,
                    "auth.refresh_reuse",
                    actor=target.username,
                    resource=f"session:{target.id}",
                    detail={"family_id": str(target.family_id)},
                    ip=target.ip,
                )
                db.commit()
                raise ApiError("E_REFRESH_REUSE", "检测到凭证复用，会话已全部失效", status_code=401)

    raise ApiError("E_REFRESH_INVALID", "登录已过期，请重新登录", status_code=401)


def revoke_session(db: Session, session_id: str) -> None:
    """登出：标记会话撤销；重复调用不报错。"""
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        return
    session = db.get(UserSession, sid)
    if session is not None and session.revoked_at is None:
        session.revoked_at = datetime.now(UTC)
        audit.record(
            db,
            "auth.logout",
            actor=session.username,
            resource=f"session:{session.id}",
            ip=session.ip,
        )
        db.commit()


def session_is_active(db: Session, session_id: Any) -> bool:
    """access token 校验用：sid 对应会话必须存在且未撤销（M7 §2.2"撤销立即生效"）。"""
    try:
        sid = uuid.UUID(str(session_id))
    except (ValueError, TypeError):
        return False
    session = db.get(UserSession, sid)
    return session is not None and session.revoked_at is None


STREAM_TICKET_TTL_S = 60


def create_stream_ticket(db: Session, username: str) -> tuple[str, int]:
    """签发一次性 SSE stream ticket（M7 §2.4 / D-M7-5）。

    只授予读权限；ticket 只存哈希，60s 有效、用一次即删。
    """
    from app.domain.models import SseTicket

    raw = f"st_{secrets.token_urlsafe(16)}"
    now = datetime.now(UTC)
    db.add(
        SseTicket(
            ticket_hash=hashlib.sha256(raw.encode()).hexdigest(),
            username=username,
            expires_at=now + timedelta(seconds=STREAM_TICKET_TTL_S),
        )
    )
    # 顺手清掉过期票，避免表增长
    db.execute(delete(SseTicket).where(SseTicket.expires_at < now))
    db.commit()
    return raw, STREAM_TICKET_TTL_S


def consume_stream_ticket(db: Session, ticket: str) -> str | None:
    """校验并消费 ticket；一次性语义：命中即删（M7 §2.4）。"""
    from app.domain.models import SseTicket

    digest = hashlib.sha256(ticket.encode()).hexdigest()
    now = datetime.now(UTC)
    row = db.scalar(select(SseTicket).where(SseTicket.ticket_hash == digest))
    if row is None or row.used_at is not None or row.expires_at <= now:
        return None
    db.execute(delete(SseTicket).where(SseTicket.id == row.id))
    db.commit()
    return row.username
