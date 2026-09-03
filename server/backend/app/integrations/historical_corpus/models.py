"""SQLAlchemy models for the historical corpus integration.

Two tables:
  - historical_document:      one row per source file / email thread
  - historical_document_chunk: one row per embeddable chunk

Embeddings themselves live in the unified `embeddings` table via
EmbeddingService, keyed by source='historical_corpus' and
source_id=f'{document_id}:{chunk_index}'. We do NOT duplicate vectors here.
"""

from datetime import date, datetime

from sqlalchemy import (
    ARRAY, Date, DateTime, ForeignKey, Integer, String, Text, func,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coglib import Base


class HistoricalDocument(Base):
    """One extracted source document — a chat file, email thread, PDF, etc."""

    __tablename__ = "historical_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_path: Mapped[str] = mapped_column(String(1000), unique=True, index=True)
    source_type: Mapped[str] = mapped_column(String(40), index=True)
    # whatsapp_txt | email_json | pdf | docx | boq_xlsx | xlsx
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    author: Mapped[str | None] = mapped_column(String(500), nullable=True)
    participants: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    document_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    # The app always passes this explicitly (ingest.default_project_tags()
    # reads the manifest config key), so the server_default is only a
    # belt-and-braces floor for hand-written INSERTs. Was "{riverside}" — a
    # family project name baked into the schema; existing rows keep their
    # original tags as a contemporaneous record and are not rewritten.
    project_tags: Mapped[list[str]] = mapped_column(
        ARRAY(String), server_default="{household}",
    )
    body_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    doc_metadata: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    chunks: Mapped[list["HistoricalDocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
    )


class HistoricalDocumentChunk(Base):
    """One searchable slice of a document. Embedding lives in the unified table."""

    __tablename__ = "historical_document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_hdc_doc_chunk"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("historical_documents.id", ondelete="CASCADE"), index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    chunk_type: Mapped[str] = mapped_column(String(40), index=True)
    breadcrumb: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    chunk_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    chunk_metadata: Mapped[dict] = mapped_column(JSONB, server_default="{}")

    document: Mapped[HistoricalDocument] = relationship(back_populates="chunks")
