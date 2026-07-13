"""Render the snag register into the vault as a generated markdown view.

The database is the source of truth; Household/Renovation/Snags.md is a
one-way projection, regenerated after every snag write. Evidence files are
exported from the media store into Attachments/Snags/ named by UID
(SNAG-0042-1.jpg) the first time a snag is rendered, so links are stable and
self-describing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.integrations.media.models import MediaItem
from app.integrations.media import store as media_store
from app.integrations.snags.models import Snag, SnagMedia

logger = logging.getLogger(__name__)

SNAGS_NOTE_PATH = "Household/Renovation/Snags.md"
EVIDENCE_DIR = "Attachments/Snags"

TRADE_LABELS = {
    "windowco": "WindowCo (windows & doors)",
    "painter": "Painter",
    "ken-fergal": "Ken / Fergal",
    "plumber": "Plumber",
    "electrician": "Electrician (Pat)",
    "other": "Other contractor",
    "unknown": "Unassigned",
}
STATUS_BADGES = {
    "open": "⚪ open",
    "reported": "📨 reported",
    "accepted": "🤝 accepted",
    "disputed": "⚔️ disputed",
    "fixed": "🔧 fixed",
    "verified": "✅ verified",
    "closed": "✅ closed",
    "wont-fix": "🚫 won't fix",
}
SEVERITY_BADGES = {"critical": "🔥 critical", "major": "🟠 major", "cosmetic": "🫧 cosmetic"}
OPEN_STATUSES = ("open", "reported", "accepted", "disputed")

# Irish local time for display (DB is UTC). Good enough for a household
# register; revisit if we ever care about DST edges in historical renders.
LOCAL_TZ_OFFSET = timedelta(hours=1)


def _ensure_evidence_exported(session: Session, snag: Snag, evidence_abs: Path) -> list[str]:
    """Export any un-exported evidence for this snag into Attachments/Snags/,
    named <UID>-<n>.<ext>. Returns vault-relative paths for all evidence."""
    links: list[str] = []
    rows = (
        session.query(SnagMedia, MediaItem)
        .join(MediaItem, SnagMedia.media_item_id == MediaItem.id)
        .filter(SnagMedia.snag_id == snag.id)
        .order_by(SnagMedia.id)
        .all()
    )
    n = 0
    for sm, item in rows:
        n += 1
        if sm.vault_path:
            links.append(sm.vault_path)
            continue
        if item.status != "stored":
            if not media_store.download_item(session, item):
                continue
        dest_dir = evidence_abs
        dest_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(item.storage_path).suffix.lstrip(".") or "jpg"
        dest = dest_dir / f"{snag.uid}-{n}.{ext}"
        try:
            import shutil
            shutil.copy2(item.storage_path, dest)
        except OSError as e:
            logger.warning(f"[snags] evidence export failed {snag.uid}: {e}")
            continue
        sm.vault_path = f"{EVIDENCE_DIR}/{dest.name}"
        links.append(sm.vault_path)
    return links


def _line(snag: Snag, links: list[str]) -> str:
    badges = [STATUS_BADGES.get(snag.status, snag.status)]
    if snag.severity in SEVERITY_BADGES:
        badges.append(SEVERITY_BADGES[snag.severity])
    media = " ".join(
        f"[[{p}|{'🎥' if p.endswith(('.mp4', '.3gp')) else '📷'}]]" for p in links
    )
    element = f" *({snag.element})*" if snag.element else ""
    ref = f" `{snag.external_ref}`" if snag.external_ref else ""
    note = f" — *{snag.resolution_note}*" if snag.resolution_note else ""
    return f"- **{snag.uid}** · {' · '.join(badges)} —{element} {snag.title}{ref}{note} {media}".rstrip()


def render_snags_note(session: Session, user_id: int | None = None) -> str:
    """Regenerate the vault note from the DB. Returns the vault-relative path."""
    from app.services.vault_paths import resolve

    note_abs = resolve(SNAGS_NOTE_PATH, user_id_override=user_id)
    evidence_abs = resolve(EVIDENCE_DIR, user_id_override=user_id)

    snags = (
        session.query(Snag)
        .order_by(Snag.trade, Snag.room, Snag.id)
        .all()
    )
    by_trade: dict[str, dict[str, list]] = {}
    counts: dict[str, int] = {}
    for s in snags:
        counts[s.status] = counts.get(s.status, 0) + 1
        by_trade.setdefault(s.trade, {}).setdefault(s.room, []).append(s)

    now_local = datetime.now(timezone.utc) + LOCAL_TZ_OFFSET
    open_total = sum(counts.get(st, 0) for st in OPEN_STATUSES)
    done_total = len(snags) - open_total

    lines = [
        "---",
        "title: Snags",
        "type: note",
        "created: 2026-07-07",
        f"modified: {now_local:%Y-%m-%d}",
        "tags: [renovation, snags, generated]",
        "---",
        "",
        "# Snag Register",
        "",
        "> ⚠️ **Generated from the comar snag database — do not hand-edit.** "
        "Changes go through the `snag_*` tools (add / update / capture); this file "
        f"re-renders after every write. Last rendered {now_local:%Y-%m-%d %H:%M}.",
        "",
        f"**{len(snags)} snags · {open_total} open · {done_total} resolved/closed**"
        + (" · " + " · ".join(f"{STATUS_BADGES.get(k, k)} {v}" for k, v in sorted(counts.items())) if counts else ""),
        "",
        "Evidence photos live in `Attachments/Snags/` named by UID. "
        "See [[Comar Project Board]] for non-snag house tasks.",
    ]

    for trade in sorted(by_trade, key=lambda t: (t == "unknown", t)):
        rooms = by_trade[trade]
        trade_open = sum(
            1 for room in rooms.values() for s in room if s.status in OPEN_STATUSES
        )
        lines += ["", f"## {TRADE_LABELS.get(trade, trade)} ({trade_open} open)"]
        for room in sorted(rooms):
            lines += ["", f"### {room}", ""]
            for s in rooms[room]:
                links = _ensure_evidence_exported(session, s, evidence_abs)
                lines.append(_line(s, links))

    session.commit()  # persist any vault_path set during evidence export
    note_abs.parent.mkdir(parents=True, exist_ok=True)
    note_abs.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(f"[snags] rendered {len(snags)} snags → {SNAGS_NOTE_PATH}")
    return SNAGS_NOTE_PATH
