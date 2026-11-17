"""run_target 冻结 deployment 与 hypothesis 内容寻址

Revision ID: 7a4b8e2c9f16
Revises: 6e2f9a4c8d13
Create Date: 2026-11-04 21:00:00.000000

按 M4 §2.1/§4/D-M4-9/D-M4-11/D-M4-12：
- run_target 指向 deployment（含 lanes、abort_reason）；
- hypothesis 是内容寻址的推理产物（cache_key 唯一），命中即跳过转写；
- case_result 只存 timing 与状态，文本经 hypothesis_id 引用。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "7a4b8e2c9f16"
down_revision: str | None = "6e2f9a4c8d13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run_target", sa.Column("deployment_id", UUID(), nullable=True))
    op.add_column(
        "run_target",
        sa.Column("lanes", sa.SmallInteger(), nullable=False, server_default="1"),
    )
    # abort_reason 基础迁移已有（String 128），这里只补 deployment_id 与 lanes
    # 回填：取该设备最近一次部署快照（与执行时取法一致）
    op.execute(
        """
        update run_target rt
        set deployment_id = d.id
        from (
            select distinct on (device_id) id, device_id
            from deployment
            order by device_id, captured_at desc
        ) d
        where rt.device_id = d.device_id
        """
    )
    op.create_foreign_key(
        "fk_run_target_deployment",
        "run_target",
        "deployment",
        ["deployment_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_run_target_deployment_id"), "run_target", ["deployment_id"], unique=False
    )

    op.create_table(
        "hypothesis",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("cache_key", sa.String(length=32), nullable=False, unique=True),
        sa.Column("deployment_id", UUID(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("params_hash", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("chunk_ms", sa.Integer(), nullable=True),
        sa.Column("input_rate", sa.Float(), nullable=True),
        sa.Column("concurrency", sa.SmallInteger(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("segments", JSONB(), nullable=True),
        sa.Column("speaker_match", JSONB(), nullable=True),
        sa.Column("usage", JSONB(), nullable=True),
        sa.Column("raw_uri", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["deployment_id"], ["deployment.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], ondelete="CASCADE"),
    )

    op.add_column("case_result", sa.Column("hypothesis_id", UUID(), nullable=True))
    op.add_column("case_result", sa.Column("dev_ms", sa.Integer(), nullable=True))
    op.add_column("case_result", sa.Column("ttft_ms", sa.Integer(), nullable=True))
    op.add_column("case_result", sa.Column("finalize_ms", sa.Integer(), nullable=True))
    op.add_column("case_result", sa.Column("rtf_e2e", sa.Float(), nullable=True))
    op.add_column("case_result", sa.Column("resource", JSONB(), nullable=True))
    op.create_foreign_key(
        "fk_case_result_hypothesis",
        "case_result",
        "hypothesis",
        ["hypothesis_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_case_result_hypothesis", "case_result", type_="foreignkey")
    op.drop_column("case_result", "resource")
    op.drop_column("case_result", "rtf_e2e")
    op.drop_column("case_result", "finalize_ms")
    op.drop_column("case_result", "ttft_ms")
    op.drop_column("case_result", "dev_ms")
    op.drop_column("case_result", "hypothesis_id")
    op.drop_table("hypothesis")
    op.drop_index(op.f("ix_run_target_deployment_id"), table_name="run_target")
    op.drop_constraint("fk_run_target_deployment", "run_target", type_="foreignkey")
    op.drop_column("run_target", "lanes")
    op.drop_column("run_target", "deployment_id")
