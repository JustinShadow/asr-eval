"""增加按音频资产和处理口径缓存的变体表。

Revision ID: 0028_audio_variants
Revises: 0027_reference_annotations
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0028_audio_variants"
down_revision: str | None = "0027_reference_annotations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """按 M1 §4/§5.4 建立可重用的懒生成音频变体索引。"""
    op.create_table(
        "audio_variant",
        sa.Column("asset_id", UUID(), nullable=False),
        sa.Column("profile", sa.String(length=32), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("uri", sa.String(length=1024), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["audio_asset.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("asset_id", "profile"),
    )
    # 历史 AISHELL-3 版本导入时把 44.1kHz 原音误记为 as_is，修正为重采样口径。
    op.execute(
        """
        UPDATE dataset_subset AS subset
        SET audio_profile = 'mono_mix'
        FROM dataset_version AS version
        JOIN dataset ON dataset.id = version.dataset_id
        WHERE subset.dataset_version_id = version.id
          AND dataset.source_type = 'aishell3'
        """
    )


def downgrade() -> None:
    """移除音频变体缓存表。"""
    op.execute(
        """
        UPDATE dataset_subset AS subset
        SET audio_profile = 'as_is'
        FROM dataset_version AS version
        JOIN dataset ON dataset.id = version.dataset_id
        WHERE subset.dataset_version_id = version.id
          AND dataset.source_type = 'aishell3'
        """
    )
    op.drop_table("audio_variant")
