"""SQLAlchemy models for the historical corpus integration.

Two tables:
  - historical_document:      one row per source file / email thread
  - historical_document_chunk: one row per embeddable chunk

Embeddings themselves live in the unified `embeddings` table via
EmbeddingService, keyed by source='historical_corpus' and
source_id=f'{document_id}:{chunk_index}'. We do NOT duplicate vectors here.

Ownership (2026-09-06): `historical_documents.owner_user_id` is nullable.
NULL means household-shared — manuals, renovation paperwork, comms archive,
the things both people are meant to find. A set owner means the document is
private to that user; today that is every `claude_conversation` (Alex's
claude.ai export, owned by user 1). The column records the decision; the
enforcement is that an owned document's chunks are embedded with
`embeddings.user_id = owner`, so `EmbeddingService.search`'s existing
`user_id IS NULL OR user_id = caller` clause keeps them out of every other
caller's results with no corpus-specific query logic. `tools._enrich_hits`
re-checks the owner on the hydrated row as belt-and-braces.

Dedup (2026-09-07, issue #148): `raw_content_hash` is SHA256 of the raw
source *bytes* — distinct from `content_hash`, which hashes the parsed
chunk text and exists to skip re-embedding an unchanged file at the same
`source_path`. `raw_content_hash` catches the same bytes arriving under a
*different* `source_path` (a re-saved copy, a renamed export). It's
nullable: rows ingested before this column existed may never get a value
if their source file is gone by the time the backfill migration runs.
Scoping matches `owner_user_id` — a hash only dedups within the same owner,
never across users, and a NULL-owner (household) document only dedups
against another NULL-owner document. See `ingest.py::_find_duplicate`.
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
    # The set of values is parsers/types.py's KNOWN_SOURCE_TYPES — listed once,
    # there; a test holds that tuple to what the producers actually write.
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
    # SHA256 of the raw source bytes — see the module docstring's Dedup
    # section. Nullable: NULL means "not computed" (pre-migration row whose
    # source file is gone), never "empty file" (empty bytes still hash).
    raw_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    doc_metadata: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    # NULL = household-shared (the default, and the right answer for every
    # source type except a personal export). See the module docstring.
    owner_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True,
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
