"""记录设备准备与重启动作。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0033_device_ops_action"
down_revision: str | None = "0032_session_auth_method"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立设备运维历史表。"""
    op.create_table(
        "ops_action",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'queued'")
        ),
        sa.Column("detail", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["device_id"], ["device.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ops_action_device_id", "ops_action", ["device_id"])
    op.create_index("ix_ops_action_status", "ops_action", ["status"])
    op.create_index("ix_ops_action_device_created_at", "ops_action", ["device_id", "created_at"])


def downgrade() -> None:
    """删除设备运维历史表。"""
    op.drop_index("ix_ops_action_device_created_at", table_name="ops_action")
    op.drop_index("ix_ops_action_status", table_name="ops_action")
    op.drop_index("ix_ops_action_device_id", table_name="ops_action")
    op.drop_table("ops_action")
