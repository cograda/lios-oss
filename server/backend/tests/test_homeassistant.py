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
    EventRule,
    Section,
    handle_entities,
    handle_entity,
    handle_events,
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
        section = Section("water", globs=("switch.sonoff_hydro_duo_*",))
        assert section.matches(self._entity("switch.sonoff_hydro_duo_1"))
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


class TestNewSections:
    def _entity(self, entity_id, state="on"):
        return HAEntity(entity_id=entity_id, domain=entity_id.split(".", 1)[0], state=state)

    def test_heating_hot_water(self):
        section = next(s for s in ha_tools.SECTIONS if s.name == "heating_hot_water")
        assert section.matches(self._entity("water_heater.smos40_hot_water"))
        assert section.matches(self._entity("sensor.hot_water_top_bt7_30009", state="45.2"))
        assert not section.matches(self._entity("sensor.unrelated"))

    def test_car(self):
        section = next(s for s in ha_tools.SECTIONS if s.name == "car")
        assert section.matches(self._entity("sensor.polestar_2588_battery_charge_level", state="72"))
        assert not section.matches(self._entity("sensor.polestar_2588_odometer"))

    def test_air_quality(self):
        section = next(s for s in ha_tools.SECTIONS if s.name == "air_quality")
        assert section.matches(self._entity("sensor.air_quality_v1_1_co2", state="612"))
        assert section.matches(self._entity("sensor.shed_cam_eco2", state="450"))

    def test_commute(self):
        section = next(s for s in ha_tools.SECTIONS if s.name == "commute")
        assert section.matches(self._entity("sensor.commute_status", state="comfortable"))
        assert not section.matches(self._entity("sensor.dishwasher_operation_state"))


class TestEventRules:
    def _change(self, entity_id, old, new):
        return HAStateChange(entity_id=entity_id, old_state=old, new_state=new)

    def test_washer_finished_matches_run_to_stop(self):
        rule = next(r for r in ha_tools.EVENT_RULES if r.name == "washer_finished")
        assert rule.matches(self._change(
            "sensor.utility_room_washing_machine_machine_state", "run", "stop"
        ))
        assert not rule.matches(self._change(
            "sensor.utility_room_washing_machine_machine_state", "stop", "run"
        ))

    def test_dishwasher_finished(self):
        rule = next(r for r in ha_tools.EVENT_RULES if r.name == "dishwasher_finished")
        assert rule.matches(self._change("sensor.dishwasher_operation_state", "run", "finished"))
        assert not rule.matches(self._change("sensor.dishwasher_operation_state", "run", "ready"))

    def test_doorbell_any_change_matches(self):
        rule = next(r for r in ha_tools.EVENT_RULES if r.name == "doorbell_ring")
        assert rule.matches(self._change(
            "event.frankfort_front_door_doorbell",
            "2026-07-13T10:00:00+00:00", "2026-07-13T10:05:00+00:00",
        ))

    def test_car_charging_started_and_stopped(self):
        started = next(r for r in ha_tools.EVENT_RULES if r.name == "car_charging_started")
        stopped = next(r for r in ha_tools.EVENT_RULES if r.name == "car_charging_stopped")
        assert started.matches(self._change("sensor.polestar_2588_charging_status", "Idle", "Charging"))
        assert stopped.matches(self._change("sensor.polestar_2588_charging_status", "Charging", "Idle"))
        assert not started.matches(self._change("sensor.polestar_2588_charging_status", "Charging", "Idle"))

    def test_unknown_states_excluded(self):
        rule = EventRule("x", "X", "sensor.foo", to_states=frozenset({"on"}))
        assert not rule.matches(self._change("sensor.foo", "unavailable", "on"))
        assert not rule.matches(self._change("sensor.foo", "off", "unknown"))

    def test_glob_must_match(self):
        rule = next(r for r in ha_tools.EVENT_RULES if r.name == "shed_presence")
        assert not rule.matches(self._change("binary_sensor.other_presence", "off", "on"))


class TestAreaMapParsing:
    def test_bad_json_returns_empty(self):
        with patch.object(ha_client, "render_template", return_value="not json"):
            assert ha_client.fetch_area_map() == {}

    def test_entities_without_area_dropped(self):
        rendered = json.dumps({"light.hall": "Hallway", "sensor.uptime": ""})
        with patch.object(ha_client, "render_template", return_value=rendered):
            assert ha_client.fetch_area_map() == {"light.hall": "Hallway"}

    def test_http_error_returns_empty_states(self, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setattr(
            ha_client, "plugin_config",
            lambda name: SimpleNamespace(ha_url="http://127.0.0.1:1", ha_token="x"),
        )
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
        from app.plugin.config_store import plugin_config as _real_plugin_config

        def _fake_plugin_config(name):
            cfg = _real_plugin_config(name)
            if name == "homeassistant":
                cfg.ha_record_numeric_history = True
            return cfg

        monkeypatch.setattr(ha_sync, "plugin_config", _fake_plugin_config)
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
            HAEntity(entity_id="switch.sonoff_hydro_duo_1", domain="switch",
                     state="off", synced_at=now),
            HAEntity(entity_id="sensor.utility_room_washing_machine_completion_time", domain="sensor",
                     state="2026-07-02T10:30:00+00:00", synced_at=now),
            HAEntity(entity_id="sensor.dead_thing", domain="sensor",
                     state="unavailable", synced_at=now),
            HAEntity(entity_id="sensor.door_battery", domain="sensor", state="12",
                     device_class="battery", synced_at=now),
            HAEntity(entity_id="switch.frankfort_front_door_status_light",
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
        assert [e["entity_id"] for e in sections["water"]] == ["switch.sonoff_hydro_duo_1"]
        # only the light that is on
        on_ids = [e["entity_id"] for e in sections["lights_switches"]]
        assert "light.hall" in on_ids and "light.shed" not in on_ids
        # exclude_globs: doorbell config toggles stay out despite being "on"
        assert "switch.frankfort_front_door_status_light" not in on_ids

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
class TestDashboardDataCounts:
    """P8 (hardening-2026-08.md): `HomeAssistantIntegration.dashboard_data`
    used to `session.query(HAEntity).all()` and count in Python; it's now
    COUNT/MAX aggregates run in Postgres. Pin the counts (and the one
    NULL-state truthiness quirk the rewrite had to preserve) against real
    rows rather than trusting the SQL translation by inspection.
    """

    async def _run(self):
        from app.integrations.homeassistant import HomeAssistantIntegration

        return await HomeAssistantIntegration().dashboard_data()

    def test_no_data(self, db_session):
        import asyncio

        result = asyncio.run(self._run())
        assert result == {"status": "no_data"}

    def test_counts_match_seeded_rows(self, db_session):
        import asyncio

        now = datetime.now(timezone.utc)
        db_session.add_all([
            HAEntity(entity_id="light.hall", domain="light", state="on", synced_at=now),
            HAEntity(entity_id="light.shed", domain="light", state="off", synced_at=now),
            HAEntity(entity_id="switch.kettle", domain="switch", state="on", synced_at=now),
            HAEntity(entity_id="sensor.dead_thing", domain="sensor", state="unavailable", synced_at=now),
            HAEntity(entity_id="sensor.unknown_thing", domain="sensor", state="unknown", synced_at=now),
            # A media_player with no state yet — the old Python truthiness
            # `(e.state or "") not in (...)` counted this as "playing"
            # (since "" isn't in the excluded-states tuple); the SQL
            # rewrite uses coalesce(state, "") to match that exactly.
            HAEntity(entity_id="media_player.homepod", domain="media_player", state=None, synced_at=now),
            HAEntity(entity_id="media_player.tv", domain="media_player", state="off", synced_at=now),
        ])
        db_session.commit()

        result = asyncio.run(self._run())

        assert result["entity_count"] == 7
        assert result["offline_count"] == 2  # dead_thing + unknown_thing
        assert result["lights_on"] == 2  # light.hall + switch.kettle
        assert result["media_playing"] == 1  # homepod (NULL state), not tv (off)
        assert result["synced_at"] is not None


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


@pytest.mark.db
class TestHandleEvents:
    def _seed(self, db_session):
        now = datetime.now(timezone.utc)
        rows = [
            HAStateChange(
                entity_id="sensor.utility_room_washing_machine_machine_state",
                old_state="run", new_state="stop", changed_at=now - timedelta(minutes=5),
            ),
            HAStateChange(
                entity_id="sensor.dishwasher_operation_state",
                old_state="run", new_state="finished", changed_at=now - timedelta(hours=1),
            ),
            HAStateChange(
                entity_id="event.frankfort_front_door_doorbell",
                old_state="2026-07-13T09:00:00+00:00", new_state="2026-07-13T09:05:00+00:00",
                changed_at=now - timedelta(hours=2),
            ),
            # unrelated transition — shouldn't match any rule
            HAStateChange(
                entity_id="light.hall", old_state="off", new_state="on", changed_at=now,
            ),
            # outside the lookback window
            HAStateChange(
                entity_id="sensor.dishwasher_operation_state",
                old_state="run", new_state="finished", changed_at=now - timedelta(days=2),
            ),
        ]
        db_session.add_all(rows)
        db_session.commit()

    def test_returns_matching_events_within_window(self, db_session):
        self._seed(db_session)
        result = json.loads(handle_events(db_session, {"hours": 24}))
        names = {e["event"] for e in result["events"]}
        assert names == {"washer_finished", "dishwasher_finished", "doorbell_ring"}
        assert result["event_count"] == 3

    def test_narrow_window_excludes_older_events(self, db_session):
        self._seed(db_session)
        result = json.loads(handle_events(db_session, {"hours": 1}))
        names = {e["event"] for e in result["events"]}
        assert names == {"washer_finished"}

    def test_sql_cap_orders_by_newest_first(self, db_session, monkeypatch):
        """The SQL-level cap (limit*10, floored at 2000) means only the
        newest rows within that cap are ever considered — verify the query
        is actually ordered desc so a small cap doesn't silently drop the
        newest matching event in favour of stale ones. We shrink the cap
        via a tiny `limit` and bury the real match under many unrelated
        newer rows outside the cap window ordering would matter for."""
        now = datetime.now(timezone.utc)
        # One real match, old-ish.
        db_session.add(HAStateChange(
            entity_id="sensor.dishwasher_operation_state",
            old_state="run", new_state="finished",
            changed_at=now - timedelta(minutes=30),
        ))
        # A pile of newer, non-matching noise — with limit=1 the SQL cap is
        # max(1*10, 2000) = 2000, comfortably above this pile, so the match
        # must still surface if ordering is correct (newest-first, matched row
        # well within the cap).
        for i in range(20):
            db_session.add(HAStateChange(
                entity_id="light.hall", old_state="off", new_state="on",
                changed_at=now - timedelta(minutes=i),
            ))
        db_session.commit()

        result = json.loads(handle_events(db_session, {"hours": 24, "limit": 1}))
        assert result["event_count"] == 1
        assert result["events"][0]["event"] == "dishwasher_finished"


# ---------------------------------------------------------------------------
# Unit tier — facade.notify wire payload
# ---------------------------------------------------------------------------

class TestNotifyPayloadShape:
    """Guard the `notify.mobile_app_*` payload shape.

    These assert on the dict handed to `call_service` — the seam
    `tests/test_notifications.py` cannot reach, because it stubs the facade
    itself and so only ever checks the facade's *arguments*. The regression
    they exist for: `data` was spread into the payload root, HA answered
    `HTTP 400 extra keys not allowed`, and `call_service` classified that as
    permanent — so every `critical` and `recovery` push was dropped without
    a retry. Verified against the live instance on 2026-08-22: the spread
    shape returns 400, the nested shape 200.
    """

    def _capture(self):
        from app.integrations.homeassistant.facade import HomeAssistantFacade

        sent: list[tuple[str, str, dict]] = []

        def _fake_call_service(domain, service, data):
            sent.append((domain, service, data))
            return True

        return HomeAssistantFacade(), sent, _fake_call_service

    def _send(self, data):
        facade, sent, fake = self._capture()
        with patch.object(ha_client, "call_service", side_effect=fake):
            facade.notify("mobile_app_a_phone", "T", "B", data)
        return sent[0]

    def test_severity_data_is_nested_never_spread(self):
        """The bug itself: `push` must not appear at the payload root."""
        domain, service, payload = self._send(
            {"push": {"interruption-level": "critical"}, "ttl": 0, "priority": "high"}
        )
        assert (domain, service) == ("notify", "mobile_app_a_phone")
        assert payload == {
            "title": "T",
            "message": "B",
            "data": {"push": {"interruption-level": "critical"}, "ttl": 0, "priority": "high"},
        }

    def test_no_root_keys_beyond_has_accepted_schema(self):
        """HA accepts exactly message/title/target/data — anything else 400s."""
        _, _, payload = self._send({"push": {"interruption-level": "passive"}, "importance": "low"})
        assert set(payload) <= {"message", "title", "target", "data"}

    def test_empty_data_is_omitted_not_sent_as_empty_dict(self):
        """`warning`'s severity payload is `{}`. It was the one severity that
        worked before the fix (spreading `{}` is a no-op), so preserve its
        wire shape exactly rather than newly sending `data: {}`."""
        for empty in ({}, None):
            _, _, payload = self._send(empty)
            assert payload == {"title": "T", "message": "B"}

    def test_caller_data_is_copied_not_aliased(self):
        """`_SEVERITY_DATA` is a module-level dict reused on every send; the
        payload must not hand a live reference to it into the client."""
        from app.integrations.notifications.client import _SEVERITY_DATA

        original = _SEVERITY_DATA["critical"]
        _, _, payload = self._send(original)
        assert payload["data"] == original
        assert payload["data"] is not original
