"""Capture structured snag reports from WhatsApp into the snag register.

Reporting convention: `Snag - <room> - [<element>] - [<trade>] - <detail>` as a
text message or a photo caption. Room names are normalised through the
`room_aliases` config map and trades through `trades` (see vocab.py) —
both deployment-specific. Capture:

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

from app.auth.context import current_user_id
from app.integrations.snags.models import Snag, SnagMedia, SnagSourceMessage
from app.integrations.snags.vocab import room_aliases, trades
from app.plugin.capabilities import get_capability

MediaItem = get_capability("media.store").Item

logger = logging.getLogger(__name__)


def _trade_tokens() -> dict[str, str]:
    """Loose spellings of a trade → its canonical slug.

    Read per call rather than computed at import: `trades()` is config, and
    a config change should take effect without a restart. Slug-with-hyphens
    is also accepted spelled with a space or a slash, since that's how
    people type multi-name trades in a WhatsApp message.
    """
    out: dict[str, str] = {}
    for slug in trades():
        out[slug] = slug
        out[slug.replace("-", " ")] = slug
        out[slug.replace("-", "/")] = slug
    return out


def _normalise_room(token: str) -> str:
    key = token.strip().lower()
    aliases = room_aliases()
    if key in aliases:
        return aliases[key]
    return token.strip().capitalize() if token.islower() else token.strip()


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
    trade_tokens = _trade_tokens()
    for tok in middle:
        t = tok.strip().lower()
        if t in trade_tokens:
            trade = trade_tokens[t]
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
    """Scan whatsapp messages for snag-shaped text/captions and register them.

    Idempotency (`snag_source_messages`) is per-user (F5): a group-chat
    message ingested by both household members' WhatsApp bridges shares a
    `message_ref` across their two `whatsapp_messages` rows, so the "already
    captured" join must also match `user_id` — otherwise the second user's
    capture run would silently see the first user's row as "already seen"
    and skip a snag that, from their side, is new. Symmetrically, the source
    scan itself is scoped to `w.user_id`: `whatsapp_messages` is per-user
    data, and without that filter a capture run would also re-process the
    OTHER user's copy of the same shared-group message, producing two
    `snag_source_messages` inserts for one `(user_id, message_ref)` and a
    unique-constraint violation.
    """
    uid = current_user_id()
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    rows = session.execute(sa_text("""
        SELECT w.message_id, w.sender_name, w.timestamp, w.is_from_me,
               COALESCE(w.body, w.media_caption) AS snag_text
          FROM whatsapp_messages w
          LEFT JOIN snag_source_messages s
                 ON s.message_ref = w.message_id AND s.user_id = :uid
         WHERE w.user_id = :uid
           AND w.timestamp >= :cutoff
           AND s.id IS NULL
           AND (w.body ILIKE 'snag%' OR w.media_caption ILIKE 'snag%')
         ORDER BY w.timestamp ASC
    """), {"cutoff": cutoff, "uid": uid}).all()

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
            session.add(SnagSourceMessage(message_ref=ref, snag_id=snag.id, user_id=uid))
        # F5: scope to the capturing user's own MediaItem rows. Without this,
        # a message_ref shared across both bridges' whatsapp_messages rows
        # could pull in the OTHER user's media item as "evidence" the
        # current user never sent or received — a shared snag becoming an
        # implicit channel for one user's WhatsApp media to reach the other.
        media = (
            session.query(MediaItem)
            .filter(
                MediaItem.source == "whatsapp",
                MediaItem.message_ref.in_(refs),
                MediaItem.user_id == uid,
            )
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
