"""创建编辑 presence 表。

Revision ID: 0023_presence
Revises: 0022_config_etags
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_presence"
down_revision: str | None = "0022_config_etags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """按资源、资源 ID、用户名唯一记录在线编辑者。"""
    op.create_table(
        "presence",
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["username"], ["app_user.username"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("resource_type", "resource_id", "username"),
    )
    op.create_index("ix_presence_expires_at", "presence", ["expires_at"])


def downgrade() -> None:
    """删除编辑 presence 表。"""
    op.drop_index("ix_presence_expires_at", table_name="presence")
    op.drop_table("presence")
