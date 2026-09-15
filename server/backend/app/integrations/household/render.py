"""Render household Domains, and feedback captures, into the vault as
generated, one-way views.

The database (`Domain`/`DomainCheck`) is the source of truth; `Household/
Domains.md` is a projection, regenerated after every domain write
(add/update/check). This mirrors `snags/render.py` exactly — same banner
convention, same "the DB is authoritative, this file is not" framing — Phase
B's plan doc calls `snags` "a template to copy, and it is known to work
here" (household-ops-and-loops-2026-08.md, Decision 1).

`render_feedback_note` (Wave 2 N4, 2026-09-04) is the same idea applied to
`HouseholdCapture` rows of kind `feedback` only — see its own docstring for
why feedback gets a vault note when the other three review-only kinds
don't.

Idempotent, byte-for-byte
-------------------------
Re-rendering identical content must not touch the file's mtime. `obsidian`'s
vault watcher (fsevents) fires on every write and re-queues the file for
re-embedding — an unconditional `write_text()` on every domain read-adjacent
write would put a static file into a perpetual re-embed loop for no content
change (the vault echo-loop this module is written to avoid). So the
generated text is compared against whatever is already on disk *before* any
write, and a matching file is left untouched — no open, no truncate, no
`mtime` bump, nothing for the watcher to see.

Never show an unmet target nobody set (Design constraint 1)
-------------------------------------------------------------
A domain with no `standard_note`/`standard_updated_at` yet (nobody has ever
called `household_domain_check` for it) renders as **UNREPORTED** — never as
"not to standard" or any other failing-looking state. The plan is explicit
that this distinction is load-bearing: "things that stay, all not working, no
target, they stress me out" applies with more force to a system that is much
better than a human at manufacturing visible failure. `_standard_line` is the
one place that decides this, and it decides on presence of a report, not on
its content — even an owner's own "not currently to standard" note is
rendered as their words, not escalated into a badge the domain didn't ask
for.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.integrations.household.models import Domain, DomainCheck, HouseholdCapture
from app.models.users import User

logger = logging.getLogger(__name__)

DOMAINS_NOTE_PATH = "Household/Domains.md"
FEEDBACK_NOTE_PATH = "Household/Feedback.md"

# Irish local time for display (DB is UTC) — matches snags/render.py's own
# good-enough-for-a-household-register choice.
LOCAL_TZ_OFFSET = timedelta(hours=1)


def _standard_line(domain: Domain) -> str:
    """Design constraint 1: never show an unmet target nobody set. No report
    yet renders as UNREPORTED, never as failing/not-to-standard — the two are
    not the same fact and must never be conflated into one badge."""
    if domain.standard_updated_at is None:
        return "❔ **UNREPORTED** — no self-report logged yet"
    when = domain.standard_updated_at.astimezone(timezone.utc) + LOCAL_TZ_OFFSET
    note = domain.standard_note.strip() if domain.standard_note else None
    if note:
        return f"🗣️ *\"{note}\"* — self-reported {when:%Y-%m-%d}"
    return f"🗣️ Checked in, no note — self-reported {when:%Y-%m-%d}"


def _domain_block(domain: Domain, owner_name: str) -> list[str]:
    lines = [f"## {domain.name}", "", f"**Owner:** {owner_name}"]
    if domain.cadence:
        lines.append(f"**Cadence:** {domain.cadence}")
    lines += ["", "**Operational definition:**", "", domain.operational_definition, ""]
    lines += ["**Scope:**", "", domain.scope_note, ""]
    if domain.checklist:
        lines.append("**Checklist:**")
        lines.append("")
        lines += [f"- {item}" for item in domain.checklist]
        lines.append("")
    lines.append(f"**Standard:** {_standard_line(domain)}")
    return lines


def render_domains_note(session: Session, user_id: int | None = None) -> str:
    """Regenerate `Household/Domains.md` from the DB. Returns the vault-
    relative path. A no-op write (identical content already on disk) leaves
    the file's mtime untouched — see module docstring."""
    from app.services.vault_paths import resolve

    note_abs = resolve(DOMAINS_NOTE_PATH, user_id_override=user_id)

    domains = session.query(Domain).order_by(Domain.name).all()
    owners = {u.id: u for u in session.query(User).all()}

    now_local = datetime.now(timezone.utc) + LOCAL_TZ_OFFSET
    unreported = sum(1 for d in domains if d.standard_updated_at is None)

    lines = [
        "---",
        "title: Domains",
        "type: note",
        "created: 2026-08-28",
        f"modified: {now_local:%Y-%m-%d}",
        "tags: [household, domains, generated]",
        "---",
        "",
        "# Household Domains",
        "",
        "> ⚠️ **Generated from the comar household database — do not hand-edit.** "
        "Changes go through the `household_domain_*` tools (add / update / check); "
        f"this file re-renders after every write. Last rendered {now_local:%Y-%m-%d %H:%M}.",
        "",
        f"**{len(domains)} domains · {unreported} unreported**",
        "",
        "Each domain is owned end to end by exactly one person — ownership of the "
        "*domain*, not of individual tasks within it. The standard line is a "
        "self-report only: no one but the owner can write it.",
    ]

    for domain in domains:
        owner = owners.get(domain.owner_id)
        owner_name = owner.display_name if owner else f"user {domain.owner_id}"
        lines += [""] + _domain_block(domain, owner_name)

    new_content = "\n".join(lines) + "\n"

    if note_abs.exists():
        try:
            existing = note_abs.read_text(encoding="utf-8")
        except OSError:
            existing = None
        if existing is not None and _strip_rendered_at(existing) == _strip_rendered_at(new_content):
            logger.debug(f"[household] {DOMAINS_NOTE_PATH} unchanged, skipping write")
            return DOMAINS_NOTE_PATH

    note_abs.parent.mkdir(parents=True, exist_ok=True)
    note_abs.write_text(new_content, encoding="utf-8")
    logger.info(f"[household] rendered {len(domains)} domains -> {DOMAINS_NOTE_PATH}")
    return DOMAINS_NOTE_PATH


def render_feedback_note(session: Session, user_id: int | None = None) -> str:
    """Regenerate `Household/Feedback.md` from the DB — the vault-rendered
    view of `feedback`-kind captures only (Wave 2 N4). Every other capture
    kind (task/nag/discuss/surface) stays DB-only, surfaced through
    `household_capture_list`/`_review`, not rendered to the vault — see
    `capture.py`'s module docstring for why a review step, not an auto-file,
    is the honest minimum there. `feedback` gets its own note because its
    recipient (Alex, by default) is meant to triage from the vault as much
    as from a push notification, and a push is transient in a way a vault
    note isn't.

    Same idempotent-write contract as `render_domains_note`: identical
    content already on disk is left untouched (no mtime bump, no re-embed).
    """
    from app.services.vault_paths import resolve

    note_abs = resolve(FEEDBACK_NOTE_PATH, user_id_override=user_id)

    items = (
        session.query(HouseholdCapture)
        .filter(HouseholdCapture.kind == "feedback")
        .order_by(HouseholdCapture.created_at.desc())
        .all()
    )
    senders = {u.id: u for u in session.query(User).all()}

    now_local = datetime.now(timezone.utc) + LOCAL_TZ_OFFSET
    unreviewed = sum(1 for c in items if not c.reviewed)

    lines = [
        "---",
        "title: Feedback",
        "type: note",
        "created: 2026-09-04",
        f"modified: {now_local:%Y-%m-%d}",
        "tags: [household, feedback, generated]",
        "---",
        "",
        "# Feedback",
        "",
        "> ⚠️ **Generated from the comar household database — do not hand-edit.** "
        "Filed via household_capture_add/_capture (kind=feedback) or a "
        "WhatsApp message starting 'Feedback: ...'; this file re-renders "
        f"after every new feedback capture. Last rendered {now_local:%Y-%m-%d %H:%M}.",
        "",
        f"**{len(items)} items · {unreviewed} unreviewed**",
        "",
        "\"This is hard / flaky / broken\" reports, routed here for triage — "
        "see household_capture_review to mark one handled.",
    ]

    for item in items:
        sender = senders.get(item.user_id)
        sender_name = sender.display_name if sender else f"user {item.user_id}"
        when = item.created_at.astimezone(timezone.utc) + LOCAL_TZ_OFFSET
        status = "✅ reviewed" if item.reviewed else "🔲 unreviewed"
        lines += [
            "",
            f"## {when:%Y-%m-%d %H:%M} — from {sender_name}",
            "",
            f"**Status:** {status}",
            "",
            item.capture_text,
        ]

    new_content = "\n".join(lines) + "\n"

    if note_abs.exists():
        try:
            existing = note_abs.read_text(encoding="utf-8")
        except OSError:
            existing = None
        if existing is not None and _strip_rendered_at(existing) == _strip_rendered_at(new_content):
            logger.debug(f"[household] {FEEDBACK_NOTE_PATH} unchanged, skipping write")
            return FEEDBACK_NOTE_PATH

    note_abs.parent.mkdir(parents=True, exist_ok=True)
    note_abs.write_text(new_content, encoding="utf-8")
    logger.info(f"[household] rendered {len(items)} feedback items -> {FEEDBACK_NOTE_PATH}")
    return FEEDBACK_NOTE_PATH


def _strip_rendered_at(content: str) -> str:
    """Strip the two timestamp-bearing lines (`modified:` frontmatter, `Last
    rendered ...`) before comparing content for the idempotency check.

    Without this, calling `household_domain_check` twice within the same
    minute with byte-identical domain data would still differ only in the
    render timestamp, and the file would be rewritten every time regardless
    of whether anything a reader cares about changed — exactly the
    echo-loop this module exists to prevent. `snags/render.py` doesn't need
    this trick because its own test suite never asserted the no-op case;
    this one does (see tests/test_household.py::TestDomainsVaultRender), so
    the comparison has to be robust to render-time jitter, not just to
    literally-simultaneous renders.
    """
    return "\n".join(
        line for line in content.splitlines()
        if not line.startswith("modified: ") and not line.startswith("> ⚠️ **Generated")
    )
