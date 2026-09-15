"""Vault indexing — scan markdown files, enqueue for embedding via unified pipeline.

Ported from .tools/embed.py. Now uses the unified EmbeddingService queue
instead of embedding inline. The background worker handles actual embedding.

Large, list-structured files (e.g. `Task Backlog.md`) are chunked by
heading/bullet rather than embedded whole — see `chunking.py` for the
boundary rule and why. Everything below folds `CHUNKER_VERSION` into the
stored file hash (never the raw content hash alone), the same trick
`CLEANER_VERSION` uses in the embedding pipeline: bumping the chunker
version invalidates every tracked file without a schema migration, so the
next index run re-chunks and re-enqueues from scratch.
"""

import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.integrations.obsidian.chunking import (
    CHUNKER_VERSION,
    chunk_file,
    strip_frontmatter,
)
from app.integrations.obsidian.models import VaultChunk
from app.services.embedding import Embedding, EmbeddingQueue, EmbeddingService
from app.services.text import ILIKE_ESCAPE_CHAR, escape_ilike

logger = logging.getLogger(__name__)

# Folders to skip.
#
# `.stversions` was added 2026-08-14 and is the reason this comment exists.
# It is Syncthing's version history: a rolling snapshot of *every save* of
# every file. Indexing it does two distinct kinds of harm, and the second is
# the serious one:
#
#   1. It inflates the index and the embedding bill with content that is by
#      definition a duplicate.
#   2. Every superseded revision competes with its own live file for the same
#      query. Measured 2026-08-13: the query "Zappi CT clamp solar and Huawei
#      installer access Modbus" returned a 1 August snapshot at 0.7651 *above*
#      the live file it is a snapshot of at 0.7600. Search was returning a
#      record of past belief in preference to current truth.
#
# Excluding the directory is a tourniquet, not a cure — the general fix is
# provenance and recency decay in the ranking, so a live file beats its own
# snapshot by construction rather than by an exclusion list someone has to
# maintain. Tracked in the project backlog.
SKIP_DIRS = {
    ".obsidian", ".claude", ".tools", ".embeddings", ".trash", ".git",
    ".stversions", "Attachments", "Templates",
}


def is_indexable(rel_path: str | Path) -> bool:
    """Should this vault-relative path be indexed at all?

    The single predicate every entry point defers to. Each caller used to
    re-write the set-membership check inline, which meant the rule held only
    where somebody remembered to write it — and `POST /api/v1/vault/push`
    never did, so a daemon could push a `.stversions` file straight past the
    walker's filter.
    """
    parts = Path(rel_path).parts
    if not parts or not str(parts[-1]).endswith(".md"):
        return False
    return not any(part in SKIP_DIRS for part in parts)


def normalise_vault_path(rel_path: str) -> str:
    """NFC-normalise a vault-relative path. The index key must not depend on OS.

    macOS stores filenames decomposed (NFD): `Gráda` is written to APFS as
    `Gra` + U+0301, and re-normalising the *file* does not help — the
    filesystem simply decomposes it again on write. Linux/ext4 stores whatever
    bytes it is handed.

    So the same note reaches the index by two routes with two different byte
    strings: the Mac daemon pushes the NFD path it read locally, while the
    server's own scan sees whatever Syncthing wrote. Both are used raw as
    `source_id`, so one file becomes two indexed documents — it then matches
    *itself* at cosine 1.0, occupies two slots in every search, and leaves a
    phantom copy that never updates when the note is edited.

    Observed 2026-08-15 on `Wine — Rosés to Try.md` and, minutes after it was
    created, `CV - Alex O'Gráda 2026-06.md`. Only two files today, but any
    Irish fada — á é í ó ú, which the People notes are full of — re-creates it.

    Normalising here rather than renaming files is the fix, because the
    filename is not something we control and the index key is.
    """
    return unicodedata.normalize("NFC", rel_path)


_FRONTMATTER_STATUS = re.compile(
    r"\A---\r?\n(.*?)^---\r?\n", re.S | re.M
)
_STATUS_LINE = re.compile(r"^status:\s*[\"']?([^\"'\n#]+?)[\"']?\s*$", re.M)


def frontmatter_status(content: str) -> str | None:
    """The note's `status:` frontmatter value, lowercased, or None.

    Hand-parsed rather than via PyYAML on purpose: `yaml` is importable in the
    image only as somebody else's transitive dependency, it is not in
    `requirements.txt`, and a one-field read does not justify making it a real
    one. This looks *only* inside the leading `---` block, so a `status:` line
    in prose or a code fence cannot be mistaken for frontmatter.

    Deliberately not validated against a vocabulary here. The index records
    what the note says; the vocabulary is a vault convention, and a filter that
    silently dropped an unrecognised value would hide exactly the drift that
    convention exists to catch. `vault_search` surfaces whatever is stored.
    """
    m = _FRONTMATTER_STATUS.match(content or "")
    if not m:
        return None
    s = _STATUS_LINE.search(m.group(1))
    if not s:
        return None
    value = s.group(1).strip().lower()
    return value or None


_HISTORY_STATUSES = {"done", "superseded"}
# Path segments that mark a note as a historical record rather than current
# truth, independent of frontmatter `status`. `.stversions` is already
# excluded from indexing entirely (SKIP_DIRS) — listed here too so the
# provenance rule stays correct if that exclusion is ever loosened, and so
# `is_history_chunk` documents the *complete* rule in one place rather than
# splitting it across SKIP_DIRS and this set.
_HISTORY_PATH_PARTS = {"Archive", ".stversions"}


def is_history_chunk(rel_path: str, status: str | None) -> bool:
    """R4: is this chunk a superseded/archived record rather than current truth?

    True for a `done`/`superseded` frontmatter status, or a path under an
    `Archive/`/`.stversions/` folder. Feeds `EmbeddingService.search()`'s
    recency decay (an extra fixed penalty on top of age-based decay) — the
    generalisation of the `.stversions` exclusion this project's `sync.py`
    SKIP_DIRS comment names as a tourniquet, not a cure.
    """
    if status in _HISTORY_STATUSES:
        return True
    return any(part in _HISTORY_PATH_PARTS for part in Path(rel_path).parts)


def _chunk_metadata(
    modified_iso: str, heading_path, status: str | None, is_history: bool,
) -> str:
    """The metadata blob stored alongside a chunk.

    `status` rides here rather than in `chunk_text` because it must be
    *filterable* without being *embedded*: putting "status: active" into the
    vector would let a note's lifecycle bleed into its semantic position, which
    is not what anyone means by similarity. The column already exists and the
    vectors live in separate per-space tables, so adding this is a metadata
    write with no re-embedding and no schema change.

    R4: `source_date` and `is_history` are the provenance fields
    `EmbeddingService.search()` reads for staleness labelling and recency
    decay. `source_date` is the file's mtime (same value as `modified_at`,
    kept as a separate key rather than a rename so nothing reading
    `modified_at` today needs to change).
    """
    meta = {
        "modified_at": modified_iso,
        "source_date": modified_iso,
        "heading_path": heading_path,
        "is_history": is_history,
    }
    if status:
        meta["status"] = status
    return json.dumps(meta)


def refresh_status_metadata(session: Session, vault_path: str, user_id: int) -> int:
    """Bring stored `metadata_json.status` into line with the notes on disk.

    **This is now the only path by which a status change reaches the index.**
    Change detection keys on `_body_hash`, which excludes frontmatter, so
    editing `status:` no longer marks the file as changed at all — deliberately,
    because the body is what gets embedded. That makes this function load-
    bearing rather than a one-off backfill: it runs on every scan and is a no-op
    for everything it does not need to fix.

    **This costs nothing.** It rewrites `metadata_json` on the `embeddings` row
    and touches no vector: the per-space tables are keyed by `embedding_id` and
    are not read here, so nothing is re-embedded and no API call is made. Same
    structural rule the similarity surface is built on.

    Returns the number of chunk rows updated.
    """
    vp = Path(vault_path)
    if not vp.is_dir():
        return 0

    on_disk: dict[str, str | None] = {}
    for rel_path, full_path in _iter_vault_files(vp):
        try:
            head = full_path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        on_disk[rel_path] = frontmatter_status(head)

    rows = (
        session.query(Embedding.id, Embedding.source_id, Embedding.metadata_json)
        .filter(Embedding.source == "vault", Embedding.user_id == user_id)
        .all()
    )

    updated = 0
    for row_id, source_id, meta_json in rows:
        doc = source_id.split("#", 1)[0]
        if doc not in on_disk:
            continue
        want = on_disk[doc]
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except (TypeError, ValueError):
            meta = {}
        # `is_history` depends on `status` too (done/superseded), so a status
        # flip must re-derive it here — otherwise a note marked `superseded`
        # today keeps whatever is_history value indexing computed yesterday.
        want_is_history = is_history_chunk(doc, want)
        if meta.get("status") == want and meta.get("is_history") == want_is_history:
            continue
        if want is None:
            meta.pop("status", None)
        else:
            meta["status"] = want
        meta["is_history"] = want_is_history
        session.query(Embedding).filter(Embedding.id == row_id).update(
            {"metadata_json": json.dumps(meta)}, synchronize_session=False
        )
        updated += 1

    if updated:
        session.flush()
        logger.info("Vault status metadata: refreshed %d chunk(s)", updated)
    return updated


def _iter_vault_files(vault_path: Path):
    """Yield (relative_path, full_path) for all .md files worth indexing."""
    for p in sorted(vault_path.rglob("*.md")):
        rel = p.relative_to(vault_path)
        if not is_indexable(rel):
            continue
        yield normalise_vault_path(str(rel)), p


def _body_hash(content: str) -> str:
    """Hash of the *embeddable* body — frontmatter excluded. Unsalted.

    Change detection must key on what actually gets embedded. It used to hash
    the whole file, while `chunk_file` embeds only `strip_frontmatter(content)`,
    so any frontmatter-only edit looked like a content change: bumping
    `modified:`, editing a tag, flipping `status:`. Each one re-chunked the file
    and re-ran the enqueue path over every chunk.

    That never cost an API call — `EmbeddingService.enqueue` dedups on
    `(source, source_id, user_id, content_hash)` and the cleaned text was
    identical — but it is pointless work on a hot path. `/daily-note` rewrites
    `modified:` every morning, and `Task Backlog.md` alone chunks into dozens of
    rows, each getting a wasted lookup.

    Frontmatter changes that *do* matter are handled where they belong:
    `refresh_status_metadata()` syncs `status` into the embedding metadata
    without touching a vector.
    """
    # `.strip()` here, not in `strip_frontmatter`: that function feeds the
    # chunk text and therefore `content_hash`, so normalising it would re-embed
    # the corpus. It also strips only when frontmatter was present, so without
    # this a note *gaining* frontmatter would look changed on that basis alone.
    return hashlib.md5(strip_frontmatter(content).strip().encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    """Body hash for a file on disk. Callers store the versioned form."""
    return _body_hash(path.read_text(encoding="utf-8", errors="replace"))


def _versioned_hash(raw_hash: str) -> str:
    """Fold the chunker version into the stored hash.

    Both entry points (the full vault scan below, and `index_single_file`,
    which receives an externally-computed hash from the daemon/watcher) run
    every stored/compared hash through this, so a `CHUNKER_VERSION` bump
    invalidates every file's cache regardless of which path reindexed it
    last — no schema change, no separate "chunker version" column.
    """
    return f"{raw_hash}:cv{CHUNKER_VERSION}"


def _delete_stale_vault_chunks(
    session: Session, user_id: int, rel_path: str, valid_source_ids: set[str],
) -> int:
    """Remove embedding/queue rows for one file's old chunk boundaries.

    A file's chunk `source_id`s can change shape between index runs: a file
    crossing the chunking threshold goes from one bare-path id to several
    `{path}#{n}` ids; a re-chunk (edit, or a `CHUNKER_VERSION` bump) can
    shrink or grow the chunk count. Anything previously stored for this
    file that isn't in the freshly computed `valid_source_ids` is stale and
    must be deleted explicitly — `EmbeddingService.enqueue`'s dedup-by-key
    only ever touches the exact `(source, source_id)` it's given, so an old
    chunk index that the new chunking no longer produces would otherwise
    sit in the table forever, still searchable.
    """
    escaped_prefix = escape_ilike(rel_path) + "#"
    existing_ids = {
        row[0]
        for row in session.query(Embedding.source_id)
        .filter(Embedding.source == "vault", Embedding.user_id == user_id)
        .filter(
            or_(
                Embedding.source_id == rel_path,
                Embedding.source_id.ilike(f"{escaped_prefix}%", escape=ILIKE_ESCAPE_CHAR),
            )
        )
        .all()
    }
    stale = existing_ids - valid_source_ids
    if not stale:
        return 0

    session.query(Embedding).filter(
        Embedding.source == "vault",
        Embedding.user_id == user_id,
        Embedding.source_id.in_(stale),
    ).delete(synchronize_session=False)
    session.query(EmbeddingQueue).filter(
        EmbeddingQueue.source == "vault",
        EmbeddingQueue.user_id == user_id,
        EmbeddingQueue.source_id.in_(stale),
        EmbeddingQueue.status == "pending",
    ).delete(synchronize_session=False)
    session.flush()
    logger.info(
        "Vault chunking: cleaned up %d stale chunk(s) for %s (user %s)",
        len(stale), rel_path, user_id,
    )
    return len(stale)


def index_vault(session: Session, vault_path: str, user_id: int) -> dict:
    """Incrementally index one user's vault — enqueue changed files.

    Tracks file hashes in the VaultChunk table for change detection.
    Enqueues new/changed files into the unified embedding queue, owned by
    `user_id` so semantic search never crosses vaults.
    The background worker will embed them.

    Returns stats: files_total, reused, enqueued, chunks_enqueued, removed,
    stale_chunks_removed.
    """
    vp = Path(vault_path)
    if not vp.is_dir():
        logger.warning(f"Vault path does not exist: {vault_path}")
        return {"error": f"Vault path not found: {vault_path}"}

    # Load existing hashes from DB — this user's vault only
    existing = {
        row.path: {"hash": row.file_hash, "id": row.id}
        for row in session.query(VaultChunk.id, VaultChunk.path, VaultChunk.file_hash)
        .filter(VaultChunk.user_id == user_id)
        .all()
    }

    # Scan vault
    current_files = list(_iter_vault_files(vp))
    current_paths = {rel for rel, _ in current_files}

    # Determine what needs work
    to_enqueue = []  # (rel_path, versioned_hash, content, modified_at)
    reused = 0

    for rel_path, full_path in current_files:
        versioned_hash = _versioned_hash(_file_hash(full_path))
        if rel_path in existing and existing[rel_path]["hash"] == versioned_hash:
            reused += 1
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
            mtime = datetime.fromtimestamp(full_path.stat().st_mtime, tz=timezone.utc)
            to_enqueue.append((rel_path, versioned_hash, content, mtime))
        except Exception as e:
            logger.warning(f"Skip {rel_path}: {e}")

    # Remove embeddings and chunks for deleted files. Every chunk id a
    # removed file could have produced (bare path or `{path}#{n}`) is stale
    # by definition — pass an empty valid set.
    removed_paths = set(existing.keys()) - current_paths
    stale_chunks_removed = 0
    if removed_paths:
        for path in removed_paths:
            stale_chunks_removed += _delete_stale_vault_chunks(
                session, user_id, path, valid_source_ids=set(),
            )
        (
            session.query(VaultChunk)
            .filter(VaultChunk.user_id == user_id, VaultChunk.path.in_(removed_paths))
            .delete(synchronize_session=False)
        )
        session.flush()

    logger.info(
        f"Vault index: {len(current_files)} files, "
        f"{reused} reused, {len(to_enqueue)} to enqueue, {len(removed_paths)} removed"
    )

    # Enqueue new/changed files and update VaultChunk tracking
    enqueued = 0
    chunks_enqueued = 0
    if to_enqueue:
        now = datetime.now(timezone.utc)
        for rel_path, versioned_hash, content, mtime in to_enqueue:
            chunks = chunk_file(rel_path, content)
            valid_ids = {c.source_id for c in chunks}
            stale_chunks_removed += _delete_stale_vault_chunks(
                session, user_id, rel_path, valid_source_ids=valid_ids,
            )

            status = frontmatter_status(content)
            is_history = is_history_chunk(rel_path, status)
            for c in chunks:
                metadata = _chunk_metadata(mtime.isoformat(), c.heading_path, status, is_history)
                EmbeddingService.enqueue(
                    session, "vault", c.source_id, c.text, metadata, user_id=user_id,
                )
                chunks_enqueued += 1
            enqueued += 1

            # Update VaultChunk tracking record (hash + mtime, no embedding —
            # still one row per file regardless of chunk count).
            if rel_path in existing:
                chunk = session.query(VaultChunk).get(existing[rel_path]["id"])
                chunk.file_hash = versioned_hash
                chunk.modified_at = mtime
                chunk.indexed_at = now
            else:
                chunk = VaultChunk(
                    user_id=user_id,
                    path=rel_path,
                    file_hash=versioned_hash,
                    modified_at=mtime,
                )
                session.add(chunk)

        session.commit()

    # Free: rewrites metadata only, never a vector. Runs every scan so a
    # frontmatter status edit lands even on a file the hash check reused.
    status_refreshed = refresh_status_metadata(session, vault_path, user_id)

    return {
        "files_total": len(current_files),
        "reused": reused,
        "enqueued": enqueued,
        "chunks_enqueued": chunks_enqueued,
        "removed": len(removed_paths),
        "stale_chunks_removed": stale_chunks_removed,
        "status_refreshed": status_refreshed,
    }


def index_single_file(
    session: Session, rel_path: str, content: str, file_hash: str, user_id: int,
) -> None:
    """Re-index a single vault file pushed by a daemon or seen by the watcher.

    Enqueues the file for embedding via the unified pipeline, owned by
    `user_id` — the vault it came from.

    `file_hash` arrives from the caller (client daemon / server-side
    watcher), computed independently of `_file_hash` above — it's an
    unsalted content hash, "advisory" per the `/vault/push` API. It's run
    through the same `_versioned_hash` fold as the full-scan path so a
    `CHUNKER_VERSION` bump forces a re-chunk here too, regardless of which
    entry point last touched the file.

    Exclusion is enforced *here*, not only in the callers. This is the one
    point every path funnels through — the full scan, the server-side
    watcher, and `POST /api/v1/vault/push` — so a rule applied here cannot
    be bypassed by a caller that forgot it. The filters in the two watchers
    are kept as an optimisation (they save a pointless network round-trip
    and a DB hit), never as the guarantee.
    """
    if not is_indexable(rel_path):
        logger.debug("index_single_file: skipping excluded path %s", rel_path)
        return

    # Same chokepoint argument as the exclusion above: this is the one function
    # every index route funnels through, so normalising the key here is what
    # makes the daemon's NFD path and the server scan's NFC path resolve to a
    # single document instead of two. See normalise_vault_path.
    rel_path = normalise_vault_path(rel_path)

    # Recomputed from the body rather than trusting the caller's hash, which
    # is whole-file and therefore moves when only frontmatter does. The API
    # documents `file_hash` as advisory; this is the one place that matters.
    versioned_hash = _versioned_hash(_body_hash(content))
    existing = (
        session.query(VaultChunk)
        .filter_by(user_id=user_id, path=rel_path)
        .first()
    )

    # Skip if hash hasn't changed
    if existing and existing.file_hash == versioned_hash:
        return

    now = datetime.now(timezone.utc)
    chunks = chunk_file(rel_path, content)
    valid_ids = {c.source_id for c in chunks}
    _delete_stale_vault_chunks(session, user_id, rel_path, valid_source_ids=valid_ids)

    status = frontmatter_status(content)
    is_history = is_history_chunk(rel_path, status)
    for c in chunks:
        metadata = _chunk_metadata(now.isoformat(), c.heading_path, status, is_history)
        EmbeddingService.enqueue(
            session, "vault", c.source_id, c.text, metadata, user_id=user_id,
        )

    # Update tracking record
    if existing:
        existing.file_hash = versioned_hash
        existing.modified_at = now
        existing.indexed_at = now
    else:
        chunk = VaultChunk(
            user_id=user_id,
            path=rel_path,
            file_hash=versioned_hash,
            modified_at=now,
        )
        session.add(chunk)

    session.commit()
    logger.info(
        f"Single-file enqueued for user {user_id}: {rel_path} "
        f"({len(chunks)} chunk(s))"
    )
