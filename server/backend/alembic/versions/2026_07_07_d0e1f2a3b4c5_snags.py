"""snags — snag register (DB is source of truth, vault note is generated)

snags: one row per defect with immutable UID (SNAG-0042, from snag_uid_seq),
trade/severity/status triage axes, and follow-up fields. snag_media links
evidence from media_items; snag_source_messages makes WhatsApp capture
idempotent. Household-shared (no user_id) like finance.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d0e1f2a3b4c5"
down_revision: Union[str, Sequence[str], None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS snag_uid_seq START 1")

    op.create_table(
        "snags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uid", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("room", sa.String(length=100), nullable=False),
        sa.Column("element", sa.String(length=200), nullable=True),
        sa.Column("trade", sa.String(length=30), nullable=False, server_default="unknown"),
        sa.Column("severity", sa.String(length=20), nullable=False, server_default="minor"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("reported_by", sa.String(length=100), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("reported_to_trade_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_ref", sa.String(length=200), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_snags_uid", "snags", ["uid"], unique=True)
    op.create_index("ix_snags_room", "snags", ["room"])
    op.create_index("ix_snags_trade", "snags", ["trade"])
    op.create_index("ix_snags_severity", "snags", ["severity"])
    op.create_index("ix_snags_status", "snags", ["status"])
    op.create_index("ix_snags_reported_at", "snags", ["reported_at"])

    op.create_table(
        "snag_media",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("snag_id", sa.Integer(), sa.ForeignKey("snags.id", ondelete="CASCADE"), nullable=False),
        sa.Column("media_item_id", sa.Integer(), sa.ForeignKey("media_items.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("vault_path", sa.String(length=1000), nullable=True),
        sa.UniqueConstraint("snag_id", "media_item_id", name="uq_snag_media"),
    )
    op.create_index("ix_snag_media_snag_id", "snag_media", ["snag_id"])
    op.create_index("ix_snag_media_media_item_id", "snag_media", ["media_item_id"])

    op.create_table(
        "snag_source_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("message_ref", sa.String(length=200), nullable=False),
        sa.Column("snag_id", sa.Integer(), sa.ForeignKey("snags.id", ondelete="CASCADE"), nullable=False),
        sa.UniqueConstraint("message_ref", name="uq_snag_source_messages_message_ref"),
    )
    op.create_index("ix_snag_source_messages_message_ref", "snag_source_messages", ["message_ref"])
    op.create_index("ix_snag_source_messages_snag_id", "snag_source_messages", ["snag_id"])


def downgrade() -> None:
    for idx, table in (
        ("ix_snag_source_messages_snag_id", "snag_source_messages"),
        ("ix_snag_source_messages_message_ref", "snag_source_messages"),
        ("ix_snag_media_media_item_id", "snag_media"),
        ("ix_snag_media_snag_id", "snag_media"),
        ("ix_snags_reported_at", "snags"),
        ("ix_snags_status", "snags"),
        ("ix_snags_severity", "snags"),
        ("ix_snags_trade", "snags"),
        ("ix_snags_room", "snags"),
        ("ix_snags_uid", "snags"),
    ):
        op.execute(f"DROP INDEX IF EXISTS {idx}")
    op.execute("DROP TABLE IF EXISTS snag_source_messages")
    op.execute("DROP TABLE IF EXISTS snag_media")
    op.execute("DROP TABLE IF EXISTS snags")
    op.execute("DROP SEQUENCE IF EXISTS snag_uid_seq")
