"""Orchestrator for historical corpus ingestion.

Pipeline: scan → parse → upsert doc → upsert chunks → enqueue for embedding.

Idempotent: re-runs skip files whose SHA256(body) is unchanged. Chunks use the
same hash on their own text, so changing one chunk doesn't re-embed the whole
doc (the unified embedding queue already dedupes on content_hash per source_id).

Entry points:
  - ingest_path(session, path, project_tags)  — one file
  - ingest_root(session, root, project_tags, limit_per_type) — whole corpus
  - ingest_claude_export(session, paths, owner_user_id=...) — one person's
    claude.ai export; the only entry point that stamps an owner

Ownership: `_upsert_document(owner_user_id=...)` records the owner on the
document AND enqueues its chunks with `user_id=owner`, which is the half that
makes search private — `EmbeddingService.search` already excludes another
user's rows. Every entry point except the Claude export leaves it None
(household-shared), which is the corpus's default and the right answer for
manuals, renovation paperwork and the comms archive.

Dedup by raw content hash (2026-09-07, issue #148): `ingest_path` hashes the
raw source bytes *before* dispatching to a parser and checks
`_find_duplicate` first. A match — same bytes, same owner scope, a
different `source_path` — skips parsing and embedding entirely and returns
the existing document with `deduplicated: true`, recording the new path as
an alternate source on the existing row's metadata. This is distinct from
`_upsert_document`'s existing same-`source_path` re-run skip (which compares
`content_hash`, the parsed text, not the raw bytes) — that one already
worked; this one is for the same file arriving under a new name or path.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.integrations.historical_corpus.models import (
    HistoricalDocument, HistoricalDocumentChunk,
)
from app.integrations.historical_corpus.parsers import ChunkRecord, DocMeta
from app.integrations.historical_corpus.parsers import (
    boq as boq_parser,
    claude_export as claude_export_parser,
    docx as docx_parser,
    email_json as email_parser,
    manual as manual_parser,
    pdf as pdf_parser,
    voice_memo as voice_memo_parser,
    whatsapp as whatsapp_parser,
)
from app.plugin.config_store import plugin_config
from app.services.embedding import EmbeddingService

logger = logging.getLogger(__name__)

EMBEDDING_SOURCE = "historical_corpus"

WHATSAPP_ROOT = "Comms Archive/WhatsApp Conversations"
EMAIL_JSON_NAME = "email_conversations.json"
VOICE_MEMO_ROOT = "Voice Memos"  # staged keeper transcripts (.md) from sandbox/voice-memos
SKIP_DIRS = {"Archive"}  # lower priority per plan doc


def default_project_tags() -> list[str]:
    """Project tag(s) to apply when the caller doesn't specify any.

    Deployment config, not a code constant — see the `default_project_tag`
    key in this integration's manifest. Previously the literal "riverside"
    (a family renovation project) hardcoded at four call sites.
    """
    return [plugin_config("historical_corpus").default_project_tag]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _find_duplicate(
    session: Session, raw_content_hash: str, owner_user_id: int | None,
) -> HistoricalDocument | None:
    """Look up an existing document with the same raw bytes, scoped to the
    same owner (see the module docstring's Dedup section and the model's).

    NULL-owner (household) only matches another NULL-owner document; a set
    owner only matches that same owner. Never across users — a private
    export and a household copy that happen to share bytes are not the
    same document as far as anyone's search results are concerned.
    """
    q = session.query(HistoricalDocument).filter_by(raw_content_hash=raw_content_hash)
    if owner_user_id is None:
        q = q.filter(HistoricalDocument.owner_user_id.is_(None))
    else:
        q = q.filter(HistoricalDocument.owner_user_id == owner_user_id)
    return q.first()


def _record_alternate_source(session: Session, doc: HistoricalDocument, source_path: str) -> None:
    """Note a second filename/path the same bytes were seen under, without
    touching the document's own `source_path` (the one it was created at)."""
    meta = dict(doc.doc_metadata or {})
    alternates = list(meta.get("alternate_sources") or [])
    if source_path not in alternates:
        alternates.append(source_path)
        meta["alternate_sources"] = alternates
        doc.doc_metadata = meta
        session.commit()


def _scrub(value):
    """Strip NUL bytes (and other Postgres-hostile control chars) from any
    string leaves inside the value. Postgres text columns and jsonb both reject
    U+0000; PDF extractors sometimes leak them from broken text operators."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def _iter_whatsapp(root: Path):
    """Yield one .txt per chat folder (prefer named over internal _chat.txt)."""
    wa_root = root / WHATSAPP_ROOT
    if not wa_root.exists():
        return
    folders: dict[Path, list[Path]] = {}
    for txt in wa_root.rglob("*.txt"):
        folders.setdefault(txt.parent, []).append(txt)
    for folder, txts in folders.items():
        named = [t for t in txts if t.name != "_chat.txt"]
        yield (named[0] if named else txts[0]), folder.name


def _iter_voice_memos(root: Path):
    """Yield staged voice-memo transcript .md files (Voice Memos/ tree)."""
    vm_root = root / VOICE_MEMO_ROOT
    if not vm_root.exists():
        return
    for p in sorted(vm_root.rglob("*.md")):
        if p.is_file():
            yield p


def _iter_file_tree(root: Path, extensions: set[str]):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in extensions:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p


def _upsert_document(
    session: Session,
    *,
    source_path: str,
    meta: DocMeta,
    chunks: list[ChunkRecord],
    project_tags: list[str],
    owner_user_id: int | None = None,
    raw_content_hash: str | None = None,
) -> tuple[HistoricalDocument, bool, int]:
    """Upsert a document and its chunks. Returns (doc, created, chunks_enqueued).

    `owner_user_id=None` (the default) is household-shared. A set owner is
    written to the document row and threaded into `enqueue_batch` as the
    embeddings' `user_id`, so the vector search scopes the chunks to that
    user by the same clause it applies to email and WhatsApp.
    """
    # Scrub NUL bytes from parser output before they hit Postgres.
    meta = DocMeta(
        title=_scrub(meta.title),
        source_type=meta.source_type,
        author=_scrub(meta.author),
        participants=_scrub(meta.participants),
        document_date=meta.document_date,
        metadata=_scrub(meta.metadata) or {},
    )
    chunks = [
        ChunkRecord(
            chunk_type=c.chunk_type,
            chunk_text=_scrub(c.chunk_text),
            breadcrumb=_scrub(c.breadcrumb),
            metadata=_scrub(c.metadata) or {},
        )
        for c in chunks
    ]
    combined_body = "\n\n".join(c.chunk_text for c in chunks) if chunks else ""
    doc_hash = _hash(combined_body)

    existing = (
        session.query(HistoricalDocument)
        .filter_by(source_path=source_path)
        .one_or_none()
    )

    if existing and existing.content_hash == doc_hash:
        return existing, False, 0

    if existing:
        # Content changed — wipe prior chunks so we emit clean state.
        session.query(HistoricalDocumentChunk).filter_by(
            document_id=existing.id
        ).delete(synchronize_session=False)
        doc = existing
        doc.source_type = meta.source_type
        doc.title = meta.title
        doc.author = meta.author
        doc.participants = meta.participants or None
        doc.document_date = meta.document_date
        doc.project_tags = project_tags
        doc.body_chars = len(combined_body)
        doc.content_hash = doc_hash
        doc.doc_metadata = meta.metadata or {}
        doc.owner_user_id = owner_user_id
        if raw_content_hash is not None:
            doc.raw_content_hash = raw_content_hash
        created = False
    else:
        doc = HistoricalDocument(
            source_path=source_path,
            source_type=meta.source_type,
            title=meta.title,
            author=meta.author,
            participants=meta.participants or None,
            document_date=meta.document_date,
            project_tags=project_tags,
            body_chars=len(combined_body),
            content_hash=doc_hash,
            raw_content_hash=raw_content_hash,
            doc_metadata=meta.metadata or {},
            owner_user_id=owner_user_id,
        )
        session.add(doc)
        created = True
    session.flush()

    # Persist chunks + enqueue embeddings.
    embed_items: list[tuple[str, str, str, str | None]] = []
    for idx, c in enumerate(chunks):
        chunk_hash = _hash(c.chunk_text)
        session.add(HistoricalDocumentChunk(
            document_id=doc.id,
            chunk_index=idx,
            chunk_type=c.chunk_type,
            breadcrumb=c.breadcrumb,
            chunk_text=c.chunk_text,
            content_hash=chunk_hash,
            chunk_metadata=c.metadata or {},
        ))
        md = {
            "doc_id": doc.id,
            "source_type": meta.source_type,
            "chunk_type": c.chunk_type,
            "breadcrumb": c.breadcrumb,
            "document_date": meta.document_date.isoformat() if meta.document_date else None,
            "project_tags": project_tags,
            **(c.metadata or {}),
        }
        embed_items.append((
            EMBEDDING_SOURCE,
            f"{doc.id}:{idx}",
            c.chunk_text,
            json.dumps(md, default=str),
        ))
    session.flush()

    enqueued = (
        EmbeddingService.enqueue_batch(session, embed_items, user_id=owner_user_id)
        if embed_items else 0
    )
    return doc, created, enqueued


def _safe_upsert(session: Session, **kwargs):
    """Wrap _upsert_document in a savepoint so one bad file can't poison the
    session for its neighbours. Returns (doc, created, enqueued) or None on failure."""
    sp = session.begin_nested()
    try:
        result = _upsert_document(session, **kwargs)
        sp.commit()
        return result
    except Exception:
        sp.rollback()
        raise


def _dispatch_by_suffix(path: Path):
    """Return a (meta, chunks) producer, or None if unsupported.

    Dispatch for pdf/docx/xlsx is by **sniffed content**, not by the
    extension the filename happens to carry (issue #140) — a file arriving
    named `.doc` that is really OOXML (a zip containing
    `word/document.xml`) is routed to the docx parser regardless, and a
    genuine legacy `.doc`/`.xls` (OLE2/MS-CFB) is recognised and skipped for
    the right reason rather than falling through as an unlabelled mismatch.
    `sniff_document_kind` is the one sniffer shared with the inbox and
    attachments ingestion paths, so all three agree on what a file actually is.

    `.txt`/`.md` stay suffix/frontmatter-based — the ambiguity there isn't a
    byte-level format question (WhatsApp export layout; `manual_of:`
    frontmatter for `.md`).
    """
    from app.services.doc_sniff import sniff_document_kind

    suffix = path.suffix.lower()
    name_l = path.name.lower()
    if suffix == ".txt":
        # Only WhatsApp exports are covered for .txt. iter_whatsapp handles paths;
        # outside that root we skip.
        return None
    if suffix == ".md":
        # Two different documents share this suffix. Route on content, not location: a
        # manual carries a `manual_of:` frontmatter key. Before this check every .md went
        # to voice_memo, so an equipment manual would have been stored with
        # source_type="voice_memo" and chunked by the transcript word-windower — wrong
        # label, wrong chunking, and invisible as a mislabelling because nothing errors.
        if manual_parser.looks_like_manual(path):
            return lambda: manual_parser.parse(path)
        return lambda: voice_memo_parser.parse(path)

    kind = sniff_document_kind(path)
    if kind == "pdf":
        return lambda: pdf_parser.parse(path)
    if kind == "docx":
        return lambda: docx_parser.parse(path)
    if kind == "xlsx":
        # BoQ family heuristic — other xlsx deferred.
        if "bill of quantities" in name_l or "recommendation for payment" in name_l:
            return lambda: boq_parser.parse(path)
        return None
    # "doc" / "xls" (legacy OLE2) and everything else: no parser exists.
    return None


def ingest_root(
    session: Session,
    root: Path,
    *,
    project_tags: list[str] | None = None,
    limit_per_type: int | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Walk the corpus and ingest every supported file.

    Commits in chunks (per source-type block) so a crash doesn't lose the full run.
    Embedding is handled by the background worker — we only enqueue here.
    """
    project_tags = project_tags or default_project_tags()
    stats = {
        "whatsapp_docs": 0, "whatsapp_skipped": 0,
        "email_threads": 0,
        "pdf_docs": 0, "pdf_low_yield": 0, "pdf_failed": 0,
        "docx_docs": 0, "docx_failed": 0, "docx_legacy_skipped": 0,
        "boq_docs": 0, "boq_failed": 0,
        "voice_memo_docs": 0, "voice_memo_failed": 0,
        "chunks": 0, "embeddings_enqueued": 0,
    }

    def _emit(msg: str) -> None:
        if on_progress:
            on_progress(msg)
        logger.info(msg)

    # --- WhatsApp ---
    for i, (path, chat_name) in enumerate(_iter_whatsapp(root)):
        if limit_per_type and i >= limit_per_type:
            break
        try:
            meta, chunks = whatsapp_parser.parse(path, chat_name=chat_name)
            if not chunks:
                stats["whatsapp_skipped"] += 1
                continue
            rel = str(path.relative_to(root))
            _, _, enq = _safe_upsert(
                session, source_path=rel, meta=meta, chunks=chunks,
                project_tags=project_tags,
            )
            stats["whatsapp_docs"] += 1
            stats["chunks"] += len(chunks)
            stats["embeddings_enqueued"] += enq
            _emit(f"[whatsapp] {chat_name}: {len(chunks)} chunks (+{enq} embed)")
        except Exception as e:
            logger.exception(f"whatsapp parse failed: {path}")
            _emit(f"[whatsapp] FAIL {path.name}: {e}")
    session.commit()

    # --- Email JSON (one file, many threads → many docs) ---
    email_path = root / EMAIL_JSON_NAME
    if email_path.exists():
        try:
            for thread_id, meta, chunks in email_parser.iter_threads(email_path):
                if not chunks:
                    continue
                rel = f"{EMAIL_JSON_NAME}#thread={thread_id}"
                _, _, enq = _safe_upsert(
                    session, source_path=rel, meta=meta, chunks=chunks,
                    project_tags=project_tags,
                )
                stats["email_threads"] += 1
                stats["chunks"] += len(chunks)
                stats["embeddings_enqueued"] += enq
            _emit(f"[email_json] {stats['email_threads']} threads ingested")
        except Exception as e:
            logger.exception("email_json parse failed")
            _emit(f"[email_json] FAIL: {e}")
    session.commit()

    # --- BoQ xlsx ---
    for i, path in enumerate(_iter_file_tree(root, {".xlsx"})):
        if limit_per_type and i >= limit_per_type:
            break
        name_l = path.name.lower()
        if "bill of quantities" not in name_l and "recommendation for payment" not in name_l:
            continue
        try:
            meta, chunks = boq_parser.parse(path)
            rel = str(path.relative_to(root))
            _, _, enq = _safe_upsert(
                session, source_path=rel, meta=meta, chunks=chunks,
                project_tags=project_tags,
            )
            stats["boq_docs"] += 1
            stats["chunks"] += len(chunks)
            stats["embeddings_enqueued"] += enq
            _emit(f"[boq] {path.name}: {len(chunks)} chunks (+{enq} embed)")
        except Exception as e:
            stats["boq_failed"] += 1
            logger.exception(f"boq parse failed: {path}")
            _emit(f"[boq] FAIL {path.name}: {e}")
    session.commit()

    # --- PDFs ---
    for i, path in enumerate(_iter_file_tree(root, {".pdf"})):
        if limit_per_type and i >= limit_per_type:
            break
        try:
            meta, chunks = pdf_parser.parse(path)
            if meta.metadata.get("extraction_quality") == "low":
                stats["pdf_low_yield"] += 1
            if not chunks:
                continue
            rel = str(path.relative_to(root))
            _, _, enq = _safe_upsert(
                session, source_path=rel, meta=meta, chunks=chunks,
                project_tags=project_tags,
            )
            stats["pdf_docs"] += 1
            stats["chunks"] += len(chunks)
            stats["embeddings_enqueued"] += enq
            if stats["pdf_docs"] % 25 == 0:
                session.commit()
                _emit(f"[pdf] {stats['pdf_docs']} processed…")
        except Exception as e:
            stats["pdf_failed"] += 1
            logger.exception(f"pdf parse failed: {path}")
            _emit(f"[pdf] FAIL {path.name}: {e}")
    session.commit()

    # --- docx ---
    # Also walks `.doc` (issue #140): some of those are really OOXML that was
    # saved/exported with the old extension, and `sniff_document_kind`
    # (content, not extension) is what tells the two apart. A genuine legacy `.doc`
    # (OLE2/MS-CFB) sniffs as "doc" and is skipped below, same as always.
    from app.services.doc_sniff import sniff_document_kind

    for i, path in enumerate(_iter_file_tree(root, {".docx", ".doc"})):
        if limit_per_type and i >= limit_per_type:
            break
        if sniff_document_kind(path) != "docx":
            if path.suffix.lower() == ".doc":
                stats["docx_legacy_skipped"] += 1
            continue
        try:
            meta, chunks = docx_parser.parse(path)
            if not chunks:
                continue
            rel = str(path.relative_to(root))
            _, _, enq = _safe_upsert(
                session, source_path=rel, meta=meta, chunks=chunks,
                project_tags=project_tags,
            )
            stats["docx_docs"] += 1
            stats["chunks"] += len(chunks)
            stats["embeddings_enqueued"] += enq
            _emit(f"[docx] {path.name}: {len(chunks)} chunks (+{enq} embed)")
        except Exception as e:
            stats["docx_failed"] += 1
            logger.exception(f"docx parse failed: {path}")
            _emit(f"[docx] FAIL {path.name}: {e}")
    session.commit()

    # --- Voice memos (.md transcripts) ---
    for i, path in enumerate(_iter_voice_memos(root)):
        if limit_per_type and i >= limit_per_type:
            break
        try:
            meta, chunks = voice_memo_parser.parse(path)
            if not chunks:
                continue
            rel = str(path.relative_to(root))
            _, _, enq = _safe_upsert(
                session, source_path=rel, meta=meta, chunks=chunks,
                project_tags=project_tags,
            )
            stats["voice_memo_docs"] += 1
            stats["chunks"] += len(chunks)
            stats["embeddings_enqueued"] += enq
            _emit(f"[voice_memo] {path.stem}: {len(chunks)} chunks (+{enq} embed)")
        except Exception as e:
            stats["voice_memo_failed"] += 1
            logger.exception(f"voice_memo parse failed: {path}")
            _emit(f"[voice_memo] FAIL {path.name}: {e}")
    session.commit()

    return stats


def ingest_claude_export(
    session: Session,
    paths: list[Path],
    *,
    owner_user_id: int,
    project_tags: list[str] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Ingest one or more claude.ai data-export conversations.json files.

    Unlike ingest_root, this takes explicit file paths rather than walking a
    directory tree — the export's conversations-NNN.json files don't live
    under doc_corpus and aren't named predictably across re-exports.

    `owner_user_id` is required, not defaulted: a claude.ai export is one
    person's conversation history, and Alex decided (2026-09-06) that it is
    private to that person. Until then every conversation was ingested as
    household-shared and `claude_history_search` answered any caller from
    his 5,000 chats. There is no shared Claude export, so there is no
    sensible default here — the caller has to say whose it is.
    """
    if owner_user_id is None:
        raise ValueError(
            "ingest_claude_export needs owner_user_id — a claude.ai export is "
            "one person's history and is never household-shared"
        )
    project_tags = project_tags or ["claude-conversations"]
    stats = {"conversations": 0, "skipped_empty": 0, "chunks": 0, "embeddings_enqueued": 0}

    def _emit(msg: str) -> None:
        if on_progress:
            on_progress(msg)
        logger.info(msg)

    for conv_uuid, meta, chunks in claude_export_parser.iter_conversations(paths):
        if not chunks:
            stats["skipped_empty"] += 1
            continue
        rel = f"claude_export#conversation={conv_uuid}"
        _, _, enq = _safe_upsert(
            session, source_path=rel, meta=meta, chunks=chunks,
            project_tags=project_tags, owner_user_id=owner_user_id,
        )
        stats["conversations"] += 1
        stats["chunks"] += len(chunks)
        stats["embeddings_enqueued"] += enq
        if stats["conversations"] % 200 == 0:
            session.commit()
            _emit(f"[claude_export] {stats['conversations']} conversations processed…")

    session.commit()
    _emit(f"[claude_export] done: {stats['conversations']} conversations, {stats['chunks']} chunks")
    return stats


def ingest_manuals(
    session: Session,
    root: Path,
    *,
    project_tags: list[str] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Ingest a tree of normalised equipment manuals (markdown with `manual_of` frontmatter).

    Separate from `ingest_root` because the manuals live in their own tree
    (`Documents/Reference/Manuals/text/`) alongside the source PDFs they were built from.
    Pointing `ingest_root` at that tree would ingest both — the same manual twice, once as
    clean markdown and once as raw pypdf output, competing for every query.

    ⚠️ Only the `text/` tree should ever be passed here. Its sibling `_not-ours/` holds
    correct manuals for equipment that isn't ours, and indexing those produces confident
    answers about hardware the house doesn't have.
    """
    project_tags = project_tags or ["manuals"]
    stats = {"manual_docs": 0, "chunks": 0, "embeddings_enqueued": 0, "manual_failed": 0}

    def _emit(msg: str) -> None:
        if on_progress:
            on_progress(msg)
        logger.info(msg)

    for path in sorted(root.rglob("*.md")):
        if not manual_parser.looks_like_manual(path):
            continue
        try:
            meta, chunks = manual_parser.parse(path)
            if not chunks:
                continue
            rel = str(path.relative_to(root))
            _, _, enq = _safe_upsert(
                session, source_path=rel, meta=meta, chunks=chunks,
                project_tags=project_tags,
            )
            stats["manual_docs"] += 1
            stats["chunks"] += len(chunks)
            stats["embeddings_enqueued"] += enq
            _emit(f"[manual] {meta.title}: {len(chunks)} chunks (+{enq} embed)")
        except Exception as e:
            stats["manual_failed"] += 1
            logger.exception(f"manual parse failed: {path}")
            _emit(f"[manual] FAIL {path.name}: {e}")
        if stats["manual_docs"] % 10 == 0:
            session.commit()
    session.commit()
    _emit(f"[manual] done: {stats['manual_docs']} manuals, {stats['chunks']} chunks")
    return stats


def ingest_path(
    session: Session, path: Path, *,
    project_tags: list[str] | None = None,
    owner_user_id: int | None = None,
) -> dict:
    """Ingest a single file. Handy for targeted re-runs, and the entry point
    `inbox_to_corpus` reuses. Shared unless told otherwise.

    Checks the raw bytes against every other document owned by the same
    scope (see `_find_duplicate`) *before* dispatching to a parser — a
    match skips parsing and embedding entirely and returns the existing
    document with `deduplicated: True`.
    """
    project_tags = project_tags or default_project_tags()
    producer = _dispatch_by_suffix(path)
    if producer is None:
        return {"skipped": str(path), "reason": "unsupported_or_no_heuristic"}

    rel = path.name
    raw_hash = _hash_bytes(path.read_bytes())
    dup = _find_duplicate(session, raw_hash, owner_user_id)
    if dup is not None and dup.source_path != rel:
        _record_alternate_source(session, dup, rel)
        return {
            "document_id": dup.id,
            "created": False,
            "deduplicated": True,
            "existing_id": dup.id,
            "chunks": 0,
            "embeddings_enqueued": 0,
        }

    meta, chunks = producer()
    doc, created, enq = _upsert_document(
        session, source_path=rel, meta=meta, chunks=chunks, project_tags=project_tags,
        owner_user_id=owner_user_id, raw_content_hash=raw_hash,
    )
    session.commit()
    return {
        "document_id": doc.id,
        "created": created,
        "chunks": len(chunks),
        "embeddings_enqueued": enq,
        "deduplicated": False,
    }
