"""审计日志写入。

约定见 docs/m7-api-auth-design.md D-M7-11：审计与业务写在同一事务提交，
本模块只负责 add，不 commit——由调用方的业务事务一起落库。
"""

from typing import Any

from sqlalchemy.orm import Session

from app.domain.models import AuditLog


def record(
    db: Session,
    action: str,
    *,
    actor: str | None = None,
    resource: str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """在当前事务内追加一条审计记录；提交由调用方负责。"""
    db.add(
        AuditLog(
            actor=actor,
            action=action,
            resource=resource,
            detail=detail or {},
            ip=ip,
        )
    )
