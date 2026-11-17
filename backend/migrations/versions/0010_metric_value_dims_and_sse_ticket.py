"""metric_value 维度列与 sse_ticket

Revision ID: 6e2f9a4c8d13
Revises: 5d94c7b1f0a2
Create Date: 2026-11-04 20:10:00.000000

按 M5 §4：MetricValue 需要目标/桶维度（run_target_id/scope/bucket）与 CI 列位；
按 M7 §2.4：SSE 一次性 stream ticket（60s、用一次即删、只存哈希）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "6e2f9a4c8d13"
down_revision: str | None = "5d94c7b1f0a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("metric_value", sa.Column("run_target_id", UUID(), nullable=True))
    op.add_column(
        "metric_value",
        sa.Column("scope", sa.String(length=8), nullable=False, server_default="micro"),
    )
    op.add_column(
        "metric_value",
        sa.Column("bucket", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column("metric_value", sa.Column("ci_low", sa.Float(), nullable=True))
    op.add_column("metric_value", sa.Column("ci_high", sa.Float(), nullable=True))
    op.create_foreign_key(
        "fk_metric_value_run_target",
        "metric_value",
        "run_target",
        ["run_target_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        op.f("ix_metric_value_run_target_id"),
        "metric_value",
        ["run_target_id"],
        unique=False,
    )

    op.create_table(
        "sse_ticket",
        sa.Column("id", UUID(), nullable=False),
        sa.Column("ticket_hash", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticket_hash"),
    )
    op.create_index(op.f("ix_sse_ticket_username"), "sse_ticket", ["username"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_sse_ticket_username"), table_name="sse_ticket")
    op.drop_table("sse_ticket")
    op.drop_index(op.f("ix_metric_value_run_target_id"), table_name="metric_value")
    op.drop_constraint("fk_metric_value_run_target", "metric_value", type_="foreignkey")
    op.drop_column("metric_value", "ci_high")
    op.drop_column("metric_value", "ci_low")
    op.drop_column("metric_value", "bucket")
    op.drop_column("metric_value", "scope")
    op.drop_column("metric_value", "run_target_id")
