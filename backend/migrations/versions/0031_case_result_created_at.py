"""记录 Run 样本结果的落库时间。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_case_result_created_at"
down_revision: str | None = "0030_run_condition_abort"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为新写入的逐样本结果保留真实记录时间；历史记录保持为空。"""
    op.add_column(
        "case_result",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """移除逐样本记录时间列。"""
    op.drop_column("case_result", "created_at")
