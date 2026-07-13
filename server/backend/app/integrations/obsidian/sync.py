"""Vault indexing — scan markdown files, enqueue for embedding via unified pipeline.

Ported from .tools/embed.py. Now uses the unified EmbeddingService queue
instead of embedding inline. The background worker handles actual embedding.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.integrations.obsidian.models import VaultChunk
from app.services.embedding import EmbeddingService

logger = logging.getLogger(__name__)

# Folders to skip
SKIP_DIRS = {".obsidian", ".claude", ".tools", ".embeddings", ".trash", ".git", "Attachments", "Templates"}


def _iter_vault_files(vault_path: Path):
    """Yield (relative_path, full_path) for all .md files worth indexing."""
    for p in sorted(vault_path.rglob("*.md")):
        parts = p.relative_to(vault_path).parts
        if any(part in SKIP_DIRS for part in parts):
            continue
        yield str(p.relative_to(vault_path)), p


def _file_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _prepare_chunk(rel_path: str, content: str) -> str:
    """Build the text chunk: path + first ~2000 chars of content (minus frontmatter)."""
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:].strip()
    content = content[:2000]
    return f"{rel_path}\n\n{content}"


def index_vault(session: Session, vault_path: str) -> dict:
    """Incrementally index the vault — enqueue changed files for embedding.

    Tracks file hashes in the VaultChunk table for change detection.
    Enqueues new/changed files into the unified embedding queue.
    The background worker will embed them.

    Returns stats: files_total, reused, enqueued, removed.
    """
    vp = Path(vault_path)
    if not vp.is_dir():
        logger.warning(f"Vault path does not exist: {vault_path}")
        return {"error": f"Vault path not found: {vault_path}"}

    # Load existing hashes from DB
    existing = {
        row.path: {"hash": row.file_hash, "id": row.id}
        for row in session.query(VaultChunk.id, VaultChunk.path, VaultChunk.file_hash).all()
    }

    # Scan vault
    current_files = list(_iter_vault_files(vp))
    current_paths = {rel for rel, _ in current_files}

    # Determine what needs work
    to_enqueue = []  # (rel_path, hash, chunk_text, modified_at)
    reused = 0

    for rel_path, full_path in current_files:
        h = _file_hash(full_path)
        if rel_path in existing and existing[rel_path]["hash"] == h:
            reused += 1
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
            chunk = _prepare_chunk(rel_path, content)
            mtime = datetime.fromtimestamp(full_path.stat().st_mtime, tz=timezone.utc)
            to_enqueue.append((rel_path, h, chunk, mtime))
        except Exception as e:
            logger.warning(f"Skip {rel_path}: {e}")

    # Remove embeddings and chunks for deleted files
    removed_paths = set(existing.keys()) - current_paths
    if removed_paths:
        for path in removed_paths:
            EmbeddingService.delete_source(session, "vault", path)
        session.query(VaultChunk).filter(VaultChunk.path.in_(removed_paths)).delete(synchronize_session=False)
        session.flush()

    logger.info(
        f"Vault index: {len(current_files)} files, "
        f"{reused} reused, {len(to_enqueue)} to enqueue, {len(removed_paths)} removed"
    )

    # Enqueue new/changed files and update VaultChunk tracking
    enqueued = 0
    if to_enqueue:
        now = datetime.now(timezone.utc)
        for rel_path, h, chunk_text, mtime in to_enqueue:
            metadata = json.dumps({"modified_at": mtime.isoformat()})
            EmbeddingService.enqueue(session, "vault", rel_path, chunk_text, metadata)
            enqueued += 1

            # Update VaultChunk tracking record (hash + mtime, no embedding)
            if rel_path in existing:
                chunk = session.query(VaultChunk).get(existing[rel_path]["id"])
                chunk.file_hash = h
                chunk.modified_at = mtime
                chunk.indexed_at = now
            else:
                chunk = VaultChunk(
                    path=rel_path,
                    file_hash=h,
                    modified_at=mtime,
                )
                session.add(chunk)

        session.commit()

    return {
        "files_total": len(current_files),
        "reused": reused,
        "enqueued": enqueued,
        "removed": len(removed_paths),
    }


def index_single_file(session: Session, rel_path: str, content: str, file_hash: str) -> None:
    """Re-index a single vault file pushed via gRPC PushVaultFile.

    Enqueues the file for embedding via the unified pipeline.
    """
    existing = (
        session.query(VaultChunk)
        .filter_by(path=rel_path)
        .first()
    )

    # Skip if hash hasn't changed
    if existing and existing.file_hash == file_hash:
        return

    chunk_text = _prepare_chunk(rel_path, content)
    now = datetime.now(timezone.utc)
    metadata = json.dumps({"modified_at": now.isoformat()})

    # Enqueue for embedding
    EmbeddingService.enqueue(session, "vault", rel_path, chunk_text, metadata)

    # Update tracking record
    if existing:
        existing.file_hash = file_hash
        existing.modified_at = now
        existing.indexed_at = now
    else:
        chunk = VaultChunk(
            path=rel_path,
            file_hash=file_hash,
            modified_at=now,
        )
        session.add(chunk)

    session.commit()
    logger.info(f"Single-file enqueued: {rel_path}")
