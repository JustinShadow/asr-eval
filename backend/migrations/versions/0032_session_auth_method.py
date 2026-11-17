"""在用户会话中记录登录认证方式。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_session_auth_method"
down_revision: str | None = "0031_case_result_created_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """记录会话认证来源，使 break-glass 告警在刷新后保持准确。"""
    op.add_column(
        "user_session",
        sa.Column(
            "auth_method",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'local'"),
        ),
    )


def downgrade() -> None:
    """移除会话认证来源。"""
    op.drop_column("user_session", "auth_method")
