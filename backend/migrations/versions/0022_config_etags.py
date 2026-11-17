"""为配置资源增加并发版本与修改人信息。

Revision ID: 0022_config_etags
Revises: 0021_idempotency_key
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_config_etags"
down_revision: str | None = "0021_idempotency_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为配置记录增加并发元数据；MetricProfile.version 已由 0012 创建。"""
    for table in ("device", "benchmark"):
        op.add_column(
            table,
            sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        )
    for table in ("device", "metric_profile", "benchmark"):
        op.add_column(table, sa.Column("updated_by", sa.String(length=255), nullable=True))

    for table in ("metric_profile", "benchmark"):
        op.add_column(
            table,
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )


def downgrade() -> None:
    """回退配置资源的 ETag 元数据。"""
    for table in ("metric_profile", "benchmark"):
        op.drop_column(table, "updated_at")
    for table in ("device", "benchmark"):
        op.drop_column(table, "version")
    for table in ("device", "metric_profile", "benchmark"):
        op.drop_column(table, "updated_by")
