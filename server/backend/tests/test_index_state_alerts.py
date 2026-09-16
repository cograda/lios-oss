"""R4 (2026-09-04): the `index_state` `system_alerts` axis — embedding queue
rebuild state (rebuilding bool + percent complete), derived from
`EmbeddingQueue` pending vs already-embedded `Embedding` rows.

Wave 5.2 (2026-09-05): `rebuilding` no longer means `queue_pending > 0` — the
steady-state queue holds ~3 rows at all times (the 5-minute embedding worker
is always slightly behind), so that flat rule reported a rebuild that wasn't
happening. `rebuilding` is now true when EITHER `queue_pending` exceeds
`system.index_rebuild_pending_threshold` OR the `embedding_reembed` backfill
script (`app/scripts/reembed.py`) has an open `runs` row — see
`app.integrations.system.tools._index_state`.

db tier: real `embeddings`/`embedding_queue`/`runs` rows.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.db


def _seed_embedding(session, source_id: str):
    from app.services.embedding import Embedding

    session.add(Embedding(
        source="vault", source_id=source_id, user_id=None,
        chunk_text="x", content_hash=f"hash-{source_id}",
    ))


def _seed_queue_item(session, source_id: str, status: str):
    from app.services.embedding import EmbeddingQueue

    session.add(EmbeddingQueue(
        source="vault", source_id=source_id, user_id=None,
        content="x", content_hash=f"hash-{source_id}", status=status,
    ))


def _set_threshold(threshold: int) -> None:
    from app.plugin import config_store

    config_store.set_config_value("system", "index_rebuild_pending_threshold", threshold)


def _seed_run(session, *, name: str, started_at, finished_at=None, kind="script"):
    from app.models.runs import Run

    session.add(Run(
        run_id=f"r{abs(hash((name, started_at)))}"[:16],
        kind=kind,
        name=name,
        user_id=None,
        started_at=started_at,
        finished_at=finished_at,
        outcome="ok" if finished_at else "error",
        trigger="cli",
    ))


def test_index_state_present_and_healthy_with_nothing_queued(db_session):
    from app.integrations.system.tools import handle_alerts_household

    _seed_embedding(db_session, "a.md")
    db_session.commit()

    payload = json.loads(handle_alerts_household(db_session, {}))
    state = payload["index_state"]
    assert state["rebuilding"] is False
    assert state["rebuild_reason"] is None
    assert state["percent_complete"] == 100.0
    assert state["queue_pending"] == 0


def test_index_state_not_rebuilding_at_steady_state_lag(db_session):
    """3 rows pending is the measured production steady-state — the whole
    reason the flat `pending > 0` rule was wrong."""
    from app.integrations.system.tools import handle_alerts_household

    _seed_embedding(db_session, "a.md")
    _seed_queue_item(db_session, "b.md", "pending")
    _seed_queue_item(db_session, "c.md", "pending")
    _seed_queue_item(db_session, "d.md", "processing")
    db_session.commit()

    payload = json.loads(handle_alerts_household(db_session, {}))
    state = payload["index_state"]
    assert state["queue_pending"] == 3
    assert state["rebuilding"] is False
    assert state["rebuild_reason"] is None


def test_index_state_reports_rebuilding_above_threshold(db_session):
    from app.integrations.system.tools import handle_alerts_household

    _set_threshold(5)
    _seed_embedding(db_session, "a.md")
    for i in range(6):
        _seed_queue_item(db_session, f"pending-{i}.md", "pending")
    db_session.commit()

    payload = json.loads(handle_alerts_household(db_session, {}))
    state = payload["index_state"]
    assert state["queue_pending"] == 6
    assert state["rebuilding"] is True
    assert state["rebuild_reason"] == "pending_above_threshold"


def test_index_state_reports_rebuilding_with_running_backfill(db_session):
    """0 pending, but the backfill script's `runs` row is still open
    (`finished_at IS NULL`) — `fill-space` bypasses `EmbeddingQueue` entirely,
    so this is the only signal available while it's running."""
    from app.integrations.system.tools import handle_alerts_household

    _seed_embedding(db_session, "a.md")
    _seed_run(
        db_session, name="embedding_reembed",
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        finished_at=None,
    )
    db_session.commit()

    payload = json.loads(handle_alerts_household(db_session, {}))
    state = payload["index_state"]
    assert state["queue_pending"] == 0
    assert state["rebuilding"] is True
    assert state["rebuild_reason"] == "backfill_running"


def test_index_state_not_rebuilding_once_backfill_finished(db_session):
    from app.integrations.system.tools import handle_alerts_household

    _seed_embedding(db_session, "a.md")
    _seed_run(
        db_session, name="embedding_reembed",
        started_at=datetime.now(timezone.utc) - timedelta(hours=1),
        finished_at=datetime.now(timezone.utc) - timedelta(minutes=50),
    )
    db_session.commit()

    payload = json.loads(handle_alerts_household(db_session, {}))
    state = payload["index_state"]
    assert state["rebuilding"] is False
    assert state["rebuild_reason"] is None


def test_index_state_ignores_a_stale_abandoned_backfill_row(db_session):
    """An open row started long before `EMBEDDING_BACKFILL_MAX_AGE_HOURS` ago
    is an abandoned/crashed process, not a live backfill — otherwise a killed
    script would pin `rebuilding: true` forever."""
    from app.integrations.system.tools import handle_alerts_household

    _seed_embedding(db_session, "a.md")
    _seed_run(
        db_session, name="embedding_reembed",
        started_at=datetime.now(timezone.utc) - timedelta(hours=48),
        finished_at=None,
    )
    db_session.commit()

    payload = json.loads(handle_alerts_household(db_session, {}))
    state = payload["index_state"]
    assert state["rebuilding"] is False
    assert state["rebuild_reason"] is None


def test_index_state_reports_errors_separately_from_pending(db_session):
    from app.integrations.system.tools import handle_alerts_household

    _seed_queue_item(db_session, "bad.md", "error")
    db_session.commit()

    payload = json.loads(handle_alerts_household(db_session, {}))
    state = payload["index_state"]
    # An errored (given-up) item is not "still working through it".
    assert state["rebuilding"] is False
    assert state["queue_errors"] == 1


def test_index_state_present_with_a_totally_empty_index(db_session):
    """Never-indexed and fully-indexed must not look the same as
    "unmeasured" — this axis is always present, same reasoning as
    restore_drill/daemon_status."""
    from app.integrations.system.tools import handle_alerts_household

    payload = json.loads(handle_alerts_household(db_session, {}))
    assert payload["index_state"]["percent_complete"] == 100.0
    assert payload["index_state"]["rebuilding"] is False
