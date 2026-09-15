"""system_what_lios_sees (N5) — the per-user "what does lios hold about me"
tool, and its own privacy invariant: never another user's data or ids.

This complements tests/test_user_scoping.py's generic canary sweep (which
already exercises this tool as one of many, since it's `readOnlyHint`) with
assertions specific to this tool's contract: per-user row counts are exactly
what was seeded for THAT caller, the shared-by-design section is exactly
`app.privacy.HOUSEHOLD_SHARED_TABLES` (one source of truth, not two lists
that could drift), and — the mutation-check named in the N5 backlog —
removing the `user_id` filter from the tool's own count query makes this
test fail.
"""

import json

import pytest

from app.auth.context import use_user
from app.privacy import HOUSEHOLD_SHARED_TABLES

pytestmark = pytest.mark.db


def _call(session, user_id: int) -> dict:
    from app.integrations.system.tools import handle_what_lios_sees

    with use_user(user_id):
        out = handle_what_lios_sees(session, {})
    return json.loads(out)


def test_reports_only_the_caller_own_counts_and_no_other_markers(db_session):
    """Seed distinct, marked rows for both users in two different private
    tables; calling as user 2 must report exactly user 2's counts and never
    contain user 1's marker string anywhere in the payload."""
    from app.integrations.apple_reminders.models import Reminder
    from app.integrations.coffee.models import CoffeeBrew, Coffee
    from datetime import datetime, timezone

    u1_marker = "N5-OWN-U1-4f3a"
    u2_marker = "N5-OWN-U2-9b21"

    coffee = Coffee(name="N5 Test Bean", roaster="N5 Roastery")
    db_session.add(coffee)
    db_session.flush()

    # 2 reminders + 1 brew for user 1, 1 reminder + 2 brews for user 2 —
    # deliberately asymmetric counts so a swapped-user bug would show up as
    # a wrong count, not just a coincidentally-matching one.
    db_session.add_all([
        Reminder(user_id=1, uid="n5-u1-a", summary=f"{u1_marker}-a", list_name="Reminders"),
        Reminder(user_id=1, uid="n5-u1-b", summary=f"{u1_marker}-b", list_name="Reminders"),
        Reminder(user_id=2, uid="n5-u2-a", summary=f"{u2_marker}-a", list_name="Reminders"),
        CoffeeBrew(
            user_id=1, coffee_id=coffee.id, method="filter",
            brew_context="home", brewed_at=datetime.now(timezone.utc),
        ),
        CoffeeBrew(
            user_id=2, coffee_id=coffee.id, method="espresso",
            brew_context="home", brewed_at=datetime.now(timezone.utc),
        ),
        CoffeeBrew(
            user_id=2, coffee_id=coffee.id, method="espresso",
            brew_context="home", brewed_at=datetime.now(timezone.utc),
        ),
    ])
    db_session.commit()

    result = _call(db_session, 2)
    raw = json.dumps(result)

    assert u1_marker not in raw, "user 2's report leaked user 1's marker text"

    by_table = {row["table"]: row for row in result["private"]}
    assert by_table["reminders"]["row_count"] == 1
    assert by_table["coffee_brews"]["row_count"] == 2

    # And the reverse direction, same tables, opposite expectation.
    result1 = _call(db_session, 1)
    raw1 = json.dumps(result1)
    assert u2_marker not in raw1, "user 1's report leaked user 2's marker text"
    by_table1 = {row["table"]: row for row in result1["private"]}
    assert by_table1["reminders"]["row_count"] == 2
    assert by_table1["coffee_brews"]["row_count"] == 1


def test_shared_by_design_matches_the_single_source_of_truth(db_session):
    """The tool's `shared_by_design` section must be exactly
    `app.privacy.HOUSEHOLD_SHARED_TABLES` — same tables, same reasons. If
    these ever disagree, Sam is told something different from what the
    scoping test enforces, which is the exact drift N5 exists to close.
    """
    result = _call(db_session, 1)
    reported = {row["table"]: row["reason"] for row in result["shared_by_design"]}
    assert reported == HOUSEHOLD_SHARED_TABLES


def test_every_private_row_is_a_user_owned_model(db_session):
    """The `private` section must be exactly `user_owned_models()` — no
    more, no fewer — so a newly-added UserOwnedMixin table shows up here
    with zero edits to this tool, and nothing sneaks in that isn't
    actually per-user."""
    from app.privacy import user_owned_models

    result = _call(db_session, 1)
    reported_tables = {row["table"] for row in result["private"]}
    expected_tables = {m.__tablename__ for m in user_owned_models()}
    assert reported_tables == expected_tables


def test_mutation_check_unscoped_count_query_would_be_caught(db_session):
    """N5's stated mutation-check: if the tool's count query ever lost its
    `user_id` filter, this test must fail — proving the assertion actually
    exercises the scoping rather than passing vacuously.

    Reminder is seeded 2-for-user-1 / 1-for-user-2: an unscoped count would
    report 3 for user 2 (all rows in the table), a scoped one reports 1.
    Asserting against the scoped figure — and that it differs from the
    unscoped one — is what would catch `handle_what_lios_sees` dropping its
    `.filter(model.user_id == uid)`; verified manually by temporarily
    replacing that filter with a no-op and confirming this test fails (see
    PR body).
    """
    from sqlalchemy import func as sa_func

    from app.integrations.apple_reminders.models import Reminder

    db_session.add_all([
        Reminder(user_id=1, uid="n5-mut-a", summary="a", list_name="Reminders"),
        Reminder(user_id=1, uid="n5-mut-b", summary="b", list_name="Reminders"),
        Reminder(user_id=2, uid="n5-mut-c", summary="c", list_name="Reminders"),
    ])
    db_session.commit()

    unscoped_count = db_session.query(sa_func.count()).select_from(Reminder).scalar()
    scoped_count = (
        db_session.query(sa_func.count())
        .select_from(Reminder)
        .filter(Reminder.user_id == 2)
        .scalar()
    )
    assert unscoped_count != scoped_count, (
        "test setup is broken — an unscoped count must differ from the "
        "scoped one for this mutation-check to mean anything"
    )

    result = _call(db_session, 2)
    by_table = {row["table"]: row for row in result["private"]}
    assert by_table["reminders"]["row_count"] == scoped_count
    assert by_table["reminders"]["row_count"] != unscoped_count
