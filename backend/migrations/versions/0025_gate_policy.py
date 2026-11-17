"""增加配置化评测门禁策略。

Revision ID: 0025_gate_policy
Revises: 0024_resource_event_versions
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0025_gate_policy"
down_revision: str | None = "0024_resource_event_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """按 M7 §12 保存管理员配置的门禁阈值与策略版本。"""
    op.create_table(
        "gate_policy",
        sa.Column("id", UUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("benchmark_id", UUID(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("rules", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("on_missing", sa.String(length=8), server_default="skip", nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["benchmark_id"], ["benchmark.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_gate_policy_benchmark_id", "gate_policy", ["benchmark_id"])


def downgrade() -> None:
    """删除门禁策略表。"""
    op.drop_index("ix_gate_policy_benchmark_id", table_name="gate_policy")
    op.drop_table("gate_policy")
