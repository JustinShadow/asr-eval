"""report 快照

Revision ID: 5f2c8d1e7a46
Revises: 4e8a1c6d9b35
Create Date: 2026-11-05 14:00:00.000000

按 M6 §2/§5.8：报告快照不可变；payload/html/xlsx 落本地对象目录。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "5f2c8d1e7a46"
down_revision: str | None = "4e8a1c6d9b35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="single"),
        sa.Column("evaluation_ids", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("query", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "template_version", sa.String(length=16), nullable=False, server_default="report-1"
        ),
        sa.Column("level", sa.String(length=16), nullable=False, server_default="summary"),
        sa.Column("payload_uri", sa.String(length=512), nullable=True),
        sa.Column("html_uri", sa.String(length=512), nullable=True),
        sa.Column("xlsx_uri", sa.String(length=512), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(op.f("ix_report_created_at"), "report", ["created_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_report_created_at"), table_name="report")
    op.drop_table("report")
