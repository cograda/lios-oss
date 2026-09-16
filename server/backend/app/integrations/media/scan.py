"""Scan whatsapp_messages for media we don't yet track.

Populates media_items rows from raw_json — no downloads happen here, so the
scan is safe to run on every sync cycle. Idempotent via uq_media_item.

Expiry mirrors attachments/scan.py: WhatsApp CDN URLs carry a hard expiry and
the re-upload path (sock.updateMediaMessage) only works while the sender's
device still holds the bytes — outbound ~21 days, inbound ~30 days. Older
items are indexed as 'expired' rather than 'indexed' so the auto-downloader
skips them; media_fetch can still attempt them explicitly.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.integrations.media.models import MediaItem

OUTBOUND_EXPIRY_DAYS = 21
INBOUND_EXPIRY_DAYS = 30

# Baileys message node → our media_type. Documents are excluded on purpose:
# they belong to the attachments (parse-and-embed) pipeline.
MEDIA_NODES = {
    "imageMessage": "image",
    "videoMessage": "video",
    "audioMessage": "audio",
}

logger = logging.getLogger(__name__)


def _extract_media_meta(raw: dict) -> dict | None:
    msg = raw.get("message") or {}
    for node_name, media_type in MEDIA_NODES.items():
        node = msg.get(node_name)
        if node:
            size = node.get("fileLength")
            try:
                size = int(size) if size is not None else None
            except (TypeError, ValueError):
                size = None
            return {
                "media_type": media_type,
                "mime_type": node.get("mimetype"),
                "size_bytes": size,
                "caption": node.get("caption"),
            }
    return None


def scan_whatsapp_media(session: Session, user_id: int | None = None) -> dict:
    """Insert MediaItem rows for every WhatsApp image/video/audio message we
    don't already track. Returns counts so callers can surface progress.

    `user_id` scopes the scan to one user's `whatsapp_messages` (the MCP tool
    passes the caller's — 2026-09-06 scoping audit); `None` is the scheduled
    sync, unbound and household-wide, attributing rows via `w.user_id`.
    """
    uid_clause = "AND w.user_id = :uid" if user_id is not None else ""
    params = {"uid": user_id} if user_id is not None else {}
    sql = sa_text(f"""
        SELECT w.message_id, w.chat_name, w.chat_id, w.sender_name, w.timestamp,
               w.media_caption, w.raw_json, w.is_from_me, w.user_id
          FROM whatsapp_messages w
          LEFT JOIN media_items m
            ON m.source = 'whatsapp'
           AND m.message_ref = w.message_id
           AND m.user_id = w.user_id
         WHERE w.message_type IN ('image', 'video', 'audio')
           AND m.id IS NULL
           {uid_clause}
    """)
    rows = session.execute(sql, params).all()

    now = datetime.now(timezone.utc)
    outbound_cutoff = now - timedelta(days=OUTBOUND_EXPIRY_DAYS)
    inbound_cutoff = now - timedelta(days=INBOUND_EXPIRY_DAYS)

    indexed = expired = malformed = 0
    for r in rows:
        try:
            raw = json.loads(r.raw_json) if r.raw_json else {}
        except (TypeError, ValueError):
            raw = {}

        meta = _extract_media_meta(raw)
        if not meta:
            malformed += 1
            continue

        is_expired = (
            r.timestamp is not None
            and r.timestamp < (outbound_cutoff if r.is_from_me else inbound_cutoff)
        )
        session.add(MediaItem(
            user_id=r.user_id,
            source="whatsapp",
            message_ref=r.message_id,
            media_type=meta["media_type"],
            mime_type=meta["mime_type"],
            size_bytes=meta["size_bytes"],
            caption=meta["caption"] or r.media_caption,
            sender_name=r.sender_name,
            chat_or_thread=r.chat_name or r.chat_id,
            is_from_me=bool(r.is_from_me),
            message_ts=r.timestamp,
            status="expired" if is_expired else "indexed",
            skip_reason=(
                "older than the recoverable CDN/re-upload window at scan time"
                if is_expired else None
            ),
        ))
        if is_expired:
            expired += 1
        else:
            indexed += 1

    session.commit()
    return {
        "source": "whatsapp",
        "new_indexed": indexed,
        "new_expired": expired,
        "malformed": malformed,
    }
