"""MCP tools for household Domains. The DB is the source of truth; `Household/
Domains.md` is a generated one-way view, snags-style (see render.py) — added
after B1/B3 originally shipped without one. No Sheets export (unlike
`snags`); Phase B doesn't call for one.

  - household_domains_list: every domain, household-shared (visible to both
    users regardless of owner). Optional owner filter.
  - household_domain_get:   one domain by name, with recent check-in history.
  - household_domain_add:   create a domain — name, single owner, an
    explicit operational definition, and an end-to-end scope clause.
  - household_domain_update: edit structural fields (owner, definition,
    scope, cadence, checklist, name). NEVER touches the self-reported
    standard signal — see household_domain_check.
  - household_domain_check: the ONLY way to write standard_note /
    standard_updated_at, and only the domain's own owner may call it for
    their domain. This is the self-report-only guardrail from the plan's
    §Social risk: a domain's standard signal must never become a channel
    for one household member to flag the other's domain.
  - household_domains_render: force a re-render of the generated vault note
    (see render.py) — add/update/check already re-render on every write, so
    this is a manual recovery path.

`household_domain_add`/`_update`/`_check` each re-render `Household/
Domains.md` after a successful DB write (see render.py) — best-effort, via
`_render()` below, so a vault-write hiccup never fails the write that
already succeeded.

Written as hand-written `CustomTool`s throughout, following the `snags`
precedent exactly: a small, closed registry of ~6 rows doesn't fit
`ListTool`'s "date range + auto per-user-scope" shape (there is no
timestamp a caller would filter on, and the household-shared, single-owner
scoping here is the opposite of what `ListTool`'s auto per-user WHERE does)
any better than snag_list/snag_add/snag_update did.

Phase A2/A4 adds the capture inbox (see `capture.py` for parsing/idempotency
detail and `models.py` for the scoping contrast with Domain above):

  - household_capture_capture: scan WhatsApp for task/nag/discuss/surface/
    feedback-shaped messages and register them (idempotent). Sends a
    best-effort push via `notify.push` for each newly created capture — see
    that handler's docstring for what "confirmation" actually means today,
    given the WhatsApp bridge itself cannot send a reply.
  - household_capture_add: manually add a capture (typed, or transcribed
    from voice) — the JSON return IS the confirmation for this path, since
    whoever calls it is already in a live conversation.
  - household_capture_list: list captures, filterable by kind/source/
    sender/reviewed. Per-user (UserOwnedMixin) — defaults to the caller's
    own captures; `all_senders` opts into seeing the whole household's,
    since a `nag` addressed to you is still something your partner may
    reasonably want to see exists.
  - household_capture_review: mark one or more captures reviewed. This is
    the review step that keeps captures out of Task Backlog.md until a
    human has looked at them (Design constraint 1 — a half-parsed capture
    must never silently become a task that looks overdue).

`feedback` (Wave 2 N4, 2026-09-04) is a fifth kind with a different shape
from the other four: rather than only awaiting review, it is ALSO pushed
straight to a configured recipient (`household`'s `feedback_recipient_user`
config key, resolved via `_resolve_feedback_recipient`) the moment it's
created — from `household_capture_add` and from a WhatsApp `feedback: ...`
scan hit alike — and rendered into its own vault note (`Household/
Feedback.md`, see render.py). "Sam files 'this is hard/flaky/broken',
routed to Alex so he can triage it" is the whole point; waiting for a
review pass would defeat it. It still lands in `household_capture_list`/
`_review` like every other kind — the routing is additional, not a
replacement for the review step.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.integrations.household.capture import KINDS, capture_whatsapp_keywords
from app.integrations.household.models import Domain, DomainCheck, HouseholdCapture
from app.integrations.household.render import render_domains_note, render_feedback_note
from app.models.users import User
from app.plugin.config_store import plugin_config
from app.tools import CustomTool, ToolAnnotations
from app.tools.helpers import iso_or_none, scoped_query, serialize

logger = logging.getLogger(__name__)

_CAPTURE_SOURCES = ("whatsapp", "voice", "typed")

_CAPTURE_FIELDS = [
    "id", "kind", "raw_text", "capture_text", "source", "reviewed",
    "reviewed_at", "created_at",
]

_DOMAIN_FIELDS = [
    "id", "name", "operational_definition", "scope_note", "cadence",
    "checklist", "standard_note", "standard_updated_at",
    "created_at", "updated_at",
]


def _domain_dict(d: Domain, owner: User | None) -> dict[str, Any]:
    out = serialize(
        d, _DOMAIN_FIELDS,
        transforms={
            "standard_updated_at": iso_or_none,
            "created_at": iso_or_none,
            "updated_at": iso_or_none,
        },
    )
    out["owner"] = owner.name if owner else None
    return out


def _resolve_owner(session: Session, owner_name: str) -> User | None:
    return (
        session.query(User)
        .filter(User.name == (owner_name or "").strip().lower(), User.is_active.is_(True))
        .one_or_none()
    )


def _find_domain(session: Session, name: str) -> Domain | None:
    return (
        session.query(Domain)
        .filter(Domain.name.ilike((name or "").strip()))
        .one_or_none()
    )


def _render(session: Session) -> str:
    """Re-render Household/Domains.md. Never raises — a vault-write hiccup
    must not fail the DB write that already succeeded (same contract as
    `snags/tools.py::_render`'s own docstring, minus the Sheets half: this
    package has no Sheets export)."""
    try:
        return render_domains_note(session)
    except Exception:
        logger.exception("[household] domains render failed, continuing")
        return ""


def _render_feedback(session: Session) -> str:
    """Re-render Household/Feedback.md — same never-raise contract as
    `_render` above. Called only when a `feedback`-kind capture is created,
    so an unrelated task/nag/discuss/surface add never touches this file."""
    try:
        return render_feedback_note(session)
    except Exception:
        logger.exception("[household] feedback render failed, continuing")
        return ""


def household_domains_list_handler(session: Session, arguments: dict) -> str:
    q = session.query(Domain)
    if arguments.get("owner"):
        owner = _resolve_owner(session, arguments["owner"])
        if owner is None:
            return json.dumps({"status": "error", "detail": f"no such user {arguments['owner']!r}"})
        q = q.filter(Domain.owner_id == owner.id)
    domains = q.order_by(Domain.name).all()

    owners = {u.id: u for u in session.query(User).all()}
    return json.dumps({
        "count": len(domains),
        "results": [_domain_dict(d, owners.get(d.owner_id)) for d in domains],
    }, default=str)


def household_domain_get_handler(session: Session, arguments: dict) -> str:
    name = arguments.get("name")
    if not name:
        return json.dumps({"status": "error", "detail": "name is required"})
    domain = _find_domain(session, name)
    if domain is None:
        return json.dumps({"status": "error", "detail": f"no domain named {name!r}"})

    owner = session.get(User, domain.owner_id)
    limit = int(arguments.get("recent_checks_limit") or 5)
    checks = (
        session.query(DomainCheck)
        .filter(DomainCheck.domain_id == domain.id)
        .order_by(DomainCheck.created_at.desc())
        .limit(limit)
        .all()
    )
    users_by_id = {u.id: u for u in session.query(User).all()}

    out = _domain_dict(domain, owner)
    out["recent_checks"] = [
        {
            "checked_by": (users_by_id.get(c.checked_by_id).name if users_by_id.get(c.checked_by_id) else None),
            "note": c.note,
            "checked_at": iso_or_none(c.created_at),
        }
        for c in checks
    ]
    return json.dumps(out, default=str)


def household_domain_add_handler(session: Session, arguments: dict) -> str:
    name = (arguments.get("name") or "").strip()
    owner_name = (arguments.get("owner") or "").strip()
    operational_definition = (arguments.get("operational_definition") or "").strip()
    scope_note = (arguments.get("scope_note") or "").strip()
    if not name or not owner_name or not operational_definition or not scope_note:
        return json.dumps({
            "status": "error",
            "detail": "name, owner, operational_definition and scope_note are all required",
        })

    if _find_domain(session, name) is not None:
        return json.dumps({"status": "error", "detail": f"a domain named {name!r} already exists"})

    owner = _resolve_owner(session, owner_name)
    if owner is None:
        return json.dumps({"status": "error", "detail": f"no such user {owner_name!r}"})

    checklist = arguments.get("checklist") or []
    if not isinstance(checklist, list) or not all(isinstance(item, str) for item in checklist):
        return json.dumps({"status": "error", "detail": "checklist must be a list of strings"})

    domain = Domain(
        name=name,
        owner_id=owner.id,
        operational_definition=operational_definition,
        scope_note=scope_note,
        cadence=arguments.get("cadence"),
        checklist=checklist,
    )
    session.add(domain)
    session.commit()
    rendered = _render(session)

    from app.plugin.dispatch import set_affected
    set_affected([f"domain:{domain.name}"])

    return json.dumps({"created": _domain_dict(domain, owner), "rendered": rendered})


def household_domain_update_handler(session: Session, arguments: dict) -> str:
    name = arguments.get("name")
    if not name:
        return json.dumps({"status": "error", "detail": "name is required"})
    domain = _find_domain(session, name)
    if domain is None:
        return json.dumps({"status": "error", "detail": f"no domain named {name!r}"})

    changed: dict[str, Any] = {}

    if arguments.get("owner"):
        owner = _resolve_owner(session, arguments["owner"])
        if owner is None:
            return json.dumps({"status": "error", "detail": f"no such user {arguments['owner']!r}"})
        domain.owner_id = owner.id
        changed["owner"] = owner.name

    for field in ("operational_definition", "scope_note", "cadence"):
        if arguments.get(field) is not None:
            setattr(domain, field, arguments[field])
            changed[field] = arguments[field]

    if arguments.get("checklist") is not None:
        checklist = arguments["checklist"]
        if not isinstance(checklist, list) or not all(isinstance(item, str) for item in checklist):
            return json.dumps({"status": "error", "detail": "checklist must be a list of strings"})
        domain.checklist = checklist
        changed["checklist"] = checklist

    if arguments.get("rename_to"):
        new_name = arguments["rename_to"].strip()
        existing = _find_domain(session, new_name)
        if new_name and existing is not None and existing.id != domain.id:
            return json.dumps({"status": "error", "detail": f"a domain named {new_name!r} already exists"})
        if new_name:
            domain.name = new_name
            changed["name"] = new_name

    if not changed:
        return json.dumps({"status": "error", "detail": "no recognised fields to update"})

    session.commit()
    rendered = _render(session)
    owner = session.get(User, domain.owner_id)

    from app.plugin.dispatch import set_affected
    set_affected([f"domain:{domain.name}"])

    return json.dumps(
        {"updated": _domain_dict(domain, owner), "changed": changed, "rendered": rendered},
        default=str,
    )


def household_domain_check_handler(session: Session, arguments: dict) -> str:
    """Self-report only. Enforces `current_user_id() == domain.owner_id` —
    the ONLY write path onto `standard_note`/`standard_updated_at`
    (`household_domain_update` never touches these two columns). See the
    module docstring and models.py for why: the plan's own §Social risk
    warns this signal must never become a channel for one household member
    to flag the other's domain."""
    name = arguments.get("name")
    if not name:
        return json.dumps({"status": "error", "detail": "name is required"})
    domain = _find_domain(session, name)
    if domain is None:
        return json.dumps({"status": "error", "detail": f"no domain named {name!r}"})

    caller_id = current_user_id()
    if caller_id != domain.owner_id:
        owner = session.get(User, domain.owner_id)
        return json.dumps({
            "status": "error",
            "detail": (
                f"only {owner.name if owner else 'the owner'} can record a "
                f"check-in for {domain.name!r} — self-report only"
            ),
        })

    note = arguments.get("note")
    now = datetime.now(timezone.utc)
    domain.standard_note = note
    domain.standard_updated_at = now
    session.add(DomainCheck(domain_id=domain.id, checked_by_id=caller_id, note=note))
    session.commit()
    rendered = _render(session)

    from app.plugin.dispatch import set_affected
    set_affected([f"domain:{domain.name}"])

    owner = session.get(User, domain.owner_id)
    return json.dumps({"checked": _domain_dict(domain, owner), "rendered": rendered}, default=str)


def household_domains_render_handler(session: Session, arguments: dict) -> str:
    """Force a re-render of Household/Domains.md. Mostly for recovering from
    a best-effort render that failed silently on a prior write (`_render`
    swallows exceptions so a vault-write hiccup never fails the DB write it
    followed) — the normal path already re-renders on every add/update/check."""
    rendered = render_domains_note(session)
    return json.dumps({"rendered": rendered})


# ---------------------------------------------------------------------------
# Capture inbox — Phase A2/A4
# ---------------------------------------------------------------------------


def _capture_dict(c: HouseholdCapture, sender: User | None) -> dict[str, Any]:
    out = serialize(
        c, _CAPTURE_FIELDS,
        transforms={"reviewed_at": iso_or_none, "created_at": iso_or_none},
    )
    out["sender"] = sender.name if sender else None
    return out


def _notify_capture_confirmation(kind: str, capture_text: str, sender: User | None) -> None:
    """Best-effort acknowledgement for a capture created from an async source
    (WhatsApp/voice) — there is no live conversation to hand the JSON return
    to, so it never gets seen unless something pushes it somewhere. This is
    Phase A4: "a captured item must acknowledge itself back or she keeps a
    parallel list" (household-ops-and-loops-2026-08.md).

    Honest limitation: the WhatsApp bridge is read-only (sendMessage,
    sendReadReceipt and presence are all stubbed — see server/CLAUDE.md), so
    this cannot reply in the same thread the message arrived on. What it CAN
    do — and must — is route to the CAPTURER's own device, never the
    household-wide fan-out: `notify.push`'s `user_id` parameter (added
    2026-08-13 alongside the HA sink swap) resolves through `targets`, a
    per-user mapping, whereas `user_id=None` resolves through
    `household_targets`, which both plan docs record as deliberately ONE
    person's device (the alert sweep's fan-out). Passing no `user_id` here
    would silently misroute every confirmation to that one device regardless
    of who actually sent the message — the household-shared bug this
    parameter exists to avoid. If `sender` can't be resolved to a user,
    the push is skipped rather than guessed onto household_targets.

    ⚠️ This calls `notify.push`'s `send()` → `client.publish()` directly —
    the SAME ad-hoc/direct push path notifications' own docs describe as
    unledgered and fire-and-forget. It must NOT go through
    `notifications.sweep`'s ledgered path: that's where PR #28's push-
    boundary gating (persistence gate, re-fire cooldown, quiet hours) lives,
    built for recurring infrastructure alerts. A capture confirmation is a
    one-off acknowledgement of something a person just did, not a recurring
    problem to be rate-limited or held for quiet hours — gating it would
    mean a `nag` sent at 9pm silently getting no confirmation until morning,
    which is exactly the "keeps a parallel list" failure A4 exists to avoid.

    Never raises — mirrors `notifications.facade.send()`'s own contract and
    `inbox.scan._notify_transcribed`'s use of it: a dropped confirmation
    must never fail the capture that triggered it.
    """
    if sender is None:
        logger.debug("[household] capture confirmation skipped: no resolved sender")
        return
    try:
        from app.plugin.capabilities import get_capability

        preview = capture_text[:120]
        get_capability("notify.push").send(
            f"lios: {kind} captured",
            preview,
            "recovery",  # informational, not a problem — lowest priority
            user_id=sender.id,
            source="household",
        )
    except Exception:  # noqa: BLE001
        logger.debug("[household] capture confirmation push unavailable", exc_info=True)


def _resolve_feedback_recipient(session: Session) -> User | None:
    """Who a `feedback`-kind capture notifies — Wave 2 N4: "That should come
    to me first so that I can look at it and triage it and improve it."

    Configured via `household`'s `feedback_recipient_user` (a user *name*,
    resolved at runtime — never a hardcoded user id). If unset, fall back to
    a sensible default rather than silently notifying nobody.

    There is no `is_admin`/role column on `User` (see `app/models/users.py`)
    to fall back on — the household is two flat rows, seeded
    `(1, 'alex', 'Alex')` then `(2, 'sam', 'Sam')`. In the absence of a
    role, the least-surprising fallback is the household's first-created
    active member — the lowest `id`, matching seed order — which resolves
    to Alex today without this module ever naming him. This mirrors the
    existing "other active user" fallback shape in
    `capture.py::_attribute_sender_id` (also `order_by(User.id)`), just
    without excluding anyone: `feedback_recipient_user` unset means "the
    household's default triage contact", not "whoever didn't send this".
    """
    configured = (plugin_config("household").feedback_recipient_user or "").strip()
    if configured:
        user = (
            session.query(User)
            .filter(User.name == configured.lower(), User.is_active.is_(True))
            .one_or_none()
        )
        if user is not None:
            return user
        logger.warning(
            "[household] feedback_recipient_user=%r does not match an active "
            "user; falling back", configured,
        )
    return (
        session.query(User)
        .filter(User.is_active.is_(True))
        .order_by(User.id)
        .first()
    )


def _notify_feedback_recipient(session: Session, capture_text: str, sender: User | None) -> None:
    """Push a `feedback` capture to the configured recipient — never the
    sender, unless sender and recipient happen to be the same person (Alex
    filing feedback to himself is fine, and still records normally either
    way). Deliberately a separate push from `_notify_capture_confirmation`:
    that one tells the SENDER "got it"; this one tells the RECIPIENT
    "something needs your attention" — different audience, different
    message, and a household with `capture_keywords` excluding `feedback`
    from the WhatsApp scan can still get this via `household_capture_add`.

    Never raises — same best-effort contract as
    `_notify_capture_confirmation`; a dropped push must never fail the
    capture that triggered it.
    """
    try:
        recipient = _resolve_feedback_recipient(session)
        if recipient is None:
            logger.debug("[household] feedback recipient unresolved, notification skipped")
            return
        sender_label = sender.display_name if sender else "someone"
        from app.plugin.capabilities import get_capability

        get_capability("notify.push").send(
            f"Feedback from {sender_label}",
            capture_text[:280],
            "recovery",  # a report to triage, not an active incident
            user_id=recipient.id,
            source="household",
        )
    except Exception:  # noqa: BLE001
        logger.debug("[household] feedback notification unavailable", exc_info=True)


def household_capture_capture_handler(session: Session, arguments: dict) -> str:
    since_days = int(arguments.get("since_days") or 7)
    result = capture_whatsapp_keywords(session, since_days=since_days)

    if result["created_items"]:
        users_by_id = {u.id: u for u in session.query(User).all()}
        any_feedback = False
        for item in result["created_items"]:
            sender = users_by_id.get(item["sender_user_id"])
            _notify_capture_confirmation(item["kind"], item["capture_text"], sender)
            if item["kind"] == "feedback":
                _notify_feedback_recipient(session, item["capture_text"], sender)
                any_feedback = True
        if any_feedback:
            _render_feedback(session)
    return json.dumps(result)


def household_capture_add_handler(session: Session, arguments: dict) -> str:
    """Manually add a capture — typed directly, or transcribed from a voice
    memo. The JSON return below IS the confirmation for this path (A4):
    whoever is calling this is in a live conversation and sees the response
    immediately, unlike the async WhatsApp-scan path in
    `household_capture_capture_handler`."""
    kind = (arguments.get("kind") or "").strip().lower()
    capture_text = (arguments.get("capture_text") or "").strip()
    source = (arguments.get("source") or "typed").strip().lower()

    if kind not in KINDS:
        return json.dumps({"status": "error", "detail": f"kind must be one of {list(KINDS)}"})
    if not capture_text:
        return json.dumps({"status": "error", "detail": "capture_text is required"})
    if source not in _CAPTURE_SOURCES:
        return json.dumps({"status": "error", "detail": f"source must be one of {list(_CAPTURE_SOURCES)}"})

    sender_id = current_user_id()
    if arguments.get("sender"):
        sender = (
            session.query(User)
            .filter(User.name == arguments["sender"].strip().lower(), User.is_active.is_(True))
            .one_or_none()
        )
        if sender is None:
            return json.dumps({"status": "error", "detail": f"no such user {arguments['sender']!r}"})
        sender_id = sender.id

    capture = HouseholdCapture(
        kind=kind,
        raw_text=arguments.get("raw_text") or capture_text,
        capture_text=capture_text,
        source=source,
        user_id=sender_id,
    )
    session.add(capture)
    session.commit()

    from app.plugin.dispatch import set_affected
    set_affected([f"household_capture:{capture.id}"])

    sender = session.get(User, sender_id)
    if kind == "feedback":
        _notify_feedback_recipient(session, capture_text, sender)
        _render_feedback(session)
    return json.dumps({"created": _capture_dict(capture, sender)})


def household_capture_list_handler(session: Session, arguments: dict) -> str:
    q = session.query(HouseholdCapture)

    if arguments.get("sender"):
        sender = (
            session.query(User)
            .filter(User.name == arguments["sender"].strip().lower(), User.is_active.is_(True))
            .one_or_none()
        )
        if sender is None:
            return json.dumps({"status": "error", "detail": f"no such user {arguments['sender']!r}"})
        q = q.filter(HouseholdCapture.user_id == sender.id)
    elif not arguments.get("all_senders"):
        q = q.filter(HouseholdCapture.user_id == current_user_id())

    if arguments.get("kind"):
        q = q.filter(HouseholdCapture.kind == arguments["kind"])
    if arguments.get("source"):
        q = q.filter(HouseholdCapture.source == arguments["source"])
    if arguments.get("reviewed") is not None:
        q = q.filter(HouseholdCapture.reviewed == bool(arguments["reviewed"]))

    limit = min(int(arguments.get("limit") or 50), 200)
    rows = q.order_by(HouseholdCapture.created_at.desc()).limit(limit).all()

    users_by_id = {u.id: u for u in session.query(User).all()}
    return json.dumps({
        "count": len(rows),
        "results": [_capture_dict(c, users_by_id.get(c.user_id)) for c in rows],
    }, default=str)


def household_capture_review_handler(session: Session, arguments: dict) -> str:
    ids = arguments.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return json.dumps({"status": "error", "detail": "ids must be a non-empty list of capture ids"})

    now = datetime.now(timezone.utc)
    reviewed_ids: list[int] = []
    any_feedback = False
    for raw_id in ids:
        # Only the caller's own captures are reviewable. `_list` and `_add`
        # deliberately cross users (a nag addressed to you is something your
        # partner may reasonably see exists), but *reviewing* is the owner's
        # act: marking someone else's capture reviewed removes it from their
        # inbox without them ever seeing it (2026-09-06 scoping audit). A
        # foreign id is skipped and reported below, indistinguishably from a
        # nonexistent one.
        capture = (
            scoped_query(session, HouseholdCapture)
            .filter(HouseholdCapture.id == int(raw_id))
            .first()
        )
        if capture is None:
            continue
        capture.reviewed = True
        capture.reviewed_at = now
        reviewed_ids.append(capture.id)
        if capture.kind == "feedback":
            any_feedback = True

    session.commit()
    if any_feedback:
        # Keep Household/Feedback.md's per-item status line in step —
        # otherwise a reviewed item would keep showing "unreviewed" until
        # the next unrelated feedback capture forced a re-render.
        _render_feedback(session)
    # `skipped` is every id that was not reviewed — nonexistent OR another
    # user's — as one list, so the response never reveals whether a foreign
    # id exists. `not_found` is the same list under its original key, kept
    # for callers that already read it.
    skipped = [i for i in ids if int(i) not in reviewed_ids]
    return json.dumps({"reviewed": reviewed_ids, "skipped": skipped, "not_found": skipped})


def mcp_tools() -> list[dict[str, Any]]:
    return [
        CustomTool(
            name="household_domains_list",
            description=(
                "List household domains — standing areas of end-to-end "
                "responsibility (bins, dishwasher, laundry, toilets), each "
                "owned by exactly one person. Visible to every household "
                "member regardless of owner. Optionally filter by owner."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "Filter to one owner's domains, by user name (e.g. 'alex').",
                    },
                },
            },
            handler=household_domains_list_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            category="household",
            examples=["What domains does one household member own?"],
        ).build(),
        CustomTool(
            name="household_domain_get",
            description=(
                "Get one household domain by name, including its checklist, "
                "current self-reported standard note, and recent check-in "
                "history."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "recent_checks_limit": {
                        "type": "integer", "default": 5, "minimum": 0, "maximum": 50,
                    },
                },
                "required": ["name"],
            },
            handler=household_domain_get_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            category="household",
            examples=["Is laundry to standard right now?"],
        ).build(),
        CustomTool(
            name="household_domain_add",
            description=(
                "Add a new household domain: name, single owner, an explicit "
                "operational definition (what 'done' means, in the owner's "
                "own words), and an end-to-end scope clause (ownership is of "
                "the domain, not of individual tasks within it). Optional "
                "cadence and checklist."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "owner": {
                        "type": "string",
                        "description": "User name of whoever owns this domain end to end.",
                    },
                    "operational_definition": {
                        "type": "string",
                        "description": "What 'done' means for this domain, in the owner's own words.",
                    },
                    "scope_note": {
                        "type": "string",
                        "description": "Explicit end-to-end scope — what counts as part of this domain.",
                    },
                    "cadence": {
                        "type": "string",
                        "description": "Free text, e.g. 'daily', 'as needed', 'every Tuesday'.",
                    },
                    "checklist": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Ordered process steps.",
                    },
                },
                "required": ["name", "owner", "operational_definition", "scope_note"],
            },
            handler=household_domain_add_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
            category="household",
            examples=["Add a laundry domain owned by one household member"],
        ).build(),
        CustomTool(
            name="household_domain_update",
            description=(
                "Update a household domain's owner, operational definition, "
                "scope note, cadence, checklist, or name. Never touches the "
                "self-reported standard note — use household_domain_check "
                "for that, which only the domain's own owner can call."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Current name of the domain to update."},
                    "owner": {"type": "string", "description": "Reassign to a different user name."},
                    "operational_definition": {"type": "string"},
                    "scope_note": {"type": "string"},
                    "cadence": {"type": "string"},
                    "checklist": {"type": "array", "items": {"type": "string"}},
                    "rename_to": {"type": "string", "description": "New name for the domain."},
                },
                "required": ["name"],
            },
            handler=household_domain_update_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
            category="household",
            examples=["Reassign the bins domain to Alex"],
        ).build(),
        CustomTool(
            name="household_domain_check",
            description=(
                "Record a self-reported check-in for a domain — an optional "
                "note on its current standard. Self-report only: only the "
                "domain's own owner may call this for their domain (a "
                "deliberate guardrail against one household member flagging "
                "the other's domain). Never computes a streak, a miss count, "
                "or an overdue flag — history only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "note": {"type": "string", "description": "Optional free-text note on current state."},
                },
                "required": ["name"],
            },
            handler=household_domain_check_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
            category="household",
            examples=["Check in on the dishwasher domain"],
        ).build(),
        CustomTool(
            name="household_domains_render",
            description=(
                "Force a re-render of the generated vault note "
                "(Household/Domains.md) from the household database. The "
                "normal write tools already re-render after every add/"
                "update/check; this exists to recover from a best-effort "
                "render that failed silently."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=household_domains_render_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
            category="household",
            examples=["Re-render the household domains vault note"],
        ).build(),
        CustomTool(
            name="household_capture_capture",
            description=(
                "Scan WhatsApp for capture-keyword-shaped messages (the "
                "keyword must lead the message, e.g. 'Nag, Tupperware "
                "drawer', 'Surface: bins', or 'Feedback: the loops app is "
                "slow') and register them in the capture inbox, attributed "
                "to whoever sent them. Which keywords are watched for is "
                "deployment config (default task/discuss/surface/feedback; "
                "'nag' is available but off by default — see the household "
                "integration's capture_keywords config field). Idempotent — "
                "already-captured messages are skipped, and a message whose "
                "content matches an existing capture (however it was "
                "created) is linked rather than duplicated. Best-effort "
                "pushes a confirmation notification to the capturer's own "
                "device for each new capture; a 'feedback' capture ALSO "
                "pushes to the configured feedback recipient for triage."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "since_days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 90},
                },
            },
            handler=household_capture_capture_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
            category="household",
            examples=["Scan WhatsApp for new nags, tasks, discuss items and surfaced work"],
        ).build(),
        CustomTool(
            name="household_capture_add",
            description=(
                "Manually add a captured item — task, nag, discuss, "
                "surface, or feedback — typed directly or transcribed from "
                "voice. Lands in the capture inbox for later review, not "
                "directly in Task Backlog.md. Defaults the sender to the "
                "caller; pass 'sender' to attribute it to someone else "
                "(e.g. entering a nag on their behalf). 'feedback' "
                "additionally pushes straight to the configured feedback "
                "recipient — 'this is hard/flaky/broken' reports for "
                "triage, not just a task."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "capture_text": {"type": "string", "description": "The captured content."},
                    "source": {
                        "type": "string", "enum": list(_CAPTURE_SOURCES), "default": "typed",
                    },
                    "raw_text": {
                        "type": "string",
                        "description": "Original verbatim text/utterance, if different from capture_text.",
                    },
                    "sender": {
                        "type": "string",
                        "description": "User name to attribute this capture to. Defaults to the caller.",
                    },
                },
                "required": ["kind", "capture_text"],
            },
            handler=household_capture_add_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
            category="household",
            examples=["Log a nag about the Tupperware drawer"],
        ).build(),
        CustomTool(
            name="household_capture_list",
            description=(
                "List captured items from the capture inbox (task/nag/"
                "discuss/surface). Defaults to the caller's own captures — "
                "pass all_senders to see the whole household's, or sender "
                "to filter to one person's. Filter by kind, source, or "
                "reviewed state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "source": {"type": "string", "enum": list(_CAPTURE_SOURCES)},
                    "reviewed": {"type": "boolean"},
                    "all_senders": {
                        "type": "boolean", "default": False,
                        "description": "See every household member's captures, not just the caller's.",
                    },
                    "sender": {
                        "type": "string",
                        "description": "Filter to one user's captures by name (implies all_senders).",
                    },
                    "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
                },
            },
            handler=household_capture_list_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
            category="household",
            examples=["What unreviewed nags and surfaced items are in the capture inbox?"],
        ).build(),
        CustomTool(
            name="household_capture_review",
            description=(
                "Mark one or more captures as reviewed — the step that keeps "
                "unreviewed fragments out of Task Backlog.md until a human "
                "has looked at them and decided what, if anything, becomes "
                "a real task."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ids": {
                        "type": "array", "items": {"type": "integer"},
                        "description": "Capture ids to mark reviewed.",
                    },
                },
                "required": ["ids"],
            },
            handler=household_capture_review_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
            category="household",
            examples=["Mark these captures as reviewed"],
        ).build(),
    ]
