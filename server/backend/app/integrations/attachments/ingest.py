"""Download + parse + embed WhatsApp attachments.

Flow per id:
  1. Load MessageAttachment row, validate it's pending + parseable
  2. GET http://comar-whatsapp:3100/download/{message_ref}  → bytes
  3. Stage to /tmp/wa_attachments/{id}_{filename}
  4. Dispatch to the corpus parser (pdf / docx / xlsx)
  5. Upsert HistoricalDocument + chunks (reuse corpus._upsert_document)
  6. Link attachment → doc, flip parse_status='ingested'

Same historical_corpus embedding source, so these are returned by
`renovation_context` with no extra wiring.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.integrations.attachments.models import MessageAttachment
from app.integrations.historical_corpus.ingest import _upsert_document
from app.integrations.historical_corpus.parsers import (
    boq as boq_parser,
    docx as docx_parser,
    pdf as pdf_parser,
)
from app.integrations.historical_corpus.parsers.types import DocMeta
from app.tools.helpers import scoped_query

logger = logging.getLogger(__name__)

BRIDGE_URL = os.environ.get("HOME_WA_BRIDGE_URL", "http://comar-whatsapp:3100")
STAGING_DIR = Path(os.environ.get("HOME_ATTACHMENT_STAGING", "/tmp/wa_attachments"))

# Skip anything bigger than this — anything over 25 MB on WhatsApp is
# probably a video the sender labelled as a document, and parsing would
# thrash with no retrieval benefit.
MAX_BYTES = 25 * 1024 * 1024

MIME_TO_PARSER = {
    "application/pdf": ("pdf", pdf_parser),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("docx", docx_parser),
    "application/msword": ("docx", docx_parser),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ("xlsx", boq_parser),
    "application/vnd.ms-excel": ("xlsx", boq_parser),
}


def _sanitize(name: str | None) -> str:
    """Filesystem-safe filename. Preserves the extension so parsers dispatch
    correctly — the PDF parser keys off suffix via the corpus dispatcher."""
    name = name or "attachment"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:200]


def _download(message_ref: str, dest: Path) -> int:
    from app.config import settings

    headers = {}
    if settings.wa_bridge_shared_secret:
        headers["X-Bridge-Secret"] = settings.wa_bridge_shared_secret
    with httpx.stream(
        "GET", f"{BRIDGE_URL}/download/{message_ref}", timeout=120.0, headers=headers,
    ) as r:
        r.raise_for_status()
        size = 0
        with dest.open("wb") as f:
            for chunk in r.iter_bytes(chunk_size=65536):
                f.write(chunk)
                size += len(chunk)
        return size


def ingest_one(session: Session, attachment_id: int) -> dict:
    """Ingest a single attachment. Safe to re-run: content-hash dedup upstream
    skips no-op re-embeds."""
    att = scoped_query(session, MessageAttachment).filter_by(id=attachment_id).one_or_none()
    if not att:
        return {"id": attachment_id, "status": "error", "detail": "not found"}
    if att.parse_status == "ingested" and att.historical_doc_id:
        return {"id": attachment_id, "status": "already_ingested", "historical_doc_id": att.historical_doc_id}
    if att.source != "whatsapp":
        return {"id": attachment_id, "status": "error", "detail": f"source {att.source!r} not supported yet"}
    if att.size_bytes and att.size_bytes > MAX_BYTES:
        att.parse_status = "skipped"
        att.skip_reason = f"over size cap ({att.size_bytes} > {MAX_BYTES})"
        session.commit()
        return {"id": attachment_id, "status": "skipped", "detail": att.skip_reason}

    parser_info = MIME_TO_PARSER.get(att.mime_type or "")
    if not parser_info:
        att.parse_status = "unsupported"
        att.skip_reason = f"no parser for mimetype {att.mime_type!r}"
        session.commit()
        return {"id": attachment_id, "status": "unsupported", "detail": att.skip_reason}
    kind, parser = parser_info

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    stage_path = STAGING_DIR / f"{att.id}_{_sanitize(att.filename)}"

    try:
        size = _download(att.message_ref, stage_path)
        logger.info(f"[attachments] downloaded id={att.id} size={size} → {stage_path}")
    except Exception as e:
        att.parse_status = "failed"
        att.skip_reason = f"download failed: {e}"
        session.commit()
        return {"id": attachment_id, "status": "failed", "detail": att.skip_reason}

    try:
        meta, chunks = parser.parse(stage_path)
    except Exception as e:
        logger.exception(f"[attachments] parse failed id={att.id}")
        att.parse_status = "failed"
        att.skip_reason = f"parse failed: {e}"
        session.commit()
        return {"id": attachment_id, "status": "failed", "detail": att.skip_reason}

    # Override the parser's title/source_type with attachment provenance so
    # corpus queries can distinguish these from filesystem-sourced docs.
    enriched = DocMeta(
        title=meta.title or att.filename,
        source_type=f"wa_attachment_{kind}",
        author=att.sender_name or meta.author,
        participants=meta.participants,
        document_date=att.message_ts.date() if att.message_ts else meta.document_date,
        metadata={
            **(meta.metadata or {}),
            "wa_message_ref": att.message_ref,
            "wa_sender": att.sender_name,
            "wa_chat": att.chat_or_thread,
            "wa_filename": att.filename,
            "wa_size_bytes": att.size_bytes,
            "attachment_id": att.id,
        },
    )
    # Unique source_path so this doesn't collide with any on-disk copy of the same file.
    source_path = f"wa_attachment/{att.id}/{att.filename or 'unnamed'}"

    doc, created, enq = _upsert_document(
        session, source_path=source_path, meta=enriched, chunks=chunks,
        project_tags=["renovation"],
    )
    att.historical_doc_id = doc.id
    att.storage_path = str(stage_path)
    att.parse_status = "ingested"
    att.skip_reason = None
    att.processed_at = datetime.now(timezone.utc)
    session.commit()

    return {
        "id": attachment_id,
        "status": "ingested",
        "historical_doc_id": doc.id,
        "created": created,
        "chunks": len(chunks),
        "embeddings_enqueued": enq,
        "filename": att.filename,
    }


def ingest_many(session: Session, ids: list[int]) -> dict:
    results = [ingest_one(session, i) for i in ids]
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return {"counts": by_status, "results": results}
