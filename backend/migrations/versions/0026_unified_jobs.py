"""统一异步任务表，并迁移历史导入任务。

Revision ID: 0026_unified_jobs
Revises: 0025_gate_policy
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0026_unified_jobs"
down_revision: str | None = "0025_gate_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """按 M7 §6 统一任务状态、进度、结果与错误字段。"""
    op.create_table(
        "job",
        sa.Column("id", UUID(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("ref_type", sa.String(length=32), nullable=False),
        sa.Column("ref_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("progress", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail", JSONB(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_ref_created_at", "job", ["ref_type", "ref_id", "created_at"])
    op.create_index("ix_job_status", "job", ["status"])
    op.create_index("ix_job_status_created_at", "job", ["status", "created_at"])

    op.execute(
        """
        INSERT INTO job (
            id, kind, ref_type, ref_id, status, progress, result,
            error_code, error_detail, created_by, created_at, started_at,
            finished_at, version
        )
        SELECT
            task.id,
            'import',
            CASE WHEN task.dataset_version_id IS NULL THEN 'dataset' ELSE 'dataset_version' END,
            COALESCE(task.dataset_version_id, task.dataset_id)::text,
            CASE WHEN task.status = 'done' THEN 'succeeded' ELSE task.status END,
            COALESCE(task.progress, '{}'::jsonb)
                || CASE WHEN task.phase IS NULL THEN '{}'::jsonb
                        ELSE jsonb_build_object('phase', task.phase) END,
            CASE
                WHEN task.report IS NULL AND task.dataset_version_id IS NULL THEN NULL
                ELSE COALESCE(task.report, '{}'::jsonb)
                    || jsonb_build_object(
                        'resource_id', task.dataset_version_id::text,
                        'url', '/api/v1/dataset-versions/' || task.dataset_version_id::text
                    )
            END,
            CASE WHEN task.error IS NULL THEN NULL ELSE 'E_IMPORT_FAILED' END,
            CASE WHEN task.error IS NULL THEN NULL
                 ELSE jsonb_build_object('message', task.error) END,
            task.created_by,
            task.created_at,
            task.started_at,
            task.finished_at,
            task.version
        FROM import_task AS task
        """
    )
    op.drop_index("ix_import_task_status", table_name="import_task")
    op.drop_index("ix_import_task_dataset_id", table_name="import_task")
    op.drop_table("import_task")


def downgrade() -> None:
    """恢复旧导入任务表；只回迁 kind=import 的任务。"""
    op.create_table(
        "import_task",
        sa.Column("id", UUID(), nullable=False),
        sa.Column("dataset_id", UUID(), nullable=False),
        sa.Column("dataset_version_id", UUID(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=True),
        sa.Column("progress", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
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
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["dataset.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dataset_version_id"], ["dataset_version.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_import_task_dataset_id", "import_task", ["dataset_id"])
    op.create_index("ix_import_task_status", "import_task", ["status"])
    op.execute(
        """
        INSERT INTO import_task (
            id, dataset_id, dataset_version_id, status, phase, progress, report,
            error, created_by, created_at, started_at, finished_at, version
        )
        SELECT
            job.id,
            CASE WHEN job.ref_type = 'dataset' THEN job.ref_id::uuid ELSE version.dataset_id END,
            CASE WHEN job.ref_type = 'dataset_version' THEN job.ref_id::uuid ELSE NULL END,
            CASE WHEN job.status = 'succeeded' THEN 'done' ELSE job.status END,
            job.progress->>'phase',
            job.progress - 'phase',
            job.result,
            job.error_detail->>'message',
            job.created_by,
            job.created_at,
            job.started_at,
            job.finished_at,
            job.version
        FROM job
        LEFT JOIN dataset_version AS version
            ON job.ref_type = 'dataset_version' AND version.id = job.ref_id::uuid
        WHERE job.kind = 'import'
          AND (job.ref_type = 'dataset' OR version.dataset_id IS NOT NULL)
        """
    )
    op.drop_index("ix_job_status_created_at", table_name="job")
    op.drop_index("ix_job_status", table_name="job")
    op.drop_index("ix_job_ref_created_at", table_name="job")
    op.drop_table("job")
