"""Strand A (dead-board alerting, `system_alerts` axis 11) end to end.

Plan: `vault/Projects/lios/Plans/house-management-2026-08.md` §"Strand A".
Two hard rules under test, both from that plan and restated in
`app/integrations/system/device_fleet.py`'s module docstring:

- an empty/missing/unparseable registry is a HARD REFUSAL, never a silent
  "no boards, nothing wrong"
- a board with no matching HA data reports `unknown`, never `passing`

db tier — real `ha_entities` rows (HAEntity), read via the
`homeassistant.entities` capability exactly as production does (no mocking
of `get_capability` — the real facade, the real model).
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.db


FLEET_MD_ONE_BOARD = """\
# Fleet inventory — one board, one row

## The fleet

| Board | Room (area slug) | Node name | Entity prefix | Wi-Fi MAC / BT MAC | BSSID pin |
|---|---|---|---|---|---|
| ESP32-C3 SuperMini (`test-board/`) | Test Room (`test_room`) | `test-board` | `test_board_*` | `aa:bb:cc:dd:ee:ff` / `…:01` | none |

## Planned — not yet flashed

| Board | Room | Node name | Config | State |
|---|---|---|---|---|
| ESP32-S3 | Hall | `hall-voice` | `hall-voice.yaml` | not flashed |
"""

FLEET_MD_EMPTY_TABLE = """\
# Fleet inventory

## The fleet

| Board | Room (area slug) | Node name | Entity prefix | Wi-Fi MAC / BT MAC | BSSID pin |
|---|---|---|---|---|---|

## Planned — not yet flashed

| Board | Room | Node name | Config | State |
|---|---|---|---|---|
"""


def _write_fleet_md(tmp_path, content):
    path = tmp_path / "fleet.md"
    path.write_text(content, encoding="utf-8")
    return path


def _seed_entity(session, entity_id, *, state, last_changed=None):
    from app.integrations.homeassistant.models import HAEntity

    session.add(HAEntity(
        entity_id=entity_id,
        domain=entity_id.split(".", 1)[0],
        state=state,
        last_changed=last_changed,
    ))
    session.commit()


CONTRACT_SUFFIXES = ("uptime", "esphome_version", "wifi_signal", "ip_address")


def _seed_board_entities(session, prefix, *, state, last_changed):
    for suffix in CONTRACT_SUFFIXES:
        _seed_entity(session, f"sensor.{prefix}_{suffix}", state=state, last_changed=last_changed)


# ---------------------------------------------------------------------------
# 1. Empty registry refuses
# ---------------------------------------------------------------------------

def test_missing_registry_file_raises(tmp_path):
    from app.integrations.system.device_fleet import FleetRegistryError, load_registry

    with pytest.raises(FleetRegistryError):
        load_registry(tmp_path / "does-not-exist.md")


def test_empty_table_registry_raises(tmp_path):
    from app.integrations.system.device_fleet import FleetRegistryError, load_registry

    path = _write_fleet_md(tmp_path, FLEET_MD_EMPTY_TABLE)
    with pytest.raises(FleetRegistryError):
        load_registry(path)


def test_registry_with_no_fleet_section_raises(tmp_path):
    from app.integrations.system.device_fleet import FleetRegistryError, load_registry

    path = _write_fleet_md(tmp_path, "# Fleet inventory\n\nNothing here.\n")
    with pytest.raises(FleetRegistryError):
        load_registry(path)


def test_evaluate_alert_propagates_empty_registry_as_refusal(db_session, tmp_path):
    """The axis-level contract: `evaluate_alert` must not swallow a refusal
    into a clean report — it must raise, exactly like `load_registry` does,
    so the caller (`system/tools.py`) is forced to render it loudly.
    """
    from app.integrations.system.device_fleet import FleetRegistryError, evaluate_alert

    path = _write_fleet_md(tmp_path, FLEET_MD_EMPTY_TABLE)
    with pytest.raises(FleetRegistryError):
        evaluate_alert(db_session, registry_path=path)


def test_system_alerts_surfaces_empty_registry_as_a_loud_alert(db_session, tmp_path, monkeypatch):
    """End-to-end through `system_alerts`: a refusal must show up as an
    alert entry and a non-None `registry_error` — never as `boards: []`
    with nothing else to say, which would read identically to "no fleet
    configured, all clear".
    """
    from app.integrations.system import device_fleet
    from app.integrations.system.tools import handle_alerts_household

    path = _write_fleet_md(tmp_path, FLEET_MD_EMPTY_TABLE)
    monkeypatch.setattr(device_fleet, "_fleet_md_path", lambda: path)

    payload = json.loads(handle_alerts_household(db_session, {}))

    assert payload["device_fleet"]["registry_error"] is not None
    assert payload["device_fleet"]["boards"] == []
    entry = next((a for a in payload["alerts"] if a["integration"] == "device_fleet"), None)
    assert entry is not None
    assert "registry" in entry["issues"][0]


# ---------------------------------------------------------------------------
# 2. Board with no probe reports unknown
# ---------------------------------------------------------------------------

def test_board_with_no_ha_data_reports_unknown(db_session, tmp_path):
    from app.integrations.system.device_fleet import evaluate_alert

    path = _write_fleet_md(tmp_path, FLEET_MD_ONE_BOARD)
    # Deliberately no HAEntity rows seeded — this board has never synced.

    result = evaluate_alert(db_session, registry_path=path)

    assert len(result["boards"]) == 1
    board = result["boards"][0]
    assert board["node_name"] == "test-board"
    assert board["status"] == "unknown"
    # unknown must never alert — it's neither a clean pass nor a fault
    assert result["issues"] == []


# ---------------------------------------------------------------------------
# 3. A dead board alerts
# ---------------------------------------------------------------------------

def test_dead_board_all_unavailable_alerts(db_session, tmp_path):
    from app.integrations.system.device_fleet import evaluate_alert

    path = _write_fleet_md(tmp_path, FLEET_MD_ONE_BOARD)
    now = datetime.now(timezone.utc)
    _seed_board_entities(db_session, "test_board", state="unavailable", last_changed=now)

    result = evaluate_alert(db_session, registry_path=path)

    board = result["boards"][0]
    assert board["status"] == "dead"
    assert result["issues"]
    assert "test-board" in result["issues"][0]


def test_dead_board_stale_last_changed_alerts(db_session, tmp_path):
    """Available (not `unavailable`) but hasn't changed state inside the
    configured threshold — the second half of the contract's liveness rule.
    """
    from app.integrations.system.device_fleet import evaluate_alert

    path = _write_fleet_md(tmp_path, FLEET_MD_ONE_BOARD)
    stale = datetime.now(timezone.utc) - timedelta(hours=5)
    _seed_board_entities(db_session, "test_board", state="123", last_changed=stale)

    result = evaluate_alert(db_session, registry_path=path)

    board = result["boards"][0]
    assert board["status"] == "dead"
    assert "old" in board["detail"]


def test_system_alerts_pushes_a_dead_board(db_session, tmp_path, monkeypatch):
    from app.integrations.system import device_fleet
    from app.integrations.system.tools import handle_alerts_household

    path = _write_fleet_md(tmp_path, FLEET_MD_ONE_BOARD)
    monkeypatch.setattr(device_fleet, "_fleet_md_path", lambda: path)
    now = datetime.now(timezone.utc)
    _seed_board_entities(db_session, "test_board", state="unavailable", last_changed=now)

    payload = json.loads(handle_alerts_household(db_session, {}))

    assert payload["status"] == "degraded"
    entry = next((a for a in payload["alerts"] if a["integration"] == "device_fleet"), None)
    assert entry is not None
    assert "dead" in entry["issues"][0]
    assert payload["device_fleet"]["boards"][0]["status"] == "dead"


# ---------------------------------------------------------------------------
# 4. A live board does not alert
# ---------------------------------------------------------------------------

def test_live_board_reports_ok_and_does_not_alert(db_session, tmp_path):
    from app.integrations.system.device_fleet import evaluate_alert

    path = _write_fleet_md(tmp_path, FLEET_MD_ONE_BOARD)
    now = datetime.now(timezone.utc)
    _seed_board_entities(db_session, "test_board", state="1234", last_changed=now)

    result = evaluate_alert(db_session, registry_path=path)

    board = result["boards"][0]
    assert board["status"] == "ok"
    assert result["issues"] == []


def test_system_alerts_stays_clean_for_a_live_fleet(db_session, tmp_path, monkeypatch):
    from app.integrations.system import device_fleet
    from app.integrations.system.tools import handle_alerts_household

    path = _write_fleet_md(tmp_path, FLEET_MD_ONE_BOARD)
    monkeypatch.setattr(device_fleet, "_fleet_md_path", lambda: path)
    now = datetime.now(timezone.utc)
    _seed_board_entities(db_session, "test_board", state="1234", last_changed=now)

    payload = json.loads(handle_alerts_household(db_session, {}))

    entry = next((a for a in payload["alerts"] if a["integration"] == "device_fleet"), None)
    assert entry is None
    assert payload["device_fleet"]["boards"][0]["status"] == "ok"
    assert payload["device_fleet"]["registry_error"] is None


# ---------------------------------------------------------------------------
# Per-board threshold override (Strand A4)
# ---------------------------------------------------------------------------

def test_per_board_stale_override_tolerates_a_longer_gap(db_session, tmp_path):
    """A board with a configured longer threshold must not go `dead` at the
    household default — the whole point of A4 (a battery/solar or
    deliberately-intermittent board would false-alarm on a fixed window).
    """
    from app.integrations.system.device_fleet import evaluate_alert
    from app.models.integration_config import IntegrationConfig

    path = _write_fleet_md(tmp_path, FLEET_MD_ONE_BOARD)
    stale = datetime.now(timezone.utc) - timedelta(hours=2)  # > default 30m
    _seed_board_entities(db_session, "test_board", state="123", last_changed=stale)

    db_session.add(IntegrationConfig(
        integration="system",
        key="device_fleet_stale_minutes_overrides",
        value=json.dumps({"test-board": "180"}),
    ))
    db_session.commit()

    result = evaluate_alert(db_session, registry_path=path)

    board = result["boards"][0]
    assert board["status"] == "ok"
