"""benchmark 多 subset 与 version_hash

Revision ID: 3f8c21a9e5b7
Revises: 7c1a9e0b3d44
Create Date: 2026-11-04 18:40:00.000000

按 M3 §2/§5.6：
- subsets（JSONB 数组）替换单值 subset，支持 phone+mic 多条件组合；
- version_hash 冻结内容/口径/采样/分桶/参数，相同即复用（幂等）；
- 补 params/bucket_rules/note/archived。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "3f8c21a9e5b7"
down_revision: str | None = "7c1a9e0b3d44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "benchmark",
        sa.Column("subsets", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        "benchmark",
        sa.Column("params", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column(
        "benchmark",
        sa.Column("bucket_rules", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column("benchmark", sa.Column("version_hash", sa.String(length=16), nullable=True))
    op.add_column("benchmark", sa.Column("note", sa.String(length=512), nullable=True))
    op.add_column(
        "benchmark", sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    # 旧单值迁移为数组；旧查询语义 subset="all" 对应 ["all"]
    op.execute("update benchmark set subsets = to_jsonb(array[subset])")
    op.create_index(op.f("ix_benchmark_version_hash"), "benchmark", ["version_hash"], unique=False)
    op.drop_column("benchmark", "subset")


def downgrade() -> None:
    op.add_column(
        "benchmark", sa.Column("subset", sa.String(length=32), nullable=False, server_default="all")
    )
    op.execute("update benchmark set subset = coalesce(subsets->>0, 'all')")
    op.drop_index(op.f("ix_benchmark_version_hash"), table_name="benchmark")
    op.drop_column("benchmark", "archived")
    op.drop_column("benchmark", "note")
    op.drop_column("benchmark", "version_hash")
    op.drop_column("benchmark", "bucket_rules")
    op.drop_column("benchmark", "params")
    op.drop_column("benchmark", "subsets")
