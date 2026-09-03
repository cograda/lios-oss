"""Pull message-to-self WhatsApp notes into the inbox triage queue.

WhatsApp's "Message yourself" chat is a capture surface — ideas, links, one-line
reminders typed on a phone. Those messages were already stored and searchable,
but nothing *routed* them: a voice note dropped into the inbox gets a summary and
a triage decision, while a typed note sat in `whatsapp_messages` forever, needing
someone to remember it was there. This closes that gap so both capture channels
land in the same queue.

**Why this lives in `inbox` and not `whatsapp`.** The intuitive direction —
whatsapp pushing into the inbox — closes a capability cycle that
`app/plugin/validate.py` rejects at boot, because `system` (reachable from
`inbox` via `notify.push` → `system.alerts`) depends on `whatsapp.query`. Pulling
is also the better fit: the inbox already owns the queue and a cron whose job is
drawing things into it, the same shape `attachments` uses to pull from
`mail.query`.

**Idempotency comes from content hashing, not a new table.** Every routed note
gets a `sha256` on its sidecar, and `scan.find_by_hash` searches terminal buckets
as well as pending ones — so a note that was routed, triaged and archived is not
re-created on the next sweep. That property already existed for exactly this
reason (a re-synced voice memo must not come back as new), and reusing it avoids
a migration and a second source of truth. The hashed content includes the message
id and timestamp, so two genuinely separate notes with identical text are still
distinct items.

**No push notification.** The user typed the note on their phone seconds ago;
telling them it arrived is noise. Contrast the transcription and vision sweeps,
which announce because they add information the user did not have.

⚠️ **What counts as a self-note is decided by the facade, and it is stricter than
it looks.** A WhatsApp `@lid` is scoped to the account that observed it, not
global — the same LID string names a different conversation under a different
bridge. This first shipped keyed on JID alone and the consequence was immediate:
Alex's self-chat LID matched 178 rows under Sam's bridge, all of them messages
she had *received* from a third party, and 59 were filed into her inbox as her own
notes before it was caught. `whatsapp.query::self_notes` now matches the
`(user_id, jid)` pair **and** requires `is_from_me`. Do not relax either here —
this module writes files into a person's private queue on the strength of them.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.integrations.inbox import scan

logger = logging.getLogger(__name__)

# Bounded per sweep purely to keep one run predictable — unlike the transcription
# and vision sweeps, nothing here is billable, so this is a sanity limit rather
# than a spend control. A first run against an existing self-chat is the only
# time it matters; after that a sweep handles one or two notes.
DEFAULT_LIMIT = 100

# Same floor as the embedding path (`whatsapp/sync.py::MIN_SELF_NOTE_CHARS`), for
# a different reason: there it stops a useless vector, here it stops "ok" and a
# bare emoji from becoming triage items someone has to dismiss by hand.
MIN_NOTE_CHARS = 12

BUCKET = "text"


def _cutoff() -> datetime | None:
    """Oldest note worth routing, or None for no limit.

    A triage queue is for things still worth acting on. Without this, switching
    the feature on backfills a chat's entire history in a single sweep — the first
    live run put four months of notes into the queue at once, secrets included.
    Bounds routing only: the embedding pass has no equivalent cutoff on purpose,
    because search should reach every note ever written.
    """
    from app.plugin.config_store import plugin_config

    try:
        days = int(plugin_config("inbox").inbox_whatsapp_note_max_age_days or 0)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


def _render(note: dict[str, Any]) -> str:
    """The routed note's file content: provenance frontmatter, then the note.

    Frontmatter rather than a bare body for two reasons. It makes the file
    self-describing if it is later routed into the vault by `inbox_to_vault`,
    which is the likely destination for a captured idea. And it puts the message
    id and timestamp *inside* the hashed content, so two identical notes sent
    weeks apart hash differently and both survive the dedup check — a bare body
    would silently drop the second one.

    `timestamp` is a NOT NULL column, so it is not defended against here. A
    fallback would have to invent a value, and an invented timestamp is worse
    than a failure: it becomes the filename, the inbox sorts by filename, and the
    note would sit at the top of the queue with false provenance. `route_self_notes`
    isolates a raising note and retries it next sweep instead, which loses
    nothing — the message stays in `whatsapp_messages` regardless.
    """
    return (
        "---\n"
        "source: whatsapp-self-chat\n"
        f"message_id: {note['message_id']}\n"
        f"sent: {note['timestamp'].isoformat()}\n"
        "---\n\n"
        f"{(note['body'] or '').strip()}\n"
    )


def _first_line(body: str, limit: int = 200) -> str:
    """A one-line description for the sidecar's `note` field.

    `summarise()` leads with `note`, so this is what shows up in
    `inbox_pending` — the note's own first line is the best available
    description of a note, and unlike an image there is nothing to infer.
    """
    flattened = " ".join((body or "").split())
    return flattened if len(flattened) <= limit else flattened[: limit - 1].rstrip() + "…"


def route_self_notes(session, limit: int = DEFAULT_LIMIT) -> dict[str, int]:
    """Create an inbox item for each self-chat note not already routed.

    Returns counts: considered / routed / skipped / too_short / failed.
    """
    from app.plugin.capabilities import get_capability

    counts = {"considered": 0, "routed": 0, "skipped": 0, "too_short": 0, "failed": 0}

    whatsapp = get_capability("whatsapp.query")

    # No configured self-chat → nothing to do, silently. Without this an
    # unconfigured deployment would log about it every sweep forever.
    notes = whatsapp.self_notes(session, limit=limit, since=_cutoff())
    if not notes:
        return counts

    for note in notes:
        counts["considered"] += 1
        body = (note.get("body") or "").strip()

        if len(body) < MIN_NOTE_CHARS:
            counts["too_short"] += 1
            continue

        try:
            if _route_one(note, body):
                counts["routed"] += 1
            else:
                counts["skipped"] += 1
        except Exception:  # noqa: BLE001
            # One malformed note must not stall the rest of the sweep, and the
            # message stays in `whatsapp_messages` either way — nothing is lost,
            # so this is a retry on the next run, not a dropped capture.
            counts["failed"] += 1
            logger.exception(f"[inbox] failed routing self-note {note.get('message_id')}")

    if counts["routed"] or counts["failed"]:
        logger.info(f"[inbox] whatsapp self-note sweep: {counts}")
    return counts


def _route_one(note: dict[str, Any], body: str) -> bool:
    """Write one note into its owner's inbox. False if already routed."""
    owner_id = note["user_id"]
    content = _render(note)
    raw = content.encode("utf-8")
    digest = scan.content_hash(raw)

    if scan.find_by_hash(digest, owner_id) is not None:
        return False

    # Named from the message's own timestamp, not ingest time, so the inbox's
    # filename-sorted ordering (`iter_pending_files`) reflects when the note was
    # actually captured rather than when the cron happened to notice it.
    sent = note["timestamp"]
    stamp = sent.strftime("%Y%m%d-%H%M%S")
    dest_dir = scan.user_root(owner_id) / BUCKET
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_name = f"{stamp}-wa{digest[:8]}.md"
    dest_path = dest_dir / dest_name

    dest_path.write_bytes(raw)

    scan.write_sidecar(dest_path, {
        "original_filename": "whatsapp-note.md",
        # The field `find_by_hash` reads. Without it this note is re-created on
        # every sweep, forever.
        "sha256": digest,
        "type_hint": BUCKET,
        "source": "whatsapp-self-chat",
        "note": _first_line(body),
        "extra": {
            "message_id": note["message_id"],
            "chat_id": note.get("chat_id"),
            "sent": sent.isoformat(),
            "message_type": note.get("message_type"),
        },
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    })

    scan.record_item(owner_id, f"{BUCKET}/{dest_name}", sha256=digest)

    # Inline rather than waiting for the hourly enrichment cron: these are cheap
    # local text reads, and it means the item is complete the moment it appears.
    try:
        scan.enrich_one(dest_path)
    except Exception:  # noqa: BLE001
        logger.debug(f"[inbox] enrichment failed for {dest_name} (file kept)", exc_info=True)

    return True


async def route_self_notes_task() -> None:
    """Cron entry point (see `manifest.py::background_tasks`)."""
    import asyncio

    from app.db import get_db

    def _run() -> None:
        with get_db().session() as session:
            route_self_notes(session)

    try:
        await asyncio.to_thread(_run)
    except Exception:
        logger.exception("[inbox] whatsapp self-note sweep failed")
