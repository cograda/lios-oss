"""`signal_events`/`watch_runs` model round-trip (db tier) — needs the real
tables from the 2026-09-11 migration. Run this file alone, never under
xdist, per the repo's db-tier convention (see CLAUDE.md's memory note on
this and `tests/conftest.py`'s DB_FIXTURES auto-marking)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.integrations.signals.models import SignalEvent, WatchRun

pytestmark = pytest.mark.db


def test_signal_event_round_trip(db_session):
    db_session.add(SignalEvent(
        source="protect",
        kind="person",
        device_key="8c:ed:e1:72:f4:13",
        device_name="front_door",
        occurred_at=datetime.now(timezone.utc),
        sender_event_id="evt-1",
        payload={"raw": True},
    ))
    db_session.commit()

    row = db_session.query(SignalEvent).filter(SignalEvent.sender_event_id == "evt-1").one()
    assert row.source == "protect"
    assert row.kind == "person"
    assert row.device_name == "front_door"
    assert row.payload == {"raw": True}


def test_watch_run_round_trip_and_unique_per_watcher_night(db_session):
    run = WatchRun(
        watcher="milk", night_date="2026-09-10",
        opened_at=datetime.now(timezone.utc), status="watching", checks=0,
    )
    db_session.add(run)
    db_session.commit()

    row = db_session.query(WatchRun).filter_by(watcher="milk", night_date="2026-09-10").one()
    assert row.status == "watching"
    assert row.checks == 0
    assert row.confirmed is None

    # A second run for the same watcher+night is rejected — one WatchRun per
    # watcher per night, ever (see models.py's unique index).
    db_session.add(WatchRun(
        watcher="milk", night_date="2026-09-10",
        opened_at=datetime.now(timezone.utc), status="watching", checks=0,
    ))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_watch_run_confirmation_fields(db_session):
    from app.models.users import User

    user = db_session.query(User).first()
    run = WatchRun(
        watcher="milk", night_date="2026-09-12",
        opened_at=datetime.now(timezone.utc), status="detected", checks=2,
        confidence=0.91, model="gemini-3.5-flash-lite",
    )
    db_session.add(run)
    db_session.commit()

    run.confirmed = True
    run.confirmed_by_user_id = user.id if user else None
    run.confirmed_at = datetime.now(timezone.utc)
    run.note = "correct — bottles visible"
    db_session.commit()

    row = db_session.query(WatchRun).filter_by(watcher="milk", night_date="2026-09-12").one()
    assert row.confirmed is True
    assert row.note == "correct — bottles visible"
