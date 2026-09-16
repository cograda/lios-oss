"""Weather's outdoor-sensor temperature override (issue #155).

`weather_current`'s "current temperature" used to be Open-Meteo's number
unconditionally. It now prefers a local HA sensor (the gate-sensor board's
DS18B20, by default) when that entity's state is a fresh number, and falls
back to Open-Meteo otherwise — unknown/unavailable state, no entity, stale
timestamp, or the capability call raising for any reason. The forecast tool
is untouched; only `handle_current`'s single `temperature` field changes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.integrations.weather.models import WeatherCurrent, WeatherForecast
from app.integrations.weather import tools as weather_tools

UTC = timezone.utc


def _current_row(temp: float = 10.0) -> WeatherCurrent:
    return WeatherCurrent(
        id=1,
        temp=temp,
        feels_like=temp - 1,
        humidity=80,
        wind_speed=12.0,
        wind_direction=270,
        weather_code=3,
        cloud_cover=90,
        precipitation=0.0,
        is_day=True,
        fetched_at=datetime.now(UTC),
    )


def _session_returning(current, forecast=None):
    """A session double whose `.query(Model)` dispatches on the model class,
    the way `mock_session` can't (it shares one query mock for every model).
    """
    session = MagicMock()

    def _query(model):
        q = MagicMock()
        if model is WeatherCurrent:
            q.first.return_value = current
        elif model is WeatherForecast:
            q.filter_by.return_value = q
            q.first.return_value = forecast
        else:
            q.first.return_value = None
        return q

    session.query.side_effect = _query
    return session


def _cfg(entity_id: str = "sensor.gate_sensor_temperature"):
    return SimpleNamespace(weather_outdoor_sensor_entity_id=entity_id)


@pytest.mark.unit
class TestOutdoorSensorOverride:
    def test_fresh_reading_overrides_open_meteo_and_labels_the_source(self, monkeypatch):
        session = _session_returning(_current_row(temp=8.0))
        monkeypatch.setattr(weather_tools, "plugin_config", lambda _n: _cfg())
        ha = MagicMock()
        ha.entity_state.return_value = {
            "entity_id": "sensor.gate_sensor_temperature",
            "state": "14.2",
            "last_changed": datetime.now(UTC) - timedelta(minutes=5),
            "synced_at": datetime.now(UTC),
        }
        monkeypatch.setattr(weather_tools, "get_capability", lambda _cap: ha)

        result = json.loads(weather_tools.handle_current(session, {}))

        assert result["temperature"] == 14.2
        assert result["temperature_source"] == "gate_sensor"
        assert result["temperature_source_label"] == "(gate sensor)"
        assert result["temperature_source_entity_id"] == "sensor.gate_sensor_temperature"
        # feels_like is never overridden — no local equivalent exists.
        assert result["feels_like"] == 7.0

    def test_stale_reading_falls_back_to_open_meteo(self, monkeypatch):
        session = _session_returning(_current_row(temp=8.0))
        monkeypatch.setattr(weather_tools, "plugin_config", lambda _n: _cfg())
        ha = MagicMock()
        ha.entity_state.return_value = {
            "entity_id": "sensor.gate_sensor_temperature",
            "state": "14.2",
            "last_changed": datetime.now(UTC) - timedelta(hours=2),
            "synced_at": datetime.now(UTC) - timedelta(hours=2),
        }
        monkeypatch.setattr(weather_tools, "get_capability", lambda _cap: ha)

        result = json.loads(weather_tools.handle_current(session, {}))

        assert result["temperature"] == 8.0
        assert result["temperature_source"] == "open_meteo"
        assert result["temperature_source_label"] == "(Open-Meteo)"
        assert "temperature_source_entity_id" not in result

    @pytest.mark.parametrize("state", ["unknown", "unavailable", None])
    def test_unknown_or_unavailable_state_falls_back(self, monkeypatch, state):
        session = _session_returning(_current_row(temp=8.0))
        monkeypatch.setattr(weather_tools, "plugin_config", lambda _n: _cfg())
        ha = MagicMock()
        ha.entity_state.return_value = {
            "entity_id": "sensor.gate_sensor_temperature",
            "state": state,
            "last_changed": datetime.now(UTC),
            "synced_at": datetime.now(UTC),
        }
        monkeypatch.setattr(weather_tools, "get_capability", lambda _cap: ha)

        result = json.loads(weather_tools.handle_current(session, {}))

        assert result["temperature"] == 8.0
        assert result["temperature_source"] == "open_meteo"

    def test_entity_missing_from_ha_falls_back(self, monkeypatch):
        session = _session_returning(_current_row(temp=8.0))
        monkeypatch.setattr(weather_tools, "plugin_config", lambda _n: _cfg())
        ha = MagicMock()
        ha.entity_state.return_value = None
        monkeypatch.setattr(weather_tools, "get_capability", lambda _cap: ha)

        result = json.loads(weather_tools.handle_current(session, {}))

        assert result["temperature"] == 8.0
        assert result["temperature_source"] == "open_meteo"

    def test_blank_config_disables_override_without_calling_ha(self, monkeypatch):
        session = _session_returning(_current_row(temp=8.0))
        monkeypatch.setattr(weather_tools, "plugin_config", lambda _n: _cfg(entity_id=""))
        ha = MagicMock()
        monkeypatch.setattr(weather_tools, "get_capability", lambda _cap: ha)

        result = json.loads(weather_tools.handle_current(session, {}))

        assert result["temperature"] == 8.0
        assert result["temperature_source"] == "open_meteo"
        ha.entity_state.assert_not_called()

    def test_capability_failure_never_costs_the_caller_the_weather(self, monkeypatch):
        """The override is best-effort — an HA/capability-boundary error must
        never make weather_current itself fail (module docstring precedent:
        `brief.py`'s "a dead Gmail token must not cost you the weather")."""
        session = _session_returning(_current_row(temp=8.0))
        monkeypatch.setattr(weather_tools, "plugin_config", lambda _n: _cfg())

        def _boom(_cap):
            raise RuntimeError("capability not found")

        monkeypatch.setattr(weather_tools, "get_capability", _boom)

        result = json.loads(weather_tools.handle_current(session, {}))

        assert result["temperature"] == 8.0
        assert result["temperature_source"] == "open_meteo"

    def test_forecast_is_never_touched_by_the_override(self, monkeypatch):
        """The forecast tool doesn't call the override at all — no HA capability
        lookup should happen when only weather_forecast is invoked."""
        forecasts = [
            WeatherForecast(
                id=1, date=datetime.now(UTC).date(), temp_max=12.0, temp_min=4.0,
                weather_code=2, precipitation_sum=0.0, wind_speed_max=20.0,
                sunrise="08:00", sunset="18:00",
            )
        ]
        session = MagicMock()
        q = MagicMock()
        q.filter.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        q.all.return_value = forecasts
        session.query.return_value = q

        called = MagicMock()
        monkeypatch.setattr(weather_tools, "get_capability", called)

        result = json.loads(weather_tools.handle_forecast(session, {"days": 1}))

        assert len(result) == 1
        assert result[0]["temp_max"] == 12.0
        called.assert_not_called()
