"""media_items — WhatsApp media store index

One row per image/video/audio message. Scan indexes metadata for all media;
the store step downloads recent items to the volume-mounted media root and
records storage_path + sha256. See app/integrations/media/.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "media_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("message_ref", sa.String(length=200), nullable=False),
        sa.Column("media_type", sa.String(length=20), nullable=False),
        sa.Column("mime_type", sa.String(length=200), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("sender_name", sa.String(length=500), nullable=True),
        sa.Column("chat_or_thread", sa.String(length=500), nullable=True),
        sa.Column("is_from_me", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("message_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="indexed"),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("storage_path", sa.String(length=1000), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", "source", "message_ref", name="uq_media_item"),
    )
    op.create_index("ix_media_items_user_id", "media_items", ["user_id"])
    op.create_index("ix_media_items_source", "media_items", ["source"])
    op.create_index("ix_media_items_message_ref", "media_items", ["message_ref"])
    op.create_index("ix_media_items_media_type", "media_items", ["media_type"])
    op.create_index("ix_media_items_message_ts", "media_items", ["message_ts"])
    op.create_index("ix_media_items_status", "media_items", ["status"])


def downgrade() -> None:
    for idx in (
        "ix_media_items_status",
        "ix_media_items_message_ts",
        "ix_media_items_media_type",
        "ix_media_items_message_ref",
        "ix_media_items_source",
        "ix_media_items_user_id",
    ):
        op.execute(f"DROP INDEX IF EXISTS {idx}")
    op.execute("DROP TABLE IF EXISTS media_items")
