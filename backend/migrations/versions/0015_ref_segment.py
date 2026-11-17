"""参考文本分段

Revision ID: 4e8a1c6d9b35
Revises: 2c7d9e4a6b18
Create Date: 2026-11-05 11:20:00.000000

按 M1 §4：ref_segment（时间区间 + 说话人），供 DER/命名与 Badcase 泳道。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4e8a1c6d9b35"
down_revision: str | None = "2c7d9e4a6b18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ref_segment",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("speaker", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("start_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("end_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ref_segment_item_id"), "ref_segment", ["item_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_ref_segment_item_id"), table_name="ref_segment")
    op.drop_table("ref_segment")
