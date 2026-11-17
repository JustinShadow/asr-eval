"""增加跨数据集评测组（RunGroup）。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_run_group"
down_revision: str | None = "0036_device_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建组表，并给 Run 增加可空 group_id。"""
    op.create_table(
        "run_group",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="accuracy"),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column("run", sa.Column("group_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_run_group_id",
        "run",
        "run_group",
        ["group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_run_group_id", "run", ["group_id"])


def downgrade() -> None:
    """移除组成员关系与组表。"""
    op.drop_index("ix_run_group_id", table_name="run")
    op.drop_constraint("fk_run_group_id", "run", type_="foreignkey")
    op.drop_column("run", "group_id")
    op.drop_table("run_group")
