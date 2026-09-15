"""Household Domains — standing areas of end-to-end responsibility.

Phase B of vault/Projects/lios/Plans/household-ops-and-loops-2026-08.md. A
"domain" (bins, dishwasher, laundry, toilets) is owned end to end by exactly
one person — ownership of the *domain*, not of individual tasks within it
("any clothing anywhere in the house that isn't where it lives is yours").
The test that makes something a domain, per the spec: "it is either done or
it is not."

Structurally this is the same animal as `snags`: a small, closed, DB-
authoritative table with no state machine, described in the plan itself as
"a template to copy, and it is known to work here."

Scoping decision: household-shared visibility, single owner
--------------------------------------------------------------
This is deliberately NOT a `UserOwnedMixin` table. That mixin scopes
*visibility* — it makes a row invisible to everyone except the one user_id
it names, via `scoped_query()`'s auto-injected `WHERE user_id =
current_user_id()`. Domains need the opposite: both household members must
see every domain regardless of who owns it (Sam needs to see her own
domains AND Alex's; the whole point of B1 is mutual legibility). So this
table carries a plain `owner_id` FK — naming who is responsible, never
gating who can read the row — exactly the `snags` precedent (a household-
shared table where `Snag.trade`/`reported_by` name a responsible party
without restricting visibility).

No domain rows are seeded here or in the accompanying migration.
`tests/test_personalisation_guard.py` sweeps every committed default for
household names, project names and private IPs — "laundry, owned by Sam"
is exactly the kind of fact that must be a row created at runtime (via
`household_domain_add`), never a config default or a migration seed.

Standard signal (B3) — self-report only
------------------------------------------
`standard_note` / `standard_updated_at` carry a domain's current
self-reported state ("not currently to standard, need to restock
detergent"). They are written by exactly one code path,
`household_domain_check` in tools.py, which enforces
`current_user_id() == owner_id` before writing — `household_domain_update`
never touches these two columns. This is the conservative reading of the
plan's own warning (household-ops-and-loops-2026-08.md, §Social risk):
"If the system lets one spouse flag the other's domain as below standard,
it has automated the argument." There is deliberately no field, tool, or
code path that lets anyone but the owner write these two columns.

DomainCheck is an append-only self-report log — history only. Nothing in
this package reads it to compute a streak, a miss count, or an "overdue"
flag (Design constraint 1 in the plan: "a skipped cadence must not
accumulate overdue guilt").
"""

from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import UserOwnedMixin


class Domain(Base):
    """One standing area of household responsibility, owned end to end by
    exactly one person. No `UserOwnedMixin` — see module docstring."""

    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The name itself is the stable, human-facing reference (unlike snags'
    # immutable SNAG-0042 UID) — a domain is a named standing area, not an
    # individual occurrence, so there's no separate identity column needed.
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    owner_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), index=True,
    )

    # What "done" means for this domain, in the owner's own words. The
    # spec's own contrast: the dishwasher/lunchboxes are unambiguous
    # one-liners; laundry is a full service ("gather, sort, wash, dry, tidy,
    # fold, iron, put away, plus dry cleaning").
    operational_definition: Mapped[str] = mapped_column(Text)
    # Explicit end-to-end scope clause — what counts as part of this domain,
    # so ownership can't quietly shrink to "just the tasks I remember."
    scope_note: Mapped[str] = mapped_column(Text)
    # Free text, not an enum, and never used in a computed comparison —
    # "daily", "as needed", "every Tuesday". No code path in this package
    # computes "is this domain overdue" from cadence (Design constraint 1).
    cadence: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Ordered process steps ("I need to create processes to do those things
    # effectively") — flat strings, no per-item completion state. Process
    # support, not a second tracked lifecycle.
    checklist: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}",
    )

    # --- Standard signal (B3) — self-report only; see module docstring ---
    standard_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    standard_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return f"<Domain {self.name!r} owner={self.owner_id}>"


class DomainCheck(Base):
    """Append-only self-report log backing a domain's standard signal.

    History only — nothing in this package computes a streak, a miss
    count, or an "overdue" flag from this table (Design constraint 1: a
    skipped cadence must never accumulate visible guilt). `checked_by_id`
    is enforced equal to the domain's `owner_id` at write time in
    `tools.py::household_domain_check_handler`, not here — self-report,
    never a channel for the other household member to log a check-in
    against someone else's domain.
    """

    __tablename__ = "domain_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("domains.id", ondelete="CASCADE"), index=True,
    )
    checked_by_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"),
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True,
    )

    def __repr__(self) -> str:
        return f"<DomainCheck domain={self.domain_id} by={self.checked_by_id}>"


# ---------------------------------------------------------------------------
# Capture inbox — Phase A2/A4 of household-ops-and-loops-2026-08.md
# ---------------------------------------------------------------------------
#
# Five keywords join the shipped `snag`: `task`, `nag`, `discuss`, `surface`,
# `feedback` (the last added Wave 2 N4 — routed to a configured recipient
# rather than reviewed like the other four, see tools.py::
# _notify_feedback_recipient). A message like "nag Tupperware drawer" is
# captured and attributed to whoever sent it. Structurally this borrows
# `snags`' capture shape (windowed
# WhatsApp scan + a source-message table for idempotency) but is scoped the
# OPPOSITE way to `Domain` above:
#
#   - `Domain` is household-shared (no `UserOwnedMixin`) — both users must
#     see every domain regardless of who owns it.
#   - `HouseholdCapture` IS `UserOwnedMixin` — a capture belongs to its
#     sender. A `nag` is addressed to a person, not broadcast to the
#     household; "surface" work is attributed to whoever did it. `user_id`
#     here is the attributed SENDER, resolved from the WhatsApp message
#     (see `capture.py::_attribute_sender_id`) — it is deliberately NOT the
#     id of whichever household member's bridge happened to ingest the
#     message (that's a separate, unrelated user_id, see
#     `HouseholdCaptureSourceMessage` below).
#
# These rows are explicitly NOT written into `Task Backlog.md` — see
# `capture.py`'s module docstring for why a review step is the honest
# minimum here.


class HouseholdCapture(UserOwnedMixin, Base):
    """One captured item: a `task`/`nag`/`discuss`/`surface`-shaped message,
    landed in a review inbox rather than auto-filed into the vault backlog.

    `user_id` (from `UserOwnedMixin`) is the attributed sender, not a
    visibility scope in the `Domain` sense — but per-sender visibility is
    exactly what's wanted here (a `nag` is addressed to a person), so the
    mixin's usual meaning and this table's needs happen to coincide.
    """

    __tablename__ = "household_captures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # task | nag | discuss | surface — see capture.py::KINDS. Not a DB enum:
    # a fifth keyword should be addable without a migration.
    kind: Mapped[str] = mapped_column(String(20), index=True)
    # Full original message/utterance, verbatim.
    raw_text: Mapped[str] = mapped_column(Text)
    # Text after the leading keyword and its separator punctuation, e.g.
    # "Tupperware drawer" from "Nag, Tupperware drawer".
    capture_text: Mapped[str] = mapped_column(Text)
    # whatsapp | voice | typed
    source: Mapped[str] = mapped_column(String(20), index=True)

    # Review step (the honest minimum — see capture.py). History-only in
    # spirit like DomainCheck: nothing computes an overdue/backlog count from
    # an unreviewed capture (Design constraint 1).
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True,
    )

    def __repr__(self) -> str:
        return f"<HouseholdCapture {self.kind} user={self.user_id} {self.capture_text[:30]!r}>"


class HouseholdCaptureSourceMessage(UserOwnedMixin, Base):
    """Which raw WhatsApp messages have already been turned into a capture —
    makes `capture_whatsapp_keywords` idempotent across repeated runs.

    `user_id` here is the BRIDGE ingestion user (whose `whatsapp_messages`
    row this is), matching `snags.SnagSourceMessage` exactly — a shared-group
    message ingested by both household members' bridges shares a
    `message_ref` across their two `whatsapp_messages` rows, so uniqueness
    and the "already captured" join both key on `(user_id, message_ref)`,
    never bare `message_ref` (see `snags` F5 and `tests/test_snag_scoping.py`
    for the bug this avoids). This is a DIFFERENT user_id to
    `HouseholdCapture.user_id` (the attributed sender) — a message ingested
    by Alex's bridge but sent by Sam produces a source-message row with
    `user_id=Alex` and a capture row with `user_id=Sam`.

    Closing the "snag trap": `SnagSourceMessage` is only ever written by
    `snag_capture` itself, so a manually-added snag (`snag_add`) never marks
    its underlying WhatsApp message consumed, and a later scan re-surfaces it
    as a "new" duplicate. `capture.py::capture_whatsapp_keywords` avoids
    reproducing that by also running a content-based dedup check (same kind
    + sender + normalised text, within a recency window) before creating a
    new `HouseholdCapture` — if a matching capture already exists (created
    either by a previous scan OR by hand via `household_capture_add`), the
    incoming message is linked to THAT existing capture here instead of
    spawning a duplicate. Message-ref tracking alone can only protect a
    message this function has already seen once; content dedup also
    protects a message it has never seen whose content someone already
    captured another way.
    """

    __tablename__ = "household_capture_source_messages"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "message_ref", name="uq_household_capture_source_messages_user_ref",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_ref: Mapped[str] = mapped_column(String(200), index=True)
    capture_id: Mapped[int] = mapped_column(
        ForeignKey("household_captures.id", ondelete="CASCADE"), index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
