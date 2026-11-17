"""metric_value 按目标与桶唯一。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_metric_bucket_key"
down_revision: str | None = "6a9d3c5b8e27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_metric", "metric_value", type_="unique")
    op.create_unique_constraint(
        "uq_metric",
        "metric_value",
        ["evaluation_id", "metric", "subset", "run_target_id", "scope", "bucket"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(
        sa.text(
            "select 1 from metric_value group by evaluation_id, metric, subset "
            "having count(*) > 1 limit 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError("metric_value 已含分桶行，不能降级到无桶唯一约束")
    op.drop_constraint("uq_metric", "metric_value", type_="unique")
    op.create_unique_constraint(
        "uq_metric",
        "metric_value",
        ["evaluation_id", "metric", "subset"],
    )
