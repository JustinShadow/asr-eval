"""增加 Run、Evaluation 和 job 的变更版本。

Revision ID: 0024_resource_event_versions
Revises: 0023_presence
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_resource_event_versions"
down_revision: str | None = "0023_presence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为异步更新资源保存单调递增版本，供用户级变更信号使用。"""
    for table in ("run", "evaluation", "import_task"):
        op.add_column(
            table,
            sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        )


def downgrade() -> None:
    """回退异步资源版本字段。"""
    for table in ("import_task", "evaluation", "run"):
        op.drop_column(table, "version")
