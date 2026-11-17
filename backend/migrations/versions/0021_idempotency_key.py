"""创建类接口幂等响应缓存。

Revision ID: 0021_idempotency_key
Revises: 0020_user_grant
Create Date: 2026-11-05

按 M7 §4.5 保存请求 hash、首次响应及 24 小时创建时间。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0021_idempotency_key"
down_revision: str | None = "0020_user_grant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_key",
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response", JSONB(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("headers", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("username", "key"),
    )
    op.create_index("ix_idempotency_key_created_at", "idempotency_key", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_key_created_at", table_name="idempotency_key")
    op.drop_table("idempotency_key")
