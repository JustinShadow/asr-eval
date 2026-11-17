"""允许同一口径名称下保留多个不可变版本。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0034_metric_profile_versions"
down_revision: str | None = "0033_device_ops_action"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """用名称与版本联合约束替代单一名称约束。"""
    op.drop_constraint("metric_profile_name_key", "metric_profile", type_="unique")
    op.create_unique_constraint(
        "uq_metric_profile_name_version", "metric_profile", ["name", "version"]
    )
    op.create_index("ix_metric_profile_name", "metric_profile", ["name"])


def downgrade() -> None:
    """回滚到名称唯一；同名多版本需先手工整理。"""
    op.drop_index("ix_metric_profile_name", table_name="metric_profile")
    op.drop_constraint("uq_metric_profile_name_version", "metric_profile", type_="unique")
    op.create_unique_constraint("metric_profile_name_key", "metric_profile", ["name"])
