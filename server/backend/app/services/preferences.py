"""Per-user preference registry and accessor.

The only module that reads or writes `user_preferences`. Mirrors the shape of
`app.plugin.config_store` — a declared schema with types and defaults, plus a
resolver that layers stored values over those defaults — but keyed by user
rather than by integration. See `app/models/user_preferences.py` for why the
two stores are separate.

**Defaults must stay deployment-neutral.** `tests/test_personalisation_guard.py`
sweeps config defaults for household names, project names and private
addresses, and the same rule applies here for the same reason: a default that
names someone's washing machine is that household's data sitting in a
committed file. Where a preference genuinely has no neutral default (appliance
entity ids), the default is empty and the dependent output simply doesn't
render until it's configured — config controls what is *offered*, never what
is rejected retroactively.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.models.user_preferences import UserPreference

logger = logging.getLogger(__name__)

PrefType = Literal["str", "int", "bool", "list_str"]


@dataclass(frozen=True)
class PreferenceSpec:
    """Declared type, default and description for one preference key."""

    type: PrefType
    default: Any
    description: str
    # Free-text grouping, used by the dashboard editor to lay the form out.
    group: str = "general"


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

# Ordered: `daily_note.sections` doubles as the render order of the note, so
# the default here is the canonical section order.
DEFAULT_SECTIONS = [
    "pulse",
    "food",
    "coffee",
    "transport",
    "house",
    "snags",
    "today",
    "tasks",
    "email",
    "whatsapp",
    "meetings",
    "notes",
]

PREFERENCES: dict[str, PreferenceSpec] = {
    "daily_note.sections": PreferenceSpec(
        type="list_str",
        default=DEFAULT_SECTIONS,
        description=(
            "Which daily-note sections to render, in order. This is an upper "
            "bound, not a guarantee — a listed section is still omitted when "
            "the user has no data for it (see `available_sections`)."
        ),
        group="daily_note",
    ),
    "daily_note.focus_count": PreferenceSpec(
        type="int",
        default=5,
        description="Maximum Focus items to suggest and to lock in.",
        group="daily_note",
    ),
    "daily_note.tone": PreferenceSpec(
        type="str",
        default="direct",
        description=(
            "Voice for the briefing's opinion lines — e.g. 'direct', "
            "'gentle', 'terse'. Passed through to the briefing prompt."
        ),
        group="daily_note",
    ),
    "daily_note.lookback_days": PreferenceSpec(
        type="int",
        default=2,
        description=(
            "Baseline lookback for email/WhatsApp/previous notes on Tue–Fri. "
            "Monday and the weekend still extend back to Friday regardless."
        ),
        group="daily_note",
    ),
    "health.profile_path": PreferenceSpec(
        type="str",
        default="Health/Health Profile.md",
        description=(
            "Vault-relative path to the health profile note (goals, injury "
            "context, programming) used for exercise coaching. Empty disables "
            "the coaching line."
        ),
        group="health",
    ),
    "health.track_sleep": PreferenceSpec(
        type="bool",
        default=True,
        description=(
            "Whether this user wears a watch overnight. False omits the Sleep "
            "& Vitals block entirely rather than reporting a night of zeros — "
            "the same plausible-zero failure the daily brief's volatile-health "
            "rule guards against, but for a user who will simply never have "
            "the data rather than one whose phone pushed late."
        ),
        group="health",
    ),
    "daily_note.show_listening": PreferenceSpec(
        type="bool",
        default=True,
        description=(
            "Whether Pulse carries the Listening sub-block. The brief already "
            "skips the source for a user with no scrobbles; this additionally "
            "keeps the instructions for it out of their rendered command."
        ),
        group="daily_note",
    ),
    "daily_note.show_freshness": PreferenceSpec(
        type="bool",
        default=True,
        description=(
            "Whether the note ends with the Data freshness table (per-source "
            "age vs threshold, read from `alerts.data_freshness`). The data is "
            "in every brief regardless — this only governs whether the note "
            "renders it, so a user who doesn't want diagnostics in their "
            "morning read can switch it off without losing the alerts block."
        ),
        group="daily_note",
    ),
    "health.strength_target": PreferenceSpec(
        type="int",
        default=0,
        description=(
            "Strength sessions targeted per week. 0 means no target, and the "
            "Exercise line reports activity without grading it."
        ),
        group="health",
    ),
    "house.appliance_entities": PreferenceSpec(
        type="list_str",
        default=[],
        description=(
            "Home Assistant entity ids whose recent state changes drive the "
            "House laundry/dishwasher lines, e.g. a washing machine's "
            "machine_state sensor. Empty omits those lines entirely — there "
            "is no neutral default, since entity ids are per-home."
        ),
        group="house",
    ),
    "rail.direction": PreferenceSpec(
        type="str",
        default="",
        description=(
            "Direction filter for the Transport section's departure board "
            "(e.g. 'Northbound'). Empty shows all directions."
        ),
        group="transport",
    ),
    # Defaults halved 2026-09-02. Measured on the live brief: at 50/50 the
    # payload was 130k characters, of which mail_recent was 34k and whatsapp
    # 14k — the model client spills anything over ~25k to a file, so a bigger
    # brief is a brief that gets skimmed from disk, not read. 25 mails is still
    # more than a morning triages; raise it per user if a day needs it.
    "comms.mail_limit": PreferenceSpec(
        type="int",
        default=25,
        description="How many recent emails the brief pulls for triage.",
        group="comms",
    ),
    "comms.whatsapp_limit": PreferenceSpec(
        type="int",
        default=30,
        description="How many recent WhatsApp messages the brief pulls for triage.",
        group="comms",
    ),
}


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------


def _coerce(spec: PreferenceSpec, raw: str, key: str) -> Any:
    """Decode a stored JSON value and coerce it to the declared type.

    A malformed or wrong-typed row falls back to the default rather than
    raising. A preference is a display choice; a bad one must never be able to
    take down a daily note, and the alternative — a 500 from a stray manual
    UPDATE — is far worse than a silently sensible default. The fallback is
    logged so it's diagnosable.
    """
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("preference %s: value is not valid JSON, using default", key)
        return spec.default

    try:
        if spec.type == "int":
            return int(value)
        if spec.type == "bool":
            return bool(value)
        if spec.type == "str":
            return str(value)
        if spec.type == "list_str":
            if not isinstance(value, list):
                raise TypeError("expected a list")
            return [str(v) for v in value]
    except (TypeError, ValueError):
        logger.warning(
            "preference %s: stored value %r is not a %s, using default",
            key, value, spec.type,
        )
        return spec.default

    return value


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def get_all(session: Session, user_id: int) -> dict[str, Any]:
    """Every registered preference for `user_id`, defaults filled in.

    Unknown keys still sitting in the table (a renamed preference, say) are
    ignored rather than surfaced — the registry is the contract.
    """
    resolved: dict[str, Any] = {k: v.default for k, v in PREFERENCES.items()}

    rows = (
        session.query(UserPreference)
        .filter(UserPreference.user_id == user_id)
        .all()
    )
    for row in rows:
        spec = PREFERENCES.get(row.key)
        if spec is None:
            continue
        resolved[row.key] = _coerce(spec, row.value, row.key)
    return resolved


def get(session: Session, user_id: int, key: str) -> Any:
    """One preference value, default if unset. Raises on an unknown key."""
    if key not in PREFERENCES:
        raise KeyError(f"Unknown preference {key!r}")
    return get_all(session, user_id)[key]


def set_many(session: Session, user_id: int, values: dict[str, Any]) -> list[str]:
    """Upsert preferences for a user. Returns the keys actually written.

    Validates against the registry first and raises on an unknown key or a
    value that won't coerce — unlike the read path, a *write* should fail
    loudly, because the caller is a human at a form and silently discarding
    their input is the worst outcome.
    """
    unknown = sorted(set(values) - set(PREFERENCES))
    if unknown:
        raise KeyError(f"Unknown preference key(s): {', '.join(unknown)}")

    written: list[str] = []
    for key, value in values.items():
        spec = PREFERENCES[key]
        encoded = json.dumps(value)
        # Round-trip through the same coercion the read path uses, so a value
        # that would silently degrade to the default on read is rejected here
        # instead of being accepted and then ignored.
        if _coerce(spec, encoded, key) != value:
            raise ValueError(
                f"Preference {key!r} expects {spec.type}, got {value!r}"
            )

        row = (
            session.query(UserPreference)
            .filter(UserPreference.user_id == user_id, UserPreference.key == key)
            .first()
        )
        if row is None:
            session.add(UserPreference(user_id=user_id, key=key, value=encoded))
        else:
            row.value = encoded
        written.append(key)

    session.commit()
    return written


def schema() -> list[dict[str, Any]]:
    """The registry as JSON, for the dashboard's preferences editor."""
    return [
        {
            "key": key,
            "type": spec.type,
            "default": spec.default,
            "description": spec.description,
            "group": spec.group,
        }
        for key, spec in PREFERENCES.items()
    ]
