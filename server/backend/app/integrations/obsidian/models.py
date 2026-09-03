"""SQLAlchemy models for the Obsidian vault integration.

VaultChunk tracks file hashes and modification times for incremental indexing.
Embedding vectors are stored in the unified 'embeddings' table via EmbeddingService.

Per-user since 2026-07-25. Vaults are one-per-user on disk (`/vaults/<user>/`),
so the tracker must be too — otherwise two vaults holding `Inbox/note.md`
collide on `path` and the incremental indexer thrashes between them. The same
change makes vault embeddings user-owned rather than household-shared; see the
`user_id` note on EmbeddingQueue.
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import SourcedRecordMixin, UserOwnedMixin


class VaultChunk(UserOwnedMixin, SourcedRecordMixin, Base):
    __tablename__ = "vault_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(String(500), index=True)
    file_hash: Mapped[str] = mapped_column(String(64))
    modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # user_id inherited from UserOwnedMixin
    # synced_at, source_id, source_ts, content_hash inherited from SourcedRecordMixin
    # source_id maps to path, source_ts maps to modified_at, content_hash maps to file_hash

    __table_args__ = (
        UniqueConstraint("user_id", "path", name="uq_vault_chunks_user_path"),
        Index("ix_vault_chunks_user_modified_at", "user_id", "modified_at"),
    )
