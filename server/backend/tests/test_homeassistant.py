"""Home Assistant integration tests.

Unit tier: pure helpers (numeric rule, SECTIONS matching, area-map parsing).
db tier: sync upsert + change-detection and the tool handlers against real
Postgres (the JSONB columns and query chains are what we want exercised).
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.integrations.homeassistant import client as ha_client
from app.integrations.homeassistant import tools as ha_tools
from app.integrations.homeassistant.events import apply_state_event
from app.integrations.homeassistant.models import HAEntity, HAStateChange
from app.integrations.homeassistant.sync import _is_numeric, sync_home_assistant
from app.integrations.homeassistant.tools import (
    Section,
    handle_entities,
    handle_entity,
    handle_history,
    handle_home_status,
)


# ---------------------------------------------------------------------------
# Unit tier — pure helpers
# ---------------------------------------------------------------------------

class TestNumericRule:
    def test_numbers_are_numeric(self):
        assert _is_numeric("21.5")
        assert _is_numeric("0")
        assert _is_numeric("-3")

    def test_states_are_not(self):
        assert not _is_numeric("on")
        assert not _is_numeric("unavailable")
        assert not _is_numeric(None)
        assert not _is_numeric("")


class TestSectionMatching:
    def _entity(self, entity_id, state="on", device_class=None):
        return HAEntity(
            entity_id=entity_id,
            domain=entity_id.split(".", 1)[0],
            state=state,
            device_class=device_class,
        )

    def test_domain_match(self):
        section = Section("presence", domains=frozenset({"person"}))
        assert section.matches(self._entity("person.alex", state="home"))
        assert not section.matches(self._entity("light.kitchen"))

    def test_glob_match(self):
        section = Section("water", globs=("switch.example_sprinkler_*",))
        assert section.matches(self._entity("switch.example_sprinkler_1"))
        assert not section.matches(self._entity("switch.kettle"))

    def test_only_states_filter(self):
        section = Section(
            "lights_switches",
            domains=frozenset({"light"}),
            only_states=frozenset({"on"}),
        )
        assert section.matches(self._entity("light.hall", state="on"))
        assert not section.matches(self._entity("light.hall", state="off"))

    def test_exclude_states_filter(self):
        section = Section(
            "media",
            domains=frozenset({"media_player"}),
            exclude_states=frozenset({"off", "idle", "standby"}),
        )
        assert section.matches(self._entity("media_player.homepod", state="playing"))
        assert not section.matches(self._entity("media_player.homepod", state="idle"))

    def test_device_class_match(self):
        section = Section("climate", device_classes=frozenset({"temperature"}))
        assert section.matches(
            self._entity("sensor.office_temp", state="21.5", device_class="temperature")
        )


class TestAreaMapParsing:
    def test_bad_json_returns_empty(self):
        with patch.object(ha_client, "render_template", return_value="not json"):
            assert ha_client.fetch_area_map() == {}

    def test_entities_without_area_dropped(self):
        rendered = json.dumps({"light.hall": "Hallway", "sensor.uptime": ""})
        with patch.object(ha_client, "render_template", return_value=rendered):
            assert ha_client.fetch_area_map() == {"light.hall": "Hallway"}

    def test_http_error_returns_empty_states(self, monkeypatch):
        monkeypatch.setattr(ha_client.settings, "ha_url", "http://127.0.0.1:1")
        monkeypatch.setattr(ha_client.settings, "ha_token", "x")
        assert ha_client.fetch_states() == []


# ---------------------------------------------------------------------------
# db tier — sync + tools against real Postgres
# ---------------------------------------------------------------------------

def _ha_state(entity_id, state, attributes=None, last_changed="2026-07-02T08:00:00+00:00"):
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": attributes or {},
        "last_changed": last_changed,
    }


def _run_sync(db_session, states, areas=None):
    with (
        patch(
            "app.integrations.homeassistant.sync.fetch_states",
            return_value=states,
        ),
        patch(
            "app.integrations.homeassistant.sync.fetch_area_map",
            return_value=areas or {},
        ),
    ):
        sync_home_assistant(db_session)


@pytest.mark.db
class TestSyncChangeDetection:
    def test_first_sync_creates_entities_no_history(self, db_session):
        _run_sync(db_session, [
            _ha_state("light.hall", "off", {"friendly_name": "Hall Light"}),
            _ha_state("sensor.office_temp", "21.5", {"device_class": "temperature", "unit_of_measurement": "°C"}),
        ], areas={"light.hall": "Hallway"})

        entities = {e.entity_id: e for e in db_session.query(HAEntity).all()}
        assert set(entities) == {"light.hall", "sensor.office_temp"}
        assert entities["light.hall"].area == "Hallway"
        assert entities["light.hall"].friendly_name == "Hall Light"
        assert entities["sensor.office_temp"].unit == "°C"
        assert db_session.query(HAStateChange).count() == 0

    def test_non_numeric_transition_recorded(self, db_session):
        _run_sync(db_session, [_ha_state("light.hall", "off")])
        _run_sync(db_session, [_ha_state("light.hall", "on")])

        changes = db_session.query(HAStateChange).all()
        assert len(changes) == 1
        assert (changes[0].old_state, changes[0].new_state) == ("off", "on")
        assert db_session.query(HAEntity).filter_by(entity_id="light.hall").one().state == "on"

    def test_numeric_tick_not_recorded(self, db_session):
        _run_sync(db_session, [_ha_state("sensor.office_temp", "21.5")])
        _run_sync(db_session, [_ha_state("sensor.office_temp", "21.7")])

        assert db_session.query(HAStateChange).count() == 0
        assert db_session.query(HAEntity).one().state == "21.7"

    def test_numeric_to_unavailable_recorded(self, db_session):
        _run_sync(db_session, [_ha_state("sensor.office_temp", "21.5")])
        _run_sync(db_session, [_ha_state("sensor.office_temp", "unavailable")])

        changes = db_session.query(HAStateChange).all()
        assert len(changes) == 1
        assert changes[0].new_state == "unavailable"

    def test_unchanged_state_records_nothing(self, db_session):
        _run_sync(db_session, [_ha_state("light.hall", "on")])
        _run_sync(db_session, [_ha_state("light.hall", "on")])
        assert db_session.query(HAStateChange).count() == 0

    def test_removed_entity_deleted(self, db_session):
        _run_sync(db_session, [_ha_state("light.hall", "on"), _ha_state("light.shed", "off")])
        _run_sync(db_session, [_ha_state("light.hall", "on")])
        ids = [e.entity_id for e in db_session.query(HAEntity).all()]
        assert ids == ["light.hall"]

    def test_empty_fetch_skips_sync(self, db_session):
        _run_sync(db_session, [_ha_state("light.hall", "on")])
        _run_sync(db_session, [])  # HA unreachable → keep existing data
        assert db_session.query(HAEntity).count() == 1


def _event(entity_id, old, new, attrs=None):
    """Build a state_changed event payload as HA sends it."""
    def _state(s):
        if s is None:
            return None
        return {
            "entity_id": entity_id,
            "state": s,
            "attributes": attrs or {},
            "last_changed": "2026-07-02T09:00:00+00:00",
        }
    return {"entity_id": entity_id, "old_state": _state(old), "new_state": _state(new)}


@pytest.mark.db
class TestApplyStateEvent:
    def _seed(self, db_session, entity_id="light.hall", state="off"):
        db_session.add(HAEntity(
            entity_id=entity_id, domain=entity_id.split(".")[0],
            state=state, synced_at=datetime.now(timezone.utc),
        ))
        db_session.commit()

    def test_transition_updates_state_and_records(self, real_db, db_session):
        self._seed(db_session, "light.hall", "off")
        apply_state_event(_event("light.hall", "off", "on"))

        row = db_session.query(HAEntity).filter_by(entity_id="light.hall").one()
        db_session.refresh(row)
        assert row.state == "on"
        change = db_session.query(HAStateChange).one()
        assert (change.old_state, change.new_state) == ("off", "on")

    def test_numeric_tick_updates_without_history(self, real_db, db_session):
        self._seed(db_session, "sensor.temp", "21.5")
        apply_state_event(_event("sensor.temp", "21.5", "21.7"))

        row = db_session.query(HAEntity).filter_by(entity_id="sensor.temp").one()
        db_session.refresh(row)
        assert row.state == "21.7"
        assert db_session.query(HAStateChange).count() == 0

    def test_numeric_history_flag_records_ticks(self, real_db, db_session, monkeypatch):
        from app.integrations.homeassistant import sync as ha_sync

        monkeypatch.setattr(ha_sync.settings, "ha_record_numeric_history", True)
        self._seed(db_session, "sensor.temp", "21.5")
        apply_state_event(_event("sensor.temp", "21.5", "21.7"))
        assert db_session.query(HAStateChange).count() == 1

    def test_attribute_only_change_no_history(self, real_db, db_session):
        self._seed(db_session, "sun.sun", "below_horizon")
        apply_state_event(_event(
            "sun.sun", "below_horizon", "below_horizon", attrs={"elevation": -12.3}
        ))

        row = db_session.query(HAEntity).filter_by(entity_id="sun.sun").one()
        db_session.refresh(row)
        assert row.attributes == {"elevation": -12.3}
        assert db_session.query(HAStateChange).count() == 0

    def test_unknown_entity_created(self, real_db, db_session):
        apply_state_event(_event("switch.new_device", None, "on"))
        row = db_session.query(HAEntity).filter_by(entity_id="switch.new_device").one()
        assert row.state == "on"
        assert row.domain == "switch"
        # no old_state → new entity, not a transition
        assert db_session.query(HAStateChange).count() == 0

    def test_removed_entity_deleted(self, real_db, db_session):
        self._seed(db_session, "light.gone", "on")
        apply_state_event({"entity_id": "light.gone", "old_state": None, "new_state": None})
        assert db_session.query(HAEntity).count() == 0


@pytest.mark.db
class TestHomeStatus:
    def _seed(self, db_session):
        now = datetime.now(timezone.utc)
        rows = [
            HAEntity(entity_id="person.alex", domain="person", state="home",
                     friendly_name="Alex", synced_at=now),
            HAEntity(entity_id="light.hall", domain="light", state="on", synced_at=now),
            HAEntity(entity_id="light.shed", domain="light", state="off", synced_at=now),
            HAEntity(entity_id="media_player.homepod", domain="media_player",
                     state="playing", synced_at=now),
            HAEntity(entity_id="media_player.frame_tv", domain="media_player",
                     state="off", synced_at=now),
            HAEntity(entity_id="switch.example_sprinkler_1", domain="switch",
                     state="off", synced_at=now),
            HAEntity(entity_id="sensor.utility_room_washing_machine_completion_time", domain="sensor",
                     state="2026-07-02T10:30:00+00:00", synced_at=now),
            HAEntity(entity_id="sensor.dead_thing", domain="sensor",
                     state="unavailable", synced_at=now),
            HAEntity(entity_id="sensor.door_battery", domain="sensor", state="12",
                     device_class="battery", synced_at=now),
            HAEntity(entity_id="switch.example_doorbell_status_light",
                     domain="switch", state="on", synced_at=now),
        ]
        db_session.add_all(rows)
        db_session.commit()

    def test_grouping(self, db_session):
        self._seed(db_session)
        result = json.loads(handle_home_status(db_session, {}))

        sections = result["sections"]
        assert [e["entity_id"] for e in sections["presence"]] == ["person.alex"]
        assert [e["entity_id"] for e in sections["media"]] == ["media_player.homepod"]
        assert [e["entity_id"] for e in sections["appliances"]] == [
            "sensor.utility_room_washing_machine_completion_time"
        ]
        # off valve still shows under water (state matters, presence of signal too)
        assert [e["entity_id"] for e in sections["water"]] == ["switch.example_sprinkler_1"]
        # only the light that is on
        on_ids = [e["entity_id"] for e in sections["lights_switches"]]
        assert "light.hall" in on_ids and "light.shed" not in on_ids
        # exclude_globs: doorbell config toggles stay out despite being "on"
        assert "switch.example_doorbell_status_light" not in on_ids

    def test_unavailable_goes_to_attention_not_sections(self, db_session):
        self._seed(db_session)
        result = json.loads(handle_home_status(db_session, {}))

        offline_ids = [e["entity_id"] for e in result["attention"]["offline"]]
        assert offline_ids == ["sensor.dead_thing"]
        for entities in result["sections"].values():
            assert "sensor.dead_thing" not in [e["entity_id"] for e in entities]

    def test_low_battery_flagged(self, db_session):
        self._seed(db_session)
        result = json.loads(handle_home_status(db_session, {}))
        low = [e["entity_id"] for e in result["attention"]["low_battery"]]
        assert low == ["sensor.door_battery"]

    def test_freshness_flags(self, db_session):
        self._seed(db_session)
        assert json.loads(handle_home_status(db_session, {}))["stale"] is False

        db_session.query(HAEntity).update(
            {"synced_at": datetime.now(timezone.utc) - timedelta(hours=1)}
        )
        db_session.commit()
        assert json.loads(handle_home_status(db_session, {}))["stale"] is True

    def test_empty_db_message(self, db_session):
        result = json.loads(handle_home_status(db_session, {}))
        assert result["stale"] is True
        assert "message" in result


@pytest.mark.db
class TestEntityTools:
    def _seed(self, db_session):
        now = datetime.now(timezone.utc)
        db_session.add_all([
            HAEntity(entity_id="light.kitchen_main", domain="light", state="on",
                     friendly_name="Kitchen Main", area="Kitchen", synced_at=now),
            HAEntity(entity_id="sensor.kitchen_temp", domain="sensor", state="20.1",
                     friendly_name="Kitchen Temperature", area="Kitchen", synced_at=now),
            HAEntity(entity_id="light.office", domain="light", state="off",
                     friendly_name="Office Light", area="Alex's Office", synced_at=now),
        ])
        db_session.commit()

    def test_entities_domain_filter(self, db_session):
        self._seed(db_session)
        result = json.loads(handle_entities(db_session, {"domain": "light"}))
        assert result["count"] == 2

    def test_entities_area_and_query_filters(self, db_session):
        self._seed(db_session)
        by_area = json.loads(handle_entities(db_session, {"area": "kitchen"}))
        assert {e["entity_id"] for e in by_area["entities"]} == {
            "light.kitchen_main", "sensor.kitchen_temp"
        }
        by_query = json.loads(handle_entities(db_session, {"query": "office"}))
        assert [e["entity_id"] for e in by_query["entities"]] == ["light.office"]

    def test_entity_with_history_and_missing(self, db_session):
        self._seed(db_session)
        db_session.add(HAStateChange(
            entity_id="light.office", old_state="on", new_state="off",
            changed_at=datetime.now(timezone.utc),
        ))
        db_session.commit()

        result = json.loads(handle_entity(db_session, {
            "entity_ids": ["light.office", "light.nonexistent"],
        }))
        assert result["not_found"] == ["light.nonexistent"]
        (entity,) = result["entities"]
        assert entity["entity_id"] == "light.office"
        assert len(entity["recent_changes"]) == 1


@pytest.mark.db
class TestHistory:
    def test_range_and_counts(self, db_session):
        now = datetime.now(timezone.utc)
        for days_ago, new_state in [(1, "run"), (2, "stop"), (3, "run"), (30, "run")]:
            db_session.add(HAStateChange(
                entity_id="sensor.washing_machine_job_state",
                old_state="x", new_state=new_state,
                changed_at=now - timedelta(days=days_ago),
            ))
        db_session.commit()

        result = json.loads(handle_history(db_session, {
            "entity_id": "sensor.washing_machine_job_state", "days": 7,
        }))
        assert result["transition_count"] == 3  # 30-days-ago row outside window
        assert result["counts_by_new_state"] == {"run": 2, "stop": 1}

    def test_requires_entity_id(self, db_session):
        result = json.loads(handle_history(db_session, {}))
        assert "error" in result
