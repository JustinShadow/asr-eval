"""增加 Fernet 加密的设备凭据表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_device_credentials"
down_revision: str | None = "0035_speaker_voiceprints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保存凭据密文，并随设备删除而级联清理。"""
    op.create_table(
        "device_credential",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["device_id"], ["device.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_id"),
    )
    op.create_index("ix_device_credential_device_id", "device_credential", ["device_id"])


def downgrade() -> None:
    """删除设备凭据密文表。"""
    op.drop_index("ix_device_credential_device_id", table_name="device_credential")
    op.drop_table("device_credential")
