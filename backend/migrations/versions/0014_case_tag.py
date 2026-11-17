"""Badcase 人工打标

Revision ID: 2c7d9e4a6b18
Revises: 9b1e4d6c8a27
Create Date: 2026-11-05 10:30:00.000000

按 M8 §9.7：1-9 打标；一条样本一个标签，记录操作者。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "2c7d9e4a6b18"
down_revision: str | None = "9b1e4d6c8a27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "case_tag",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("evaluation_id", UUID(), nullable=False),
        sa.Column("case_score_id", sa.BigInteger(), nullable=False),
        sa.Column("tag", sa.SmallInteger(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["evaluation_id"], ["evaluation.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["case_score_id"], ["case_score.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_score_id", name="uq_case_tag"),
    )
    op.create_index(op.f("ix_case_tag_evaluation_id"), "case_tag", ["evaluation_id"])
    op.create_index(op.f("ix_case_tag_case_score_id"), "case_tag", ["case_score_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_case_tag_case_score_id"), table_name="case_tag")
    op.drop_index(op.f("ix_case_tag_evaluation_id"), table_name="case_tag")
    op.drop_table("case_tag")
