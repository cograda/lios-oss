"""lios#224: the `source` normalisation SQL run by migration
`e6b1f4a9c3d7_tasks_source_vocab_confirmed_at`.

The migration itself runs as part of every db-tier test's schema setup (it's
in the chain `test_db_harness.py` proves is at head with no drift), so this
is not testing "does the migration apply" — it's testing "does the mapping
it applies do what the docstring claims", by inserting rows with the exact
session-debris shapes the live audit found and re-running the same UPDATE
statements the migration's `upgrade()` executes, verbatim, against them.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db

# Copied verbatim from the migration's upgrade() — see that file for the
# ordering rationale (most specific pattern first).
_ALLOWED_SOURCES = (
    "manual", "meeting", "kickoff", "seed", "seed-whatsapp", "voice", "sweep",
    "apple_reminders", "routine", "split", "backlog_import", "someday_import",
    "delegated_import", "legacy",
)


def _run_normalisation(session):
    session.execute(text(
        "UPDATE tasks SET source = 'kickoff' "
        "WHERE source = 'kickoff-triage' OR source LIKE 'triage%'"
    ))
    session.execute(text("UPDATE tasks SET source = 'voice' WHERE source LIKE 'voice memo%'"))
    session.execute(text("UPDATE tasks SET source = 'sweep' WHERE source LIKE 'found-%'"))
    session.execute(text("UPDATE tasks SET source = 'meeting' WHERE source LIKE 'meeting%'"))
    session.execute(text("UPDATE tasks SET source = 'manual' WHERE source IS NULL"))
    allowed_sql = ", ".join(f"'{s}'" for s in _ALLOWED_SOURCES)
    session.execute(text(f"UPDATE tasks SET source = 'legacy' WHERE source NOT IN ({allowed_sql})"))
    session.commit()


def _insert(session, uid: str, source: str | None) -> None:
    """Raw insert, bypassing the ORM/tool-layer validation entirely — the
    point is to plant exactly the session-debris strings the live audit
    found, some of which `_check_source` would now refuse outright."""
    session.execute(text(
        "INSERT INTO tasks (uid, title, status, source, created_at) "
        "VALUES (:uid, :uid, 'next', :source, :now)"
    ), {"uid": uid, "source": source, "now": datetime.now(timezone.utc)})
    session.commit()


@pytest.mark.anyio
async def test_source_normalisation_maps_every_documented_pattern(db_session):
    cases = {
        "TASK-M001": ("kickoff-triage", "kickoff"),
        "TASK-M002": ("triage (2026-09-01)", "kickoff"),
        "TASK-M003": ("voice memo 2026-09-10", "voice"),
        "TASK-M004": ("found-2026-09-07", "sweep"),
        "TASK-M005": ("meeting-2026-09-13", "meeting"),
        "TASK-M006": ("meeting", "meeting"),
        "TASK-M007": (None, "manual"),
        "TASK-M008": ("manual", "manual"),
        "TASK-M009": ("seed-whatsapp", "seed-whatsapp"),
        "TASK-M010": ("something nobody anticipated", "legacy"),
    }
    for uid, (before, _) in cases.items():
        _insert(db_session, uid, before)

    _run_normalisation(db_session)

    rows = dict(db_session.execute(
        text("SELECT uid, source FROM tasks WHERE uid LIKE 'TASK-M%'")
    ).all())
    for uid, (_, expected) in cases.items():
        assert rows[uid] == expected, f"{uid}: expected {expected!r}, got {rows[uid]!r}"


@pytest.mark.anyio
async def test_source_normalisation_never_touches_an_already_allowed_value(db_session):
    """Every exact vocabulary value must survive untouched — the mapping
    must not be so broad it clobbers legitimate data (the `PROTECT`-list
    lesson from the comar-oss sanitiser, applied here: a rule that is too
    eager corrupts silently)."""
    from app.integrations.tasks.models import TASK_SOURCES

    for i, source in enumerate(TASK_SOURCES):
        _insert(db_session, f"TASK-P{i:03d}", source)

    _run_normalisation(db_session)

    rows = dict(db_session.execute(
        text("SELECT uid, source FROM tasks WHERE uid LIKE 'TASK-P%'")
    ).all())
    for i, source in enumerate(TASK_SOURCES):
        assert rows[f"TASK-P{i:03d}"] == source
