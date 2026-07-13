"""historical corpus tables

Adds historical_documents + historical_document_chunks for the renovation
archive backfill. Embeddings live in the unified `embeddings` table, so no
vector column here.

Revision ID: a1b2c3d4e5f6
Revises: 769d0026face
Create Date: 2026-04-21 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "769d0026face"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "historical_documents",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("source_path", sa.String(1000), nullable=False, unique=True),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("author", sa.String(500), nullable=True),
        sa.Column("participants", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("document_date", sa.Date, nullable=True),
        sa.Column(
            "project_tags", postgresql.ARRAY(sa.String()),
            server_default="{renovation}", nullable=False,
        ),
        sa.Column("body_chars", sa.Integer, nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("doc_metadata", postgresql.JSONB, server_default="{}", nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index("ix_histdoc_source_type", "historical_documents", ["source_type"])
    op.create_index("ix_histdoc_document_date", "historical_documents", ["document_date"])
    op.create_index("ix_histdoc_content_hash", "historical_documents", ["content_hash"])
    op.create_index(
        "ix_histdoc_project_tags", "historical_documents", ["project_tags"],
        postgresql_using="gin",
    )

    op.create_table(
        "historical_document_chunks",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "document_id", sa.BigInteger,
            sa.ForeignKey("historical_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("chunk_type", sa.String(40), nullable=False),
        sa.Column("breadcrumb", sa.String(1000), nullable=True),
        sa.Column("chunk_text", sa.Text, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("chunk_metadata", postgresql.JSONB, server_default="{}", nullable=False),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_hdc_doc_chunk"),
    )
    op.create_index("ix_hdc_document_id", "historical_document_chunks", ["document_id"])
    op.create_index("ix_hdc_chunk_type", "historical_document_chunks", ["chunk_type"])
    op.create_index("ix_hdc_content_hash", "historical_document_chunks", ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_hdc_content_hash", table_name="historical_document_chunks")
    op.drop_index("ix_hdc_chunk_type", table_name="historical_document_chunks")
    op.drop_index("ix_hdc_document_id", table_name="historical_document_chunks")
    op.drop_table("historical_document_chunks")

    op.drop_index("ix_histdoc_project_tags", table_name="historical_documents")
    op.drop_index("ix_histdoc_content_hash", table_name="historical_documents")
    op.drop_index("ix_histdoc_document_date", table_name="historical_documents")
    op.drop_index("ix_histdoc_source_type", table_name="historical_documents")
    op.drop_table("historical_documents")
