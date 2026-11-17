"""refresh 宽限与审计日志

Revision ID: 7c1a9e0b3d44
Revises: 121958ddafaf
Create Date: 2026-11-04 18:05:00.000000

为什么需要这两张表：
- refresh_grace：refresh 轮换 30s 幂等宽限（M7 D-M7-18），旧哈希 → 加密的新 token；
- audit_log：认证与高危动作的审计必须与业务写同事务（M7 D-M7-11）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "7c1a9e0b3d44"
down_revision: str | None = "121958ddafaf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refresh_grace",
        sa.Column("old_hash", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("new_token_enc", sa.String(length=512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["user_session.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("old_hash"),
    )
    op.create_index(
        op.f("ix_refresh_grace_session_id"), "refresh_grace", ["session_id"], unique=False
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource", sa.String(length=128), nullable=True),
        sa.Column("detail", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_log_action"), "audit_log", ["action"], unique=False)
    op.create_index(op.f("ix_audit_log_created_at"), "audit_log", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_log_created_at"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_action"), table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index(op.f("ix_refresh_grace_session_id"), table_name="refresh_grace")
    op.drop_table("refresh_grace")
