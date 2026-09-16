"""`parsing.parse_alerts` (unit tier) — pure payload-shape parsing, no DB.

Covers: firing + resolved alerts, multiple alerts in one delivery, the Go
zero-time `endsAt` sentinel, and malformed entries being skipped rather
than raising.
"""

from __future__ import annotations

from app.integrations.alerts.parsing import parse_alerts

FIRING_ALERT = {
    "status": "firing",
    "labels": {
        "alertname": "HostDiskFull", "severity": "critical", "page": "phone",
        "instance": "dockerbox",
    },
    "annotations": {"summary": "Disk 92% full", "description": "root fs nearly full"},
    "startsAt": "2026-09-14T08:00:00Z",
    "endsAt": "0001-01-01T00:00:00Z",
    "fingerprint": "abc123",
}

RESOLVED_ALERT = {
    "status": "resolved",
    "labels": {"alertname": "LowMemory", "severity": "warning", "host": "turing"},
    "annotations": {"summary": "Memory blip", "description": "transient, self-cleared"},
    "startsAt": "2026-09-14T07:00:00Z",
    "endsAt": "2026-09-14T07:05:00Z",
    "fingerprint": "def456",
}


def test_parses_firing_and_resolved_alerts_in_one_delivery():
    body = {"version": "4", "status": "firing", "alerts": [FIRING_ALERT, RESOLVED_ALERT]}
    rows = parse_alerts(body)
    assert len(rows) == 2

    fired = next(r for r in rows if r["fingerprint"] == "abc123")
    assert fired["alertname"] == "HostDiskFull"
    assert fired["status"] == "firing"
    assert fired["severity"] == "critical"
    assert fired["page"] == "phone"
    assert fired["instance"] == "dockerbox"
    assert fired["summary"] == "Disk 92% full"
    assert fired["ends_at"] is None  # Go zero-time sentinel -> None

    resolved = next(r for r in rows if r["fingerprint"] == "def456")
    assert resolved["status"] == "resolved"
    assert resolved["page"] is None
    assert resolved["instance"] == "turing"  # falls back to `host` label
    assert resolved["ends_at"] is not None


def test_multiple_alerts_same_delivery():
    body = {"alerts": [FIRING_ALERT, FIRING_ALERT, RESOLVED_ALERT]}
    rows = parse_alerts(body)
    assert len(rows) == 3  # parsing doesn't dedupe — that's the DB's job


def test_missing_alerts_key_returns_empty_list():
    assert parse_alerts({}) == []
    assert parse_alerts({"alerts": "not-a-list"}) == []


def test_malformed_entry_is_skipped_not_raised():
    body = {
        "alerts": [
            "not-a-dict",
            {"labels": {}, "status": "firing"},  # missing alertname/fingerprint/startsAt
            FIRING_ALERT,
        ],
    }
    rows = parse_alerts(body)
    assert len(rows) == 1
    assert rows[0]["fingerprint"] == "abc123"


def test_invalid_status_is_skipped():
    bad = {**FIRING_ALERT, "status": "acknowledged", "fingerprint": "zzz"}
    rows = parse_alerts({"alerts": [bad]})
    assert rows == []
