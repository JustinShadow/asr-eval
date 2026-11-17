"""MetricProfile 文档 config 结构与模板

Revision ID: 8c3d7f1a5b29
Revises: 7a4b8e2c9f16
Create Date: 2026-11-04 21:40:00.000000

按 M3 §2.1/§6：default_zh 迁移到文档 config 结构（默认全开），
profile_hash 随之重算——口径变化意味着历史结果不再可比（预期行为）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8c3d7f1a5b29"
down_revision: str | None = "7a4b8e2c9f16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "metric_profile", sa.Column("version", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("metric_profile", sa.Column("note", sa.String(length=512), nullable=True))
    op.add_column("metric_profile", sa.Column("created_by", sa.String(length=64), nullable=True))

    # default_zh 迁移到 §2.1 结构并重算 hash
    from app.domain.metric_profiles import TEMPLATE_PARAMS, profile_hash

    params = TEMPLATE_PARAMS["default_zh"]
    hash_ = profile_hash(params)
    import json

    op.get_bind().execute(
        sa.text(
            "update metric_profile set params = :params, profile_hash = :hash "
            "where name = 'default_zh'"
        ),
        {"params": json.dumps(params, ensure_ascii=False), "hash": hash_},
    )


def downgrade() -> None:
    op.drop_column("metric_profile", "created_by")
    op.drop_column("metric_profile", "note")
    op.drop_column("metric_profile", "version")
