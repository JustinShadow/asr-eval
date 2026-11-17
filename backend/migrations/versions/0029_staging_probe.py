"""记录设备可用的声纹音频暂存方式。

Revision ID: 0029_staging_probe
Revises: 0028_audio_variants
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0029_staging_probe"
down_revision: str | None = "0028_audio_variants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立 M2 §2.5 暂存探测缓存表。"""
    op.create_table(
        "staging_probe",
        sa.Column("device_id", UUID(), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("host_dir", sa.String(length=1024), nullable=True),
        sa.Column("container_dir", sa.String(length=1024), nullable=True),
        sa.Column("deployment_uid", sa.String(length=16), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.String(length=1024), nullable=True),
        sa.ForeignKeyConstraint(["device_id"], ["device.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("device_id"),
    )


def downgrade() -> None:
    """删除暂存探测缓存表。"""
    op.drop_table("staging_probe")
