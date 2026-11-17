"""Evaluation 评分进度与队列索引。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0019_evaluation_progress"
down_revision: str | None = "0018_metric_bucket_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evaluation",
        sa.Column("progress", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_evaluation_status", "evaluation", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_evaluation_status", table_name="evaluation")
    op.drop_column("evaluation", "progress")
