"""Tests for the per-user preference store (`app.services.preferences`).

Two contracts worth pinning down, and they deliberately differ:

  * **Reads never raise.** A malformed row falls back to the declared default,
    because a bad preference must not be able to take down a daily note.
  * **Writes always raise.** The caller is a human at a form, and silently
    discarding their input — or accepting a value the read path will ignore —
    is the worse outcome.

Plus the rule the personalisation guard enforces in CI: no default may carry
household-specific data.
"""

from __future__ import annotations

import json

import pytest

from app.services import preferences as prefs
from app.services.preferences import PREFERENCES, PreferenceSpec, _coerce

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Coercion (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ptype,stored,expected",
    [
        ("int", "5", 5),
        ("int", "0", 0),
        ("bool", "true", True),
        ("bool", "false", False),
        ("str", '"direct"', "direct"),
        ("list_str", '["a","b"]', ["a", "b"]),
        ("list_str", "[]", []),
    ],
)
def test_coerce_round_trips_each_type(ptype, stored, expected):
    spec = PreferenceSpec(type=ptype, default="SENTINEL", description="")
    assert _coerce(spec, stored, "k") == expected


def test_malformed_json_falls_back_to_default():
    spec = PreferenceSpec(type="int", default=42, description="")
    assert _coerce(spec, "not json at all", "k") == 42


def test_wrong_type_falls_back_to_default():
    """A stored object where a list is declared must not propagate."""
    spec = PreferenceSpec(type="list_str", default=["fallback"], description="")
    assert _coerce(spec, '{"not": "a list"}', "k") == ["fallback"]


def test_int_from_a_non_numeric_string_falls_back():
    spec = PreferenceSpec(type="int", default=3, description="")
    assert _coerce(spec, '"banana"', "k") == 3


# ---------------------------------------------------------------------------
# Registry hygiene
# ---------------------------------------------------------------------------


def test_every_default_matches_its_declared_type():
    """A default that wouldn't survive its own coercion is a latent bug."""
    for key, spec in PREFERENCES.items():
        assert _coerce(spec, json.dumps(spec.default), key) == spec.default, key


def test_no_default_carries_deployment_specific_data():
    """Mirrors tests/test_personalisation_guard.py's rule for config schemas.

    The concrete case this exists for: `house.appliance_entities` is genuinely
    per-home, so its default is empty and the House laundry lines stay off
    until configured — rather than shipping one household's entity ids as
    everyone's default.
    """
    banned_fragments = ("utility_room", "192.168.", "10.0.", "sensor.")
    for key, spec in PREFERENCES.items():
        rendered = json.dumps(spec.default).lower()
        for fragment in banned_fragments:
            assert fragment not in rendered, f"{key} default leaks {fragment!r}"

    assert PREFERENCES["house.appliance_entities"].default == []


def test_sections_default_is_the_canonical_order():
    assert PREFERENCES["daily_note.sections"].default == prefs.DEFAULT_SECTIONS
    # Sections the brief gates on must be nameable.
    for section in ("pulse", "coffee", "transport", "house", "email", "whatsapp"):
        assert section in prefs.DEFAULT_SECTIONS


def test_schema_is_serialisable_for_the_dashboard():
    payload = prefs.schema()
    assert len(payload) == len(PREFERENCES)
    json.dumps(payload)  # must not raise
    assert {"key", "type", "default", "description", "group"} <= set(payload[0])


# ---------------------------------------------------------------------------
# Write validation
# ---------------------------------------------------------------------------


def test_set_many_rejects_an_unknown_key():
    with pytest.raises(KeyError, match="Unknown preference"):
        prefs.set_many(None, 1, {"daily_note.nonsense": 1})


def test_set_many_rejects_a_value_of_the_wrong_type():
    """Accepting a value the read path would silently ignore is worse than
    refusing it at the form."""
    with pytest.raises(ValueError, match="expects list_str"):
        prefs.set_many(None, 1, {"house.appliance_entities": "sensor.washer"})


def test_set_many_rejects_a_non_numeric_int():
    with pytest.raises(ValueError, match="expects int"):
        prefs.set_many(None, 1, {"daily_note.focus_count": "loads"})


# ---------------------------------------------------------------------------
# Persistence (real Postgres)
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_preferences_round_trip_and_are_per_user(db_session):
    """The whole point of a separate table from `integration_config`."""
    prefs.set_many(db_session, 1, {"daily_note.focus_count": 3})
    prefs.set_many(db_session, 2, {"daily_note.focus_count": 9})

    assert prefs.get(db_session, 1, "daily_note.focus_count") == 3
    assert prefs.get(db_session, 2, "daily_note.focus_count") == 9

    # An unset key still resolves to its default, per user.
    assert (
        prefs.get(db_session, 1, "daily_note.tone")
        == PREFERENCES["daily_note.tone"].default
    )


@pytest.mark.db
def test_setting_the_same_key_twice_updates_rather_than_duplicates(db_session):
    prefs.set_many(db_session, 1, {"rail.direction": "Northbound"})
    prefs.set_many(db_session, 1, {"rail.direction": "Southbound"})

    assert prefs.get(db_session, 1, "rail.direction") == "Southbound"

    from app.models.user_preferences import UserPreference

    rows = (
        db_session.query(UserPreference)
        .filter(UserPreference.user_id == 1, UserPreference.key == "rail.direction")
        .all()
    )
    assert len(rows) == 1, "unique (user_id, key) constraint not doing its job"


@pytest.mark.db
def test_list_preferences_survive_the_round_trip(db_session):
    entities = ["sensor.washer_state", "sensor.dryer_state"]
    prefs.set_many(db_session, 1, {"house.appliance_entities": entities})
    assert prefs.get(db_session, 1, "house.appliance_entities") == entities


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture
def ui_client(monkeypatch):
    """An authenticated TestClient for the UI-token-gated `/api/*` surface.

    Pins `settings.ui_token` rather than relying on it being unset. The
    middleware only enforces auth when that value is truthy, so a test that
    leaves it to chance passes in isolation (auth skipped) and 401s in a full
    run once another test has set one — which is exactly what happened here.
    Auth is by the `ui_token` cookie or the `X-UI-Token` header, not
    `Authorization`.
    """
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "ui_token", "test-ui-token")
    client = TestClient(app)
    client.headers.update({"X-UI-Token": "test-ui-token"})
    return client


@pytest.mark.db
def test_preferences_routes_round_trip(real_db, ui_client):
    """GET/PUT /api/preferences/{user_id} — the dashboard's editing surface."""
    client, headers = ui_client, {}

    schema = client.get("/api/preferences/schema", headers=headers)
    assert schema.status_code == 200
    assert len(schema.json()["preferences"]) == len(PREFERENCES)

    put = client.put(
        "/api/preferences/1", json={"daily_note.focus_count": 4}, headers=headers
    )
    assert put.status_code == 200, put.text
    assert put.json()["updated"] == ["daily_note.focus_count"]

    got = client.get("/api/preferences/1", headers=headers)
    assert got.json()["preferences"]["daily_note.focus_count"] == 4


@pytest.mark.db
def test_preferences_route_rejects_a_bad_value(real_db, ui_client):
    """A write must 400, not silently degrade to the default on read."""
    client, headers = ui_client, {}

    resp = client.put(
        "/api/preferences/1",
        json={"house.appliance_entities": "sensor.washer"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "list_str" in resp.text

    resp = client.put(
        "/api/preferences/1", json={"nope.not.a.key": 1}, headers=headers
    )
    assert resp.status_code == 400


@pytest.mark.db
def test_preferences_route_is_behind_the_ui_token(real_db, monkeypatch):
    """The gate is genuinely on — guards against the false-pass above.

    Without pinning `settings.ui_token`, the middleware skips auth entirely
    and every one of these routes is open. Asserting the 401 keeps that
    property from silently regressing.
    """
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "ui_token", "test-ui-token")
    unauthenticated = TestClient(app)

    assert unauthenticated.get("/api/preferences/1").status_code == 401
    assert unauthenticated.put(
        "/api/preferences/1", json={"daily_note.focus_count": 2}
    ).status_code == 401
