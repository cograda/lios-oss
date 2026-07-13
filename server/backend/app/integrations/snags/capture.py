"""Capture structured snag reports from WhatsApp into the snag register.

Sam's (and anyone's) convention: `Snag - <room> - [<element>] - [WindowCo] - <detail>`
as a text message or a photo caption. Capture:

  1. finds snag-shaped messages not yet in snag_source_messages
  2. groups identical (room, normalised text) so a text message + N photos of
     the same defect become ONE snag with N evidence links
  3. creates Snag rows (UID assigned from the id sequence) + snag_media links
     via the media store index

Near-duplicate wording stays separate on purpose — merging is a triage
judgement, not a capture one.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.integrations.media.models import MediaItem
from app.integrations.snags.models import Snag, SnagMedia, SnagSourceMessage, TRADES

logger = logging.getLogger(__name__)

ROOM_ALIASES = {
    "finns room": "Finn's room",
    "finn's room": "Finn's room",
    "islas room": "Isla's room",
    "isla's room": "Isla's room",
    "alex office": "Alex's office",
    "alexs office": "Alex's office",
    "sam office": "Sam's office",
    "sams office": "Sam's office",
    "guest wc": "Guest WC",
    "main space": "Main space",
    "all": "House-wide",
    "all windows": "House-wide",
    "utility": "Utility room",
    "utility room": "Utility room",
    "master": "Master bedroom",
}

_TRADE_TOKENS = {t.replace("-", " "): t for t in TRADES} | {"ken/fergal": "ken-fergal"}


def _normalise_room(token: str) -> str:
    key = token.strip().lower()
    return ROOM_ALIASES.get(key, token.strip().capitalize() if token.islower() else token.strip())


def parse_snag_text(text: str) -> dict | None:
    """Parse 'Snag - room - [element -] [trade -] detail'. Returns None if the
    text isn't snag-shaped."""
    if not text:
        return None
    parts = [p.strip() for p in re.split(r"\s+-\s+|\s+-|-\s+", text) if p.strip()]
    if len(parts) < 3 or parts[0].lower() != "snag":
        return None

    room = _normalise_room(parts[1])
    middle = parts[2:-1]
    detail = parts[-1]

    trade = "unknown"
    element_tokens = []
    for tok in middle:
        t = tok.strip().lower()
        if t in _TRADE_TOKENS:
            trade = _TRADE_TOKENS[t]
        else:
            element_tokens.append(tok.strip())

    return {
        "room": room,
        "element": " - ".join(element_tokens) or None,
        "trade": trade,
        "detail": detail,
    }


def _title_from(detail: str) -> str:
    title = detail.strip()
    title = title[0].upper() + title[1:] if title else title
    return title[:300]


def _next_uid(session: Session) -> str:
    n = session.execute(sa_text("SELECT nextval('snag_uid_seq')")).scalar()
    return f"SNAG-{int(n):04d}"


def capture_whatsapp_snags(session: Session, since_days: int = 7) -> dict:
    """Scan whatsapp messages for snag-shaped text/captions and register them."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    rows = session.execute(sa_text("""
        SELECT w.message_id, w.sender_name, w.timestamp, w.is_from_me,
               COALESCE(w.body, w.media_caption) AS snag_text
          FROM whatsapp_messages w
          LEFT JOIN snag_source_messages s ON s.message_ref = w.message_id
         WHERE w.timestamp >= :cutoff
           AND s.id IS NULL
           AND (w.body ILIKE 'snag%' OR w.media_caption ILIKE 'snag%')
         ORDER BY w.timestamp ASC
    """), {"cutoff": cutoff}).all()

    # Group identical (room, lowercased detail) → one snag, many messages
    groups: dict[tuple, dict] = {}
    skipped = 0
    for r in rows:
        parsed = parse_snag_text(r.snag_text or "")
        if not parsed:
            skipped += 1
            continue
        key = (parsed["room"].lower(), parsed["detail"].lower())
        g = groups.setdefault(key, {"parsed": parsed, "messages": []})
        g["messages"].append(r)

    created = 0
    for g in groups.values():
        parsed, messages = g["parsed"], g["messages"]
        first = messages[0]
        sender = first.sender_name or ("Alex" if first.is_from_me else None)
        snag = Snag(
            uid=_next_uid(session),
            title=_title_from(parsed["detail"]),
            description=first.snag_text,
            room=parsed["room"],
            element=parsed["element"],
            trade=parsed["trade"],
            reported_by=sender,
            reported_at=first.timestamp,
            source_ref=" ".join(m.message_id for m in messages),
        )
        session.add(snag)
        session.flush()

        refs = [m.message_id for m in messages]
        for ref in refs:
            session.add(SnagSourceMessage(message_ref=ref, snag_id=snag.id))
        media = (
            session.query(MediaItem)
            .filter(MediaItem.source == "whatsapp", MediaItem.message_ref.in_(refs))
            .all()
        )
        for item in media:
            session.add(SnagMedia(snag_id=snag.id, media_item_id=item.id))
        created += 1

    session.commit()
    return {
        "messages_seen": len(rows),
        "snags_created": created,
        "not_snag_shaped": skipped,
    }
