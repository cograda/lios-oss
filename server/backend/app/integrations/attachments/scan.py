"""Scan Gmail + WhatsApp messages for unprocessed attachments.

Populates `message_attachments` rows in `parse_status='pending'`. No downloads
happen here — that's user-gated in `ingest.py`.

WhatsApp: everything we need (filename, mimetype, size, URL+keys) is already in
`whatsapp_messages.raw_json` — zero API calls.

Gmail is NOT implemented. `google_mail/sync.py` fetches messages with
`format='metadata'`, which strips `payload.parts` — so there is no attachment
filename/mimetype info in our DB to scan. Supporting this means a full-format
Gmail backfill (refetch candidates with `format='full'` and walk
`payload.parts`) — a real feature, deliberately deferred rather than bolted on
here. `scan_gmail` below returns an explicit "not supported" note instead of a
silent empty success so callers don't mistake absence-of-attachments for
completeness.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.integrations.attachments.models import MessageAttachment

# WhatsApp media expires from CDN URLs (the `oe` parameter is a hard timestamp)
# and the re-upload path (sock.updateMediaMessage) only succeeds while the
# *sender's* device still has the bytes. Empirically:
#   - outbound (fromMe=true): unrecoverable after ~3 weeks
#   - inbound: unrecoverable after ~30 days (sender's WA client has rotated)
# Pre-skipping these keeps rows out of 'pending' purgatory and avoids wasting
# bridge connection attempts that will all 403 on the CDN.
OUTBOUND_EXPIRY_DAYS = 21
INBOUND_EXPIRY_DAYS = 30

logger = logging.getLogger(__name__)


# Keep this aligned with parsers we actually have in historical_corpus.parsers.
# Anything else lands as 'unsupported' — user still sees the row but can't ingest.
PARSEABLE_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/msword",
    "application/vnd.ms-excel",
}


def _extract_document_meta(raw: dict) -> dict | None:
    """Pull filename + mimetype + size from a Baileys documentMessage payload.

    Baileys wraps the actual document in `message.documentMessage`; filename
    lives as `fileName`, size as `fileLength`. Both may be strings when
    serialised through JSON.
    """
    msg = raw.get("message") or {}
    doc = msg.get("documentMessage")
    if not doc:
        return None
    size = doc.get("fileLength")
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None
    return {
        "filename": doc.get("fileName") or doc.get("title"),
        "mime_type": doc.get("mimetype"),
        "size_bytes": size,
    }


def scan_whatsapp(session: Session) -> dict:
    """Insert MessageAttachment rows for every WhatsApp document we don't already track.

    Idempotent: the `uq_msg_attachment` constraint collapses re-scans. Returns
    counts by status so the caller can surface progress.
    """
    # Select only rows we haven't already recorded. Doing this in SQL (rather
    # than iterating all 328 documents) keeps incremental scans cheap after
    # the initial backfill.
    sql = sa_text("""
        SELECT w.message_id, w.chat_name, w.sender_name, w.timestamp,
               w.media_caption, w.raw_json, w.is_from_me, w.user_id
          FROM whatsapp_messages w
          LEFT JOIN message_attachments a
            ON a.source = 'whatsapp' AND a.message_ref = w.message_id
         WHERE w.message_type = 'document' AND a.id IS NULL
    """)
    rows = session.execute(sql).all()

    now = datetime.now(timezone.utc)
    outbound_cutoff = now - timedelta(days=OUTBOUND_EXPIRY_DAYS)
    inbound_cutoff = now - timedelta(days=INBOUND_EXPIRY_DAYS)
    created = skipped_unsupported = skipped_malformed = skipped_outbound = skipped_inbound = 0
    for r in rows:
        try:
            raw = json.loads(r.raw_json) if r.raw_json else {}
        except (TypeError, ValueError):
            skipped_malformed += 1
            continue

        meta = _extract_document_meta(raw)
        if not meta:
            skipped_malformed += 1
            continue

        # Prefer raw filename; fall back to the media_caption the bridge stored,
        # which for documents is already set to documentMessage.fileName.
        filename = meta["filename"] or r.media_caption

        parseable = meta["mime_type"] in PARSEABLE_MIMES
        expired_outbound = (
            parseable and r.is_from_me and r.timestamp
            and r.timestamp < outbound_cutoff
        )
        expired_inbound = (
            parseable and not r.is_from_me and r.timestamp
            and r.timestamp < inbound_cutoff
        )
        if expired_outbound:
            status = "skipped"
            skip_reason = f"outbound older than {OUTBOUND_EXPIRY_DAYS}d — media likely unrecoverable"
        elif expired_inbound:
            status = "skipped"
            skip_reason = f"inbound older than {INBOUND_EXPIRY_DAYS}d — sender's CDN copy likely expired"
        elif parseable:
            status = "pending"
            skip_reason = None
        else:
            status = "unsupported"
            skip_reason = f"mimetype {meta['mime_type']!r} not parseable"

        session.add(MessageAttachment(
            # Inherit user_id from parent WhatsApp message (per Phase A
            # plan). Whatsapp_messages.user_id defaults to 1 today; second
            # bridge in Phase F will write user_id=2 explicitly.
            user_id=r.user_id,
            source="whatsapp",
            message_ref=r.message_id,
            filename=filename,
            mime_type=meta["mime_type"],
            size_bytes=meta["size_bytes"],
            sender_name=r.sender_name,
            chat_or_thread=r.chat_name,
            message_ts=r.timestamp,
            parse_status=status,
            skip_reason=skip_reason,
        ))
        if expired_outbound:
            skipped_outbound += 1
        elif expired_inbound:
            skipped_inbound += 1
        elif parseable:
            created += 1
        else:
            skipped_unsupported += 1

    session.commit()
    return {
        "source": "whatsapp",
        "new_pending": created,
        "unsupported": skipped_unsupported,
        "skipped_outbound_expired": skipped_outbound,
        "skipped_inbound_expired": skipped_inbound,
        "malformed": skipped_malformed,
    }


def scan_gmail(session: Session) -> dict:
    """Not implemented — see module docstring for why.

    Deferred-feature sketch (separate PR, not started):
      1. Query Gmail API with q="has:attachment after:<oldest mail_messages.date>"
         to get a candidate id list — cheap, no payloads.
      2. For each candidate not already in message_attachments, refetch with
         format='full' (one-shot, batched, ~10x metadata cost only on the
         actual subset).
      3. Walk payload.parts recursively: each part with a filename gets a row
         with body.attachmentId stored in storage_path so ingest can pull
         bytes via users.messages.attachments.get(messageId, id).
      4. message_ref = mail_messages.google_message_id (already unique).
      5. Do it as a delta from the current point in time — avoids backfilling
         9k+ historical messages all at once.
    """
    return {
        "source": "gmail",
        "new_pending": 0,
        "supported": False,
        "note": "gmail scanning not supported (messages synced metadata-only)",
    }
