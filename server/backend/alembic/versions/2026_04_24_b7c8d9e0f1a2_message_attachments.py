"""message attachments

Adds message_attachments table for the attachments integration — metadata-only
rows detected by scanning WhatsApp and Gmail messages for file attachments,
user-gated ingest into historical_documents.

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-04-24 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "message_attachments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("message_ref", sa.String(200), nullable=False),
        sa.Column("filename", sa.String(500), nullable=True),
        sa.Column("mime_type", sa.String(200), nullable=True),
        sa.Column("size_bytes", sa.Integer, nullable=True),
        sa.Column("sender_name", sa.String(500), nullable=True),
        sa.Column("chat_or_thread", sa.String(500), nullable=True),
        sa.Column("message_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "parse_status", sa.String(20),
            server_default="pending", nullable=False,
        ),
        sa.Column("skip_reason", sa.Text, nullable=True),
        sa.Column("storage_path", sa.String(1000), nullable=True),
        sa.Column(
            "historical_doc_id", sa.BigInteger,
            sa.ForeignKey("historical_documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "source", "message_ref", "filename", name="uq_msg_attachment",
        ),
    )
    op.create_index("ix_message_attachments_source", "message_attachments", ["source"])
    op.create_index("ix_message_attachments_message_ref", "message_attachments", ["message_ref"])
    op.create_index("ix_message_attachments_message_ts", "message_attachments", ["message_ts"])
    op.create_index("ix_message_attachments_parse_status", "message_attachments", ["parse_status"])
    op.create_index("ix_message_attachments_historical_doc_id", "message_attachments", ["historical_doc_id"])


def downgrade() -> None:
    op.drop_index("ix_message_attachments_historical_doc_id", table_name="message_attachments")
    op.drop_index("ix_message_attachments_parse_status", table_name="message_attachments")
    op.drop_index("ix_message_attachments_message_ts", table_name="message_attachments")
    op.drop_index("ix_message_attachments_message_ref", table_name="message_attachments")
    op.drop_index("ix_message_attachments_source", table_name="message_attachments")
    op.drop_table("message_attachments")
