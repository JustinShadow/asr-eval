"""import_task 异步导入任务

Revision ID: 9b1e4d6c8a27
Revises: 8c3d7f1a5b29
Create Date: 2026-11-04 22:10:00.000000

按 M1 §4/§5.9：导入切异步任务；progress.persisted 支持断点续跑。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "9b1e4d6c8a27"
down_revision: str | None = "8c3d7f1a5b29"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "import_task",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("dataset_id", UUID(), nullable=False),
        sa.Column("dataset_version_id", UUID(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("phase", sa.String(length=16), nullable=True),
        sa.Column("progress", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("report", JSONB(), nullable=True),
        sa.Column("error", sa.String(length=1024), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["dataset_id"], ["dataset.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dataset_version_id"], ["dataset_version.id"], ondelete="CASCADE"),
    )
    op.create_index(op.f("ix_import_task_dataset_id"), "import_task", ["dataset_id"])
    op.create_index(op.f("ix_import_task_status"), "import_task", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_import_task_status"), table_name="import_task")
    op.drop_index(op.f("ix_import_task_dataset_id"), table_name="import_task")
    op.drop_table("import_task")
