"""Capture `task`/`nag`/`discuss`/`surface`-shaped WhatsApp messages into the
household capture inbox — Phase A2 of
vault/Projects/lios/Plans/household-ops-and-loops-2026-08.md.

Reporting convention: the keyword LEADS the message — "nag Tupperware
drawer", "Nag, Tupperware drawer", "Surface: bins", "Task - book dentist" —
followed by optional punctuation/whitespace, then free text. This mirrors
`snags`' `Snag - room - detail` convention but with (up to) four keywords
instead of one and no structured room/trade parsing — the plan is explicit
that these are words, not sigils, because a spoken `$` doesn't survive
transcription but a spoken word does (household-ops-and-loops-2026-08.md,
Design constraint 7).

Which keywords the WhatsApp *scan* actually watches for is deployment
config (`manifest.py::capture_keywords`, read via
`configured_capture_keywords()` below) — default `task`/`discuss`/`surface`.
`nag` is deliberately excluded from that default; see
`configured_capture_keywords()`'s docstring and manifest.py. `KINDS` below
is the full, fixed storage vocabulary (unaffected by config) — it's what
`household_capture_add`'s manual/typed/voice path validates against.

The keyword must lead, not merely appear — "I have a task for you" is
conversation, not a capture. `_keyword_re()` anchors at the start of the
message and requires a word boundary right after the keyword, so
"Nagging headache" / "Tasked with X" / "discussion topic" all correctly
fail to match (the character immediately after the keyword is still a word
character, so there's no boundary).

Captures land in `HouseholdCapture`, NOT `Task Backlog.md` — see the
handler-level tools.py docstring for why a review step is the honest
minimum, per the plan's own §Design constraints (constraint 1: never
manufacture visible failure from a half-parsed capture).

Sender attribution
------------------
A capture is per-user, not household (`HouseholdCapture` carries
`UserOwnedMixin`) — "a nag is addressed to a person". The attributed
sender is NOT the bridge that ingested the message (`w.user_id`, i.e.
whichever household member's WhatsApp session happened to record this
copy) — it's derived from `is_from_me`/`sender_name`:

  - `is_from_me=True`  -> the bridge owner sent it themselves.
  - `is_from_me=False` -> try to resolve `sender_name` against a `User.name`
    (case-insensitive); if that fails, fall back to "whichever other active
    user isn't the bridge owner" — deliberately generic rather than
    hardcoding either household member's name, so it holds for any
    two-or-more-user deployment.

Idempotency and the snag trap
------------------------------
`snag_capture`'s own docstring records a known flaw: `snag_source_messages`
is written ONLY by `snag_capture` itself, so a manually-added snag
(`snag_add`) never marks its underlying WhatsApp message consumed — a later
scan re-surfaces that message as "new" and creates a duplicate snag for the
same defect.

This capture flow closes that gap with a content-based dedup check, in
ADDITION to (not instead of) message-ref tracking: before creating a new
`HouseholdCapture`, look for an existing one with the same
`(kind, sender user_id, normalised capture_text)` created within
`CAPTURE_DEDUP_WINDOW`. If one exists — whether created by a previous run of
this scanner OR by hand via `household_capture_add` — the incoming message
is linked to that existing capture in `HouseholdCaptureSourceMessage` rather
than spawning a duplicate row. Message-ref tracking alone only protects a
message this function has already seen once; content dedup also protects a
message it has NEVER seen whose content someone already captured another
way (exactly the manual-entry-then-late-scan scenario that bit `snags`).

The dedup window is deliberately short (48h): a `nag` that recurs weeks
later is a genuine new occurrence of an ongoing irritation, not a duplicate
of the first one, and must not be silently swallowed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func as sa_func
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.integrations.household.models import HouseholdCapture, HouseholdCaptureSourceMessage
from app.models.users import User
from app.plugin.config_store import plugin_config

logger = logging.getLogger(__name__)

# The full storage vocabulary for HouseholdCapture.kind — fixed, platform
# behaviour, not config. `household_capture_add` (manual/typed/voice, A4)
# validates against this whole tuple regardless of scan configuration: a
# human typing "nag ..." into a live conversation has already made that
# call themselves.
KINDS: tuple[str, ...] = ("task", "nag", "discuss", "surface")

# The WhatsApp *scan*'s default watch-list is a proper subset of KINDS — see
# `configured_capture_keywords()` and manifest.py's `capture_keywords` for
# why `nag` is excluded by default.
DEFAULT_CAPTURE_KEYWORDS: tuple[str, ...] = ("task", "discuss", "surface")

CAPTURE_DEDUP_WINDOW = timedelta(hours=48)


def configured_capture_keywords() -> tuple[str, ...]:
    """Which of `KINDS` the WhatsApp scan watches for, per deployment config.

    Read per call (like `snags/capture.py::_trade_tokens`) so a config
    change via `PUT /api/integrations/household/config` takes effect on the
    next scan without a restart. Falls back to `DEFAULT_CAPTURE_KEYWORDS`
    when unset/empty. Anything configured outside `KINDS` is dropped rather
    than trusted verbatim — a typo in config must not silently build a
    regex alternation containing garbage; if that empties the set, fall back
    to the default rather than watching for nothing.
    """
    configured = plugin_config("household").capture_keywords
    if not configured:
        return DEFAULT_CAPTURE_KEYWORDS
    valid = tuple(kw for kw in configured if kw in KINDS)
    return valid or DEFAULT_CAPTURE_KEYWORDS


def _keyword_re(keywords: tuple[str, ...]) -> re.Pattern[str]:
    """Keyword must LEAD the message (anchored at the start, allowing
    leading whitespace) and be followed by a word boundary — this is what
    rejects "Nagging"/"Tasked"/"discussion" while accepting "Nag,"/
    "Task -"/"Surface:". DOTALL so a multi-line transcribed voice note
    still captures everything after the keyword as one block of text."""
    return re.compile(
        r"^\s*(" + "|".join(keywords) + r")\b[\s,:\-]*(.+)$",
        re.IGNORECASE | re.DOTALL,
    )


def parse_capture_text(text: str, keywords: tuple[str, ...] | None = None) -> dict | None:
    """Parse '<keyword>[,:-]* <free text>'. Returns None if not capture-shaped
    (keyword doesn't lead, or there's no text left after it).

    `keywords` defaults to the full `KINDS` vocabulary — this is the
    general-purpose parser, used directly by `household_capture_add`'s
    manual/typed/voice path and by tests exercising the grammar itself, and
    it should recognise anything a human might type regardless of what the
    unattended WhatsApp scan is configured to watch for. The scan
    (`capture_whatsapp_keywords`) passes its own narrower, config-driven
    `configured_capture_keywords()` set explicitly.
    """
    if not text:
        return None
    active = keywords if keywords is not None else KINDS
    if not active:
        return None
    m = _keyword_re(active).match(text)
    if not m:
        return None
    capture_text = m.group(2).strip()
    if not capture_text:
        return None
    return {"kind": m.group(1).lower(), "capture_text": capture_text}


def _attribute_sender_id(
    session: Session, bridge_user_id: int, sender_name: str | None, is_from_me: bool,
) -> int:
    """Resolve who actually SENT the message, not whose bridge ingested it.

    See the module docstring's "Sender attribution" section.
    """
    if is_from_me:
        return bridge_user_id
    if sender_name:
        match = (
            session.query(User)
            .filter(User.name.ilike((sender_name or "").strip()), User.is_active.is_(True))
            .one_or_none()
        )
        if match:
            return match.id
    other = (
        session.query(User)
        .filter(User.id != bridge_user_id, User.is_active.is_(True))
        .order_by(User.id)
        .first()
    )
    return other.id if other else bridge_user_id


def _find_dedup_match(
    session: Session, *, kind: str, sender_id: int, capture_text: str,
) -> HouseholdCapture | None:
    cutoff = datetime.now(timezone.utc) - CAPTURE_DEDUP_WINDOW
    return (
        session.query(HouseholdCapture)
        .filter(
            HouseholdCapture.kind == kind,
            HouseholdCapture.user_id == sender_id,
            sa_func.lower(HouseholdCapture.capture_text) == capture_text.strip().lower(),
            HouseholdCapture.created_at >= cutoff,
        )
        .order_by(HouseholdCapture.created_at.desc())
        .first()
    )


def capture_whatsapp_keywords(session: Session, since_days: int = 7) -> dict:
    """Scan whatsapp_messages for task/nag/discuss/surface-shaped text/
    captions and register them as HouseholdCapture rows.

    Idempotency is per-user on the BRIDGE side (F5-style, matching
    `snags`): the "already captured" join scopes on
    `(HouseholdCaptureSourceMessage.user_id, message_ref)`, and the source
    scan itself is scoped to `w.user_id`, so a shared-group message ingested
    by both bridges is independently scanned (and independently dedup-
    checked) by each. See the module docstring for the content-based dedup
    layered on top, which is the fix for the known `snags` gap.
    """
    uid = current_user_id()
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    keywords = configured_capture_keywords()

    like_clauses = " OR ".join(
        f"w.body ILIKE :kw{i} OR w.media_caption ILIKE :kw{i}" for i in range(len(keywords))
    )
    params: dict = {f"kw{i}": f"{kw}%" for i, kw in enumerate(keywords)}
    params.update({"cutoff": cutoff, "uid": uid})

    rows = session.execute(sa_text(f"""
        SELECT w.message_id, w.sender_name, w.timestamp, w.is_from_me,
               COALESCE(w.body, w.media_caption) AS msg_text
          FROM whatsapp_messages w
          LEFT JOIN household_capture_source_messages s
                 ON s.message_ref = w.message_id AND s.user_id = :uid
         WHERE w.user_id = :uid
           AND w.timestamp >= :cutoff
           AND s.id IS NULL
           AND ({like_clauses})
         ORDER BY w.timestamp ASC
    """), params).all()

    created = 0
    duplicates = 0
    skipped = 0
    created_items: list[dict] = []

    for r in rows:
        parsed = parse_capture_text(r.msg_text or "", keywords=keywords)
        if not parsed:
            skipped += 1
            continue

        sender_id = _attribute_sender_id(session, uid, r.sender_name, r.is_from_me)
        existing = _find_dedup_match(
            session, kind=parsed["kind"], sender_id=sender_id,
            capture_text=parsed["capture_text"],
        )

        if existing is not None:
            session.add(HouseholdCaptureSourceMessage(
                message_ref=r.message_id, capture_id=existing.id, user_id=uid,
            ))
            duplicates += 1
            continue

        capture = HouseholdCapture(
            kind=parsed["kind"],
            raw_text=r.msg_text,
            capture_text=parsed["capture_text"],
            source="whatsapp",
            user_id=sender_id,
        )
        session.add(capture)
        session.flush()
        # Snapshot before commit — expire_on_commit=True would otherwise
        # invalidate attribute access on `capture` after session.commit().
        created_items.append({
            "id": capture.id,
            "kind": capture.kind,
            "capture_text": capture.capture_text,
            "sender_user_id": capture.user_id,
        })
        session.add(HouseholdCaptureSourceMessage(
            message_ref=r.message_id, capture_id=capture.id, user_id=uid,
        ))
        created += 1

    session.commit()
    return {
        "messages_seen": len(rows),
        "captures_created": created,
        "duplicates_linked": duplicates,
        "not_capture_shaped": skipped,
        "created_items": created_items,
    }
