"""编辑 presence 的 PostgreSQL 持久化服务。"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.domain.models import AppUser, Presence

PRESENCE_TTL_SECONDS = 90


def heartbeat(db: Session, *, resource_type: str, resource_id: str, username: str) -> datetime:
    """写入或续期用户 presence；过期记录随心跳清理。"""
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=PRESENCE_TTL_SECONDS)
    db.execute(delete(Presence).where(Presence.expires_at <= now))
    statement = (
        insert(Presence)
        .values(
            resource_type=resource_type,
            resource_id=resource_id,
            username=username,
            last_seen_at=now,
            expires_at=expires_at,
        )
        .on_conflict_do_update(
            index_elements=[Presence.resource_type, Presence.resource_id, Presence.username],
            set_={"last_seen_at": now, "expires_at": expires_at},
        )
    )
    db.execute(statement)
    db.commit()
    return expires_at


def leave(db: Session, *, resource_type: str, resource_id: str, username: str) -> None:
    """移除当前用户在目标资源上的 presence。"""
    db.execute(
        delete(Presence).where(
            Presence.resource_type == resource_type,
            Presence.resource_id == resource_id,
            Presence.username == username,
        )
    )
    db.commit()


def active_editors(db: Session, *, resource_type: str, resource_id: str) -> list[dict[str, Any]]:
    """列出目标资源上 TTL 未过期的编辑者。"""
    now = datetime.now(UTC)
    rows = db.execute(
        select(Presence, AppUser.display_name)
        .join(AppUser, AppUser.username == Presence.username)
        .where(
            Presence.resource_type == resource_type,
            Presence.resource_id == resource_id,
            Presence.expires_at > now,
        )
        .order_by(Presence.username)
    ).all()
    return [
        {
            "username": presence.username,
            "display_name": display_name or presence.username,
            "last_seen_at": presence.last_seen_at.isoformat(),
        }
        for presence, display_name in rows
    ]
