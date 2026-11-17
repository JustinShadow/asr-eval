"""支持 Run 条件变化终态。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_run_condition_abort"
down_revision: str | None = "0029_staging_probe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """扩展 Run 与 target 状态长度以容纳 M4 状态名。"""
    op.alter_column(
        "run",
        "status",
        existing_type=sa.String(length=24),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
    op.alter_column(
        "run_target",
        "status",
        existing_type=sa.String(length=24),
        type_=sa.String(length=32),
        existing_nullable=False,
    )


def downgrade() -> None:
    """回退时将新终态映射为 failed，target 原因仍保留在 abort_reason。"""
    op.execute("UPDATE run SET status = 'failed' WHERE status = 'aborted_condition_changed'")
    op.execute("UPDATE run_target SET status = 'failed' WHERE status = 'aborted_condition_changed'")
    op.alter_column(
        "run_target",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=24),
        existing_nullable=False,
    )
    op.alter_column(
        "run",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=24),
        existing_nullable=False,
    )
