"""按人授权表。

Revision ID: 0020_user_grant
Revises: 0019_evaluation_progress
Create Date: 2026-11-05

按 M7 §3.3 存储角色之外的临时权限，并支持撤销与对象范围。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0020_user_grant"
down_revision: str | None = "0019_evaluation_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_grant",
        sa.Column("id", UUID(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("permission", sa.String(length=64), nullable=False),
        sa.Column("scope", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=True),
        sa.Column("granted_by", sa.String(length=64), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["username"], ["app_user.username"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_grant_username", "user_grant", ["username"])
    op.create_index("ix_user_grant_permission", "user_grant", ["permission"])
    op.create_index(
        "ix_user_grant_active_user",
        "user_grant",
        ["username", "expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.add_column(
        "app_user",
        sa.Column("ldap_groups", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.create_table(
        "service_token",
        sa.Column("id", UUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("secret_hash"),
    )


def downgrade() -> None:
    op.drop_table("service_token")
    op.drop_column("app_user", "ldap_groups")
    op.drop_index("ix_user_grant_active_user", table_name="user_grant")
    op.drop_index("ix_user_grant_permission", table_name="user_grant")
    op.drop_index("ix_user_grant_username", table_name="user_grant")
    op.drop_table("user_grant")
