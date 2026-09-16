"""Download + parse + embed message attachments (WhatsApp and Gmail).

Flow per id:
  1. Load MessageAttachment row (scoped to its owner), validate it's
     pending + parseable
  2. Fetch bytes, by source:
       whatsapp — GET http://lios-whatsapp:3100/download/{message_ref}
       gmail    — `users.messages.attachments.get(messageId=message_ref,
                  id=storage_path)` through the `mail.query` capability,
                  with the row owner's Google token (see `_download_gmail`)
  3. Stage to /tmp/wa_attachments/{id}_{filename}
  4. Dispatch to the corpus parser (pdf / docx / xlsx)
  5. Upsert HistoricalDocument + chunks (reuse corpus._upsert_document)
  6. Link attachment → doc, flip parse_status='ingested'

Same historical_corpus embedding source, so these are returned by
`corpus_search` with no extra wiring — as `wa_attachment_{kind}` or
`gmail_attachment_{kind}` (`SOURCE_TYPE_PREFIX`).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.integrations.attachments.models import MessageAttachment
from app.integrations.attachments.sources import (
    SUPPORTED_INGEST_SOURCES,
    unsupported_source_reason,
)
from app.plugin.capabilities import get_capability

_corpus = get_capability("corpus.ingest")
boq_parser = _corpus.boq_parser
docx_parser = _corpus.docx_parser
pdf_parser = _corpus.pdf_parser
DocMeta = _corpus.DocMeta

logger = logging.getLogger(__name__)

BRIDGE_URL = os.environ.get("HOME_WA_BRIDGE_URL", "http://lios-whatsapp:3100")
STAGING_DIR = Path(os.environ.get("HOME_ATTACHMENT_STAGING", "/tmp/wa_attachments"))

# Skip anything bigger than this — anything over 25 MB on WhatsApp is
# probably a video the sender labelled as a document, and parsing would
# thrash with no retrieval benefit.
MAX_BYTES = 25 * 1024 * 1024

# `HistoricalDocument.source_type` prefix per attachment source, and the
# metadata-key prefix that rides alongside it. Every value here must appear
# in `historical_corpus/parsers/types.py::KNOWN_SOURCE_TYPES` (a test
# derives that registry from this table).
SOURCE_TYPE_PREFIX = {
    "whatsapp": "wa_attachment",
    "gmail": "gmail_attachment",
}
_META_PREFIX = {"whatsapp": "wa", "gmail": "gmail"}

MIME_TO_PARSER = {
    "application/pdf": ("pdf", pdf_parser),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("docx", docx_parser),
    "application/msword": ("docx", docx_parser),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ("xlsx", boq_parser),
    "application/vnd.ms-excel": ("xlsx", boq_parser),
}

# What a *content sniff* (not the sender-declared mime_type) maps to. Same
# parsers as MIME_TO_PARSER's document-shaped entries — this table exists so
# `ingest_one` can re-derive the parser from the downloaded bytes rather than
# trusting the sender's Content-Type, which is exactly the kind of untrusted
# label the inbox integration's "nothing arriving here has a usable
# filename" lesson already warns about (issue #140).
_SNIFFED_KIND_TO_PARSER = {
    "pdf": ("pdf", pdf_parser),
    "docx": ("docx", docx_parser),
    "xlsx": ("xlsx", boq_parser),
}


def _resolve_parser(mime_parser_info, stage_path: Path):
    """Resolve the (kind, parser) to actually use for a downloaded
    attachment, given what its sender-declared mime_type implied.

    Pure function over the file on disk — no DB, no network — so it's the
    seam issue #140's fix is unit-tested against directly.

    Content sniffing wins whenever it's definitive: a sniffed pdf/docx/xlsx
    is used even when it disagrees with (or the sender never named) a mime
    type. Sniffed legacy OLE2 content (`doc`/`xls`) is reported as
    unparseable outright, whatever the mime_type claimed — feeding OLE2
    bytes to python-docx would raise a confusing exception rather than the
    true reason. Anything else (an ambiguous "zip", "unknown", ...) falls
    back to what the mime_type implied, which may itself be None.

    Returns `(parser_info_or_None, reason_or_None)`. `reason` is the sniffed
    kind string whenever the sniff changed the outcome (either overriding
    the mime-implied parser, or reporting a legacy-office dead end) — the
    caller uses it purely for logging/the skip reason, never for control
    flow beyond "was there an override".
    """
    from app.services.doc_sniff import sniff_document_kind

    sniffed = sniff_document_kind(stage_path)
    sniffed_parser_info = _SNIFFED_KIND_TO_PARSER.get(sniffed)
    if sniffed_parser_info:
        if mime_parser_info is None or sniffed_parser_info[0] != mime_parser_info[0]:
            return sniffed_parser_info, sniffed
        return sniffed_parser_info, None
    if sniffed in ("doc", "xls"):
        return None, sniffed
    return mime_parser_info, None


def _sanitize(name: str | None) -> str:
    """Filesystem-safe filename. Preserves the extension so parsers dispatch
    correctly — the PDF parser keys off suffix via the corpus dispatcher."""
    name = name or "attachment"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:200]


def _download(message_ref: str, dest: Path) -> int:
    """WhatsApp: stream the bytes from the Baileys bridge."""
    with httpx.stream("GET", f"{BRIDGE_URL}/download/{message_ref}", timeout=120.0) as r:
        r.raise_for_status()
        size = 0
        with dest.open("wb") as f:
            for chunk in r.iter_bytes(chunk_size=65536):
                f.write(chunk)
                size += len(chunk)
        return size


def _gmail_account_candidates(session: Session, att: MessageAttachment, gmail) -> list[str]:
    """Which mailbox(es) to ask for this attachment.

    A `message_attachments` row records the Gmail message id but not the
    account it came from, and message ids are per-mailbox. The routine mail
    sync usually has the message cached with its `account_email`; when it
    does not (the attachment scan reaches further back than the mail
    backfill), every Google account the OWNER has connected is a candidate
    and the wrong ones answer 404. Always the row owner's accounts — spelled
    `att.user_id`, not the ambient caller, so the scoping is on the row
    (`ingest_one` has already refused a foreign id by this point).
    """
    from app.models.tokens import OAuthToken

    known = gmail.account_for_message(session, att.message_ref, user_id=att.user_id)
    if known:
        return [known]
    return [
        t.account_email
        for t in session.query(OAuthToken.account_email)
        .filter_by(user_id=att.user_id, provider="google")
        .distinct()
        .order_by(OAuthToken.account_email)
        .all()
    ]


def _download_gmail(session: Session, att: MessageAttachment, dest: Path) -> int:
    """Gmail: fetch via `users.messages.attachments.get` with the owner's token.

    `storage_path` holds the Gmail `attachmentId` recorded at scan time
    (`scan._insert_gmail_attachments`). Those ids are not stable forever —
    a 404 from every candidate account is reported as such rather than as
    a generic download failure, because the remedy (the id has rotated; a
    fresh full-format fetch of the message would mint a new one) differs
    from a network fault's. Resolved through `get_capability` at call time
    so tests can stand in a fake mail facade.
    """
    gmail = get_capability("mail.query")
    attachment_id = att.storage_path
    if not attachment_id:
        raise RuntimeError("row has no Gmail attachmentId in storage_path")

    accounts = _gmail_account_candidates(session, att, gmail)
    if not accounts:
        raise RuntimeError(f"no connected Google account for user {att.user_id}")

    gone: list[str] = []
    for account in accounts:
        try:
            data = gmail.fetch_attachment(
                account, session, att.message_ref, attachment_id, user_id=att.user_id,
            )
        except gmail.AttachmentGone:
            gone.append(account)
            continue
        if data is None:
            # No stored credentials for this (owner, account) — cannot be
            # the mailbox that produced a row we discovered with its token.
            continue
        if len(data) > MAX_BYTES:
            raise RuntimeError(f"over size cap ({len(data)} > {MAX_BYTES})")
        dest.write_bytes(data)
        return len(data)

    if gone:
        raise RuntimeError(
            "Gmail returned 404 for the recorded attachment id on message "
            f"{att.message_ref} in {', '.join(gone)} — Gmail attachment ids are "
            "not stable; the id recorded at scan time has likely rotated"
        )
    raise RuntimeError(f"no usable Google credentials for user {att.user_id}")


def _fetch_bytes(session: Session, att: MessageAttachment, dest: Path) -> int:
    """Per-source dispatch for step 2. Only sources in
    `SUPPORTED_INGEST_SOURCES` reach here; a source in that set with no
    branch below is a programming error, not a data condition."""
    if att.source == "whatsapp":
        return _download(att.message_ref, dest)
    if att.source == "gmail":
        return _download_gmail(session, att, dest)
    raise RuntimeError(f"source {att.source!r} is in SUPPORTED_INGEST_SOURCES but has no fetcher")


def ingest_one(session: Session, attachment_id: int, *, user_id: int | None = None) -> dict:
    """Ingest a single attachment. Safe to re-run: content-hash dedup upstream
    skips no-op re-embeds.

    The attachment is resolved scoped to its owner — `user_id`, defaulting to
    the bound caller. `message_attachments` is per-user data (a WhatsApp
    document or Gmail attachment one user received), but the corpus it lands in is
    household-shared, so an unscoped `session.get` here let either user
    publish the OTHER user's private attachment into the shared corpus
    (2026-09-06 scoping audit). A foreign id returns the same `not found`
    shape as a nonexistent one on purpose — "exists but isn't yours" would be
    an existence oracle over someone else's messages.
    """
    uid = current_user_id() if user_id is None else user_id
    att = (
        session.query(MessageAttachment)
        .filter(MessageAttachment.id == attachment_id, MessageAttachment.user_id == uid)
        .first()
    )
    if not att:
        return {"id": attachment_id, "status": "error", "detail": "not found"}
    if att.parse_status == "ingested" and att.historical_doc_id:
        return {"id": attachment_id, "status": "already_ingested", "historical_doc_id": att.historical_doc_id}
    if att.source not in SUPPORTED_INGEST_SOURCES:
        return {"id": attachment_id, "status": "error", "detail": unsupported_source_reason(att.source)}
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
        size = _fetch_bytes(session, att, stage_path)
        logger.info(f"[attachments] downloaded id={att.id} source={att.source} size={size} → {stage_path}")
    except Exception as e:
        att.parse_status = "failed"
        att.skip_reason = f"download failed: {e}"
        session.commit()
        return {"id": attachment_id, "status": "failed", "detail": att.skip_reason}

    # The mime_type used to pick `parser_info` above is sender-declared
    # metadata — no more trustworthy than a filename extension. Re-derive
    # from the downloaded bytes and prefer that: a `.doc`-named/labelled
    # attachment that is really OOXML content (`application/msword` claimed,
    # but the bytes are a zip containing `word/document.xml`) must still
    # route to the docx parser rather than fail or silently mis-parse.
    resolved, override_reason = _resolve_parser(parser_info, stage_path)
    if override_reason and resolved:
        logger.info(
            f"[attachments] id={att.id} mime_type={att.mime_type!r} implied "
            f"kind={kind!r} but content sniffed as {override_reason!r} — using "
            "the parser for the sniffed kind"
        )
    elif override_reason and not resolved:
        # Legacy MS-CFB/OLE2 content, however it was labelled — no parser
        # exists for either the claimed or the real shape, so fail with the
        # true reason instead of feeding OLE2 bytes to python-docx.
        att.parse_status = "unsupported"
        att.skip_reason = f"content is legacy {override_reason} (OLE2); no parser available"
        session.commit()
        return {"id": attachment_id, "status": "unsupported", "detail": att.skip_reason}
    if not resolved:
        att.parse_status = "unsupported"
        att.skip_reason = f"no parser for mimetype {att.mime_type!r}"
        session.commit()
        return {"id": attachment_id, "status": "unsupported", "detail": att.skip_reason}
    kind, parser = resolved

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
    # Metadata keys keep the historical `wa_*` names for WhatsApp rows
    # (existing documents carry them) and use `gmail_*` for Gmail, where
    # `chat` is the message subject.
    type_prefix = SOURCE_TYPE_PREFIX[att.source]
    mp = _META_PREFIX[att.source]
    enriched = DocMeta(
        title=meta.title or att.filename,
        source_type=f"{type_prefix}_{kind}",
        author=att.sender_name or meta.author,
        participants=meta.participants,
        document_date=att.message_ts.date() if att.message_ts else meta.document_date,
        metadata={
            **(meta.metadata or {}),
            "attachment_source": att.source,
            f"{mp}_message_ref": att.message_ref,
            f"{mp}_sender": att.sender_name,
            f"{mp}_chat": att.chat_or_thread,
            f"{mp}_filename": att.filename,
            f"{mp}_size_bytes": att.size_bytes,
            "attachment_id": att.id,
        },
    )
    # Unique source_path so this doesn't collide with any on-disk copy of the same file.
    source_path = f"{type_prefix}/{att.id}/{att.filename or 'unnamed'}"

    # No project_tags: the corpus applies this deployment's configured
    # default (historical_corpus.default_project_tag). Previously hardcoded
    # to the literal "riverside".
    doc, created, enq = _corpus.upsert_document(
        session, source_path=source_path, meta=enriched, chunks=chunks,
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


def ingest_many(session: Session, ids: list[int], *, user_id: int | None = None) -> dict:
    results = [ingest_one(session, i, user_id=user_id) for i in ids]
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return {"counts": by_status, "results": results}
