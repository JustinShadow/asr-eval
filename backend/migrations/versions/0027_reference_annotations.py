"""补充参考分段标记与版本级说话人属性。

Revision ID: 0027_reference_annotations
Revises: 0026_unified_jobs
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0027_reference_annotations"
down_revision: str | None = "0026_unified_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """按 M1 §4 为参考段落保存跳过/重叠标记和说话人属性。"""
    op.add_column(
        "ref_segment",
        sa.Column("skip", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "ref_segment",
        sa.Column("overlap", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_table(
        "speaker_meta",
        sa.Column("dataset_version_id", UUID(), nullable=False),
        sa.Column("speaker_id", sa.String(length=64), nullable=False),
        sa.Column("gender", sa.String(length=16), nullable=True),
        sa.Column("age", sa.String(length=32), nullable=True),
        sa.Column("accent", sa.String(length=64), nullable=True),
        sa.Column("meta", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(["dataset_version_id"], ["dataset_version.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("dataset_version_id", "speaker_id"),
    )


def downgrade() -> None:
    """移除新增的分段标记与说话人属性表。"""
    op.drop_table("speaker_meta")
    op.drop_column("ref_segment", "overlap")
    op.drop_column("ref_segment", "skip")
