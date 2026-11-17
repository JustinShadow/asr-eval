"""增加执行期设备硬件资源采样表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0038_run_resource_sample"
down_revision: str | None = "0037_run_group"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建资源采样表，随 Run/Target 删除级联清理。"""
    op.create_table(
        "run_resource_sample",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "metrics",
            JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_id"], ["run_target.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_run_resource_sample_run_id", "run_resource_sample", ["run_id"])
    op.create_index("ix_run_resource_sample_target_id", "run_resource_sample", ["target_id"])
    op.create_index("ix_run_resource_sample_sampled_at", "run_resource_sample", ["sampled_at"])


def downgrade() -> None:
    """删除资源采样表。"""
    op.drop_index("ix_run_resource_sample_sampled_at", table_name="run_resource_sample")
    op.drop_index("ix_run_resource_sample_target_id", table_name="run_resource_sample")
    op.drop_index("ix_run_resource_sample_run_id", table_name="run_resource_sample")
    op.drop_table("run_resource_sample")
