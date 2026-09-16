"""`alert_events_since` grouping (db tier) — fired/cleared split, dedupe by
fingerprint, `still_firing`, and the phone-vs-FYI counts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.integrations.alerts.models import AlertEvent
from app.integrations.alerts.service import alert_events_since

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(hours=12)


def _add(session, **overrides):
    base = dict(
        received_at=NOW,
        fingerprint="fp-1",
        alertname="HostDiskFull",
        status="firing",
        severity="critical",
        page=None,
        instance="dockerbox",
        summary="Disk full",
        description=None,
        starts_at=NOW,
        ends_at=None,
        labels={},
        annotations={},
    )
    base.update(overrides)
    session.add(AlertEvent(**base))
    session.commit()


def test_still_firing_alert_appears_in_fired(db_session):
    _add(db_session, fingerprint="fp-fired", page="phone")
    result = alert_events_since(db_session, SINCE)

    assert len(result["fired"]) == 1
    entry = result["fired"][0]
    assert entry["fingerprint"] == "fp-fired"
    assert entry["still_firing"] is True
    assert entry["page"] == "phone"
    assert result["counts"]["fired_phone"] == 1
    assert result["counts"]["fired_fyi"] == 0


def test_resolved_alert_appears_in_cleared_not_fired(db_session):
    _add(
        db_session, fingerprint="fp-cleared", status="resolved",
        ends_at=NOW + timedelta(minutes=5),
    )
    result = alert_events_since(db_session, SINCE)

    assert len(result["cleared"]) == 1
    assert result["cleared"][0]["fingerprint"] == "fp-cleared"
    assert result["fired"] == []
    assert result["counts"]["cleared_fyi"] == 1


def test_fired_then_resolved_in_window_shows_only_in_cleared(db_session):
    """A fingerprint that both fired and cleared inside the window must
    not double-count — the resolution is the more current fact."""
    _add(db_session, fingerprint="fp-lifecycle", status="firing", starts_at=NOW)
    _add(
        db_session, fingerprint="fp-lifecycle", status="resolved",
        starts_at=NOW, ends_at=NOW + timedelta(minutes=10),
        received_at=NOW + timedelta(minutes=10),
    )
    result = alert_events_since(db_session, SINCE)

    assert [e["fingerprint"] for e in result["fired"]] == []
    assert [e["fingerprint"] for e in result["cleared"]] == ["fp-lifecycle"]


def test_fired_before_since_and_resolved_before_since_is_not_still_firing(db_session):
    """A fingerprint resolved entirely BEFORE the window (so neither row is
    `received_at >= since`) must not show up at all — but if a later,
    unrelated re-fire for the same fingerprint lands inside the window, it
    should read as still firing (a fresh incident), not falsely cleared by
    the old resolution whose starts_at predates this one."""
    old_start = SINCE - timedelta(hours=6)
    _add(
        db_session, fingerprint="fp-recur", status="firing",
        starts_at=old_start, received_at=old_start,
    )
    _add(
        db_session, fingerprint="fp-recur", status="resolved",
        starts_at=old_start, ends_at=old_start + timedelta(minutes=5),
        received_at=old_start + timedelta(minutes=5),
    )
    # A fresh firing, inside the window, after the old resolution.
    _add(
        db_session, fingerprint="fp-recur", status="firing",
        starts_at=NOW, received_at=NOW,
    )

    result = alert_events_since(db_session, SINCE)
    assert len(result["fired"]) == 1
    assert result["fired"][0]["still_firing"] is True


def test_counts_split_phone_vs_fyi(db_session):
    _add(db_session, fingerprint="fp-phone", page="phone")
    _add(db_session, fingerprint="fp-fyi", page=None)
    result = alert_events_since(db_session, SINCE)

    assert result["counts"]["fired_total"] == 2
    assert result["counts"]["fired_phone"] == 1
    assert result["counts"]["fired_fyi"] == 1


def test_events_before_since_are_excluded(db_session):
    _add(
        db_session, fingerprint="fp-old",
        received_at=SINCE - timedelta(hours=1), starts_at=SINCE - timedelta(hours=1),
    )
    result = alert_events_since(db_session, SINCE)
    assert result["fired"] == []
    assert result["cleared"] == []
