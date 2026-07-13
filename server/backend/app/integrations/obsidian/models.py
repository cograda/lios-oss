"""SQLAlchemy models for the Obsidian vault integration.

VaultChunk tracks file hashes and modification times for incremental indexing.
Embedding vectors are stored in the unified 'embeddings' table via EmbeddingService.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import SourcedRecordMixin


class VaultChunk(SourcedRecordMixin, Base):
    __tablename__ = "vault_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(String(500), index=True)
    file_hash: Mapped[str] = mapped_column(String(64))
    modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # synced_at, source_id, source_ts, content_hash inherited from SourcedRecordMixin
    # source_id maps to path, source_ts maps to modified_at, content_hash maps to file_hash
