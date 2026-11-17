"""增加说话人注册音频与按部署缓存的声纹向量。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_speaker_voiceprints"
down_revision: str | None = "0034_metric_profile_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立说话人、注册录音与部署级向量缓存。"""
    op.create_table(
        "speaker",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "speaker_enrollment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("speaker_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["speaker_id"], ["speaker.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_index("ix_speaker_enrollment_speaker_id", "speaker_enrollment", ["speaker_id"])
    op.create_table(
        "voiceprint",
        sa.Column("enrollment_id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["deployment_id"], ["deployment.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["enrollment_id"], ["speaker_enrollment.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("enrollment_id", "deployment_id"),
    )
    op.create_index("ix_voiceprint_deployment", "voiceprint", ["deployment_id"])


def downgrade() -> None:
    """按外键依赖顺序删除声纹表。"""
    op.drop_index("ix_voiceprint_deployment", table_name="voiceprint")
    op.drop_table("voiceprint")
    op.drop_index("ix_speaker_enrollment_speaker_id", table_name="speaker_enrollment")
    op.drop_table("speaker_enrollment")
    op.drop_table("speaker")
