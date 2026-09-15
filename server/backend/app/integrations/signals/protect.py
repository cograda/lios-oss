"""Parse a UniFi Protect Alarm Manager webhook payload — defensively.

Researched shape (Protect's Alarm Manager "Webhook" action, current firmware
as of 2026-09): a POST body roughly —

    {
      "alarm": {
        "name": "Comar Front Door",
        "sources": [{"device": "8C:ED:E1:72:F4:13", "type": "include"}],
        "conditions": [{"condition": {"type": "is", "source": "person"}}],
        "triggers": [
          {"key": "person", "device": "8C:ED:E1:72:F4:13",
           "eventId": "...", "timestamp": 1757600000000}
        ],
        "eventPath": "/proxy/protect/api/events/...",
        "eventLocalLink": "https://192.168.1.x/protect/..."
      },
      "timestamp": 1757600000000
    }

Nothing here is guaranteed stable across firmware versions — Ubiquiti does
not publish a schema for this webhook, and the exact keys have moved before
in the wild (some builds nest `triggers` differently, some omit `eventPath`).
So every field is read defensively: a missing or reshaped field degrades the
parse rather than raising, and an unrecognised shape still produces a
`kind="unknown"` result with the whole payload preserved — never a rejection.
The one real hit from the real doorbell tells us more about the true shape
than guessing further would.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# Protect's own `condition.source` values -> this integration's coarse kind
# vocabulary (`models.KINDS`). Anything else falls through to "unknown".
_SOURCE_TO_KIND = {
    "person": "person",
    "vehicle": "vehicle",
    "package": "package",
    "ring": "ring",
    "motion": "motion",
}

# A real "Test Alarm" from Protect's Alarm Manager was observed sending MACs
# UPPERCASE WITHOUT COLONS (`A89C6CB03B50`), while `HOME_SIGNALS_DEVICES`
# config entries are naturally written colon-form
# (`a8:9c:6c:b0:3b:50` — copied straight from HA's device registry). Lower-
# casing alone (the old `_normalise_mac`) does not make those two forms
# equal, so a correctly-configured device would never resolve a name for a
# real trigger. Stripping separators as well as case is what makes both
# sides meet in the middle.
_SEPARATORS_RE = re.compile(r"[:\-\s]")


def normalize_device_key(value: Any) -> str | None:
    """Canonical form for any device identifier this integration sees: a
    payload's `trigger.device`/`sources[].device`, and every key of the
    `signals_devices` config. Lower-cases and strips `:`, `-`, and
    whitespace. Not MAC-specific — an unrecognised placeholder (Protect's
    test alarm sends the literal string `FAKE_MAC` as its trigger device)
    normalises fine too, it just won't match any real device."""
    if not isinstance(value, str) or not value.strip():
        return None
    return _SEPARATORS_RE.sub("", value.strip().lower()) or None


class ParsedSignal:
    __slots__ = ("kind", "device_key", "occurred_at", "sender_event_id", "sources_device_keys")

    def __init__(
        self,
        kind: str,
        device_key: str | None,
        occurred_at: datetime,
        sender_event_id: str | None,
        sources_device_keys: list[str] | None = None,
    ) -> None:
        self.kind = kind
        self.device_key = device_key
        self.occurred_at = occurred_at
        self.sender_event_id = sender_event_id
        # Every `alarm.sources[].device`, normalised — the alarm's
        # *configured* camera(s), as opposed to `device_key` (the
        # *triggering* device, which a Protect "Test Alarm" replaces with a
        # placeholder). `routes.py` uses this list for the sources fallback
        # when the trigger device doesn't resolve to a known device.
        self.sources_device_keys = sources_device_keys or []


def _epoch_ms_to_dt(value: Any) -> datetime | None:
    """Protect timestamps are epoch milliseconds. Accepts an int/float/
    numeric string; anything else returns None so the caller can fall back
    to "now"."""
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return None
    # Guard against a payload that's already in seconds (some Protect
    # firmwares have shipped both) — anything before year ~2001 in ms-epoch
    # terms is almost certainly actually seconds.
    if ms < 1_000_000_000_000:
        ms *= 1000
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_protect(body: dict) -> ParsedSignal:
    """Best-effort parse of one Protect Alarm Manager webhook body.

    Never raises — a body that doesn't match the researched shape at all
    still yields `ParsedSignal(kind="unknown", device_key=None,
    occurred_at=now, sender_event_id=None)`.
    """
    if not isinstance(body, dict):
        return ParsedSignal("unknown", None, datetime.now(timezone.utc), None)

    alarm = body.get("alarm")
    alarm = alarm if isinstance(alarm, dict) else {}

    triggers = alarm.get("triggers")
    trigger = triggers[0] if isinstance(triggers, list) and triggers else {}
    trigger = trigger if isinstance(trigger, dict) else {}

    conditions = alarm.get("conditions")
    condition_entry = conditions[0] if isinstance(conditions, list) and conditions else {}
    condition = (condition_entry or {}).get("condition") if isinstance(condition_entry, dict) else None
    condition = condition if isinstance(condition, dict) else {}

    # kind: prefer the trigger's own "key" (Protect's per-trigger label,
    # e.g. "person"), fall back to the condition's "source".
    kind_source = trigger.get("key") or condition.get("source")
    kind = _SOURCE_TO_KIND.get(str(kind_source).lower(), "unknown") if kind_source else "unknown"

    # sources: every alarm.sources[].device, normalised — the alarm's
    # configured camera(s). Collected regardless of whether the trigger
    # device resolves, so routes.py can fall back to it when the trigger
    # device is a placeholder (Protect's "Test Alarm" sends `FAKE_MAC`) as
    # well as when it's simply absent.
    sources = alarm.get("sources")
    sources_device_keys: list[str] = []
    if isinstance(sources, list):
        for entry in sources:
            if isinstance(entry, dict):
                key = normalize_device_key(entry.get("device"))
                if key is not None:
                    sources_device_keys.append(key)

    # device: prefer the trigger's own device, fall back to the alarm's
    # first configured source when the trigger carries none at all. (A
    # trigger device that's *present* but doesn't match any configured
    # device — e.g. a test-alarm placeholder — is left as the raw trigger
    # value here; routes.py does the config-aware "is it in sources
    # instead?" resolution, since only it has the device config.)
    device_key = normalize_device_key(trigger.get("device"))
    if device_key is None and sources_device_keys:
        device_key = sources_device_keys[0]

    # timestamp: trigger's own, then the body's top-level timestamp, then now.
    occurred_at = _epoch_ms_to_dt(trigger.get("timestamp"))
    if occurred_at is None:
        occurred_at = _epoch_ms_to_dt(body.get("timestamp"))
    if occurred_at is None:
        occurred_at = datetime.now(timezone.utc)

    sender_event_id = trigger.get("eventId")
    sender_event_id = sender_event_id if isinstance(sender_event_id, str) else None

    return ParsedSignal(kind, device_key, occurred_at, sender_event_id, sources_device_keys)
