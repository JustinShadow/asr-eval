"""Run ETA 字段与 case_result 性能列

Revision ID: 5d94c7b1f0a2
Revises: 3f8c21a9e5b7
Create Date: 2026-11-04 19:30:00.000000

按 M4 §2/§4：
- eta_ms/eta_p90_ms/eta_source/eta_confidence：ETA 必须带出处与置信度；
- case_result.server_ms/rtf_dev：ETA 基线的唯一数据来源（性能只从评测结果派生）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5d94c7b1f0a2"
down_revision: str | None = "3f8c21a9e5b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run", sa.Column("eta_ms", sa.Integer(), nullable=True))
    op.add_column("run", sa.Column("eta_p90_ms", sa.Integer(), nullable=True))
    op.add_column("run", sa.Column("eta_source", sa.String(length=16), nullable=True))
    op.add_column("run", sa.Column("eta_confidence", sa.String(length=8), nullable=True))
    op.add_column("case_result", sa.Column("server_ms", sa.Integer(), nullable=True))
    op.add_column("case_result", sa.Column("rtf_dev", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("case_result", "rtf_dev")
    op.drop_column("case_result", "server_ms")
    op.drop_column("run", "eta_confidence")
    op.drop_column("run", "eta_source")
    op.drop_column("run", "eta_p90_ms")
    op.drop_column("run", "eta_ms")
