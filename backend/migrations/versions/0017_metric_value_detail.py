"""metric_value.detail

Revision ID: 6a9d3c5b8e27
Revises: 5f2c8d1e7a46
Create Date: 2026-11-05 15:30:00.000000

按 M5 §2.1/§5.3/§5.4：标点 P/R/句末准确率、ITN 实体计数需要随聚合值一起落库。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "6a9d3c5b8e27"
down_revision: str | None = "5f2c8d1e7a46"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "metric_value",
        sa.Column("detail", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("metric_value", "detail")
