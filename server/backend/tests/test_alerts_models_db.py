"""`alert_events` model round-trip + idempotency (db tier) — needs the real
table from the 2026-09-14 migration. Run this file alone, never under
xdist (see CLAUDE.md's memory note on the db tier)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.integrations.alerts.models import AlertEvent
from app.integrations.alerts.routes import store_alert_events

pytestmark = pytest.mark.db


def _row(**overrides) -> dict:
    base = {
        "fingerprint": "abc123",
        "alertname": "HostDiskFull",
        "status": "firing",
        "severity": "critical",
        "page": "phone",
        "instance": "dockerbox",
        "summary": "Disk 92% full",
        "description": "root fs nearly full",
        "starts_at": datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc),
        "ends_at": None,
        "labels": {"alertname": "HostDiskFull"},
        "annotations": {"summary": "Disk 92% full"},
    }
    base.update(overrides)
    return base


def test_round_trip(db_session):
    inserted = store_alert_events(db_session, [_row()])
    assert inserted == 1

    row = db_session.query(AlertEvent).filter_by(fingerprint="abc123").one()
    assert row.alertname == "HostDiskFull"
    assert row.status == "firing"
    assert row.page == "phone"
    assert row.labels == {"alertname": "HostDiskFull"}


def test_repeat_delivery_is_idempotent(db_session):
    """Alertmanager resends the identical (fingerprint, status, startsAt)
    triple every `repeat_interval` — this must never create a duplicate
    row. This is the mutation-check target: reinstating the bug (dropping
    `on_conflict_do_nothing`, or narrowing its index_elements) makes this
    assert fail with a count of 2, not 1."""
    row = _row(fingerprint="repeat-1")
    first = store_alert_events(db_session, [row])
    second = store_alert_events(db_session, [row])

    assert first == 1
    assert second == 0  # silently absorbed, not inserted again

    count = (
        db_session.query(AlertEvent)
        .filter_by(fingerprint="repeat-1", status="firing")
        .count()
    )
    assert count == 1


def test_different_status_same_fingerprint_is_a_separate_row(db_session):
    """A resolution is a genuinely new fact, not a repeat of the firing —
    the unique index includes `status`, so firing then resolved for the
    same fingerprint is two rows, not a de-dupe collision."""
    fired = _row(fingerprint="lifecycle-1", status="firing")
    resolved = _row(
        fingerprint="lifecycle-1", status="resolved",
        ends_at=datetime(2026, 9, 14, 8, 30, tzinfo=timezone.utc),
    )
    store_alert_events(db_session, [fired])
    inserted = store_alert_events(db_session, [resolved])

    assert inserted == 1
    count = db_session.query(AlertEvent).filter_by(fingerprint="lifecycle-1").count()
    assert count == 2


def test_empty_rows_is_a_no_op(db_session):
    assert store_alert_events(db_session, []) == 0
