"""Tests for `app.integrations.system.brief_render` — the deterministic
markdown renderers behind `system_daily_brief(render=true)`.

One happy-path test and one empty-case test per section (matching the build
spec), plus a handful of "missing key" tests proving absence renders an
explicit "not measured" line rather than a blank that looks like zero — the
same rule `daily-note.md.j2` states for the model's own reading of the
payload. Each "not measured" assertion is mutation-checked in its own test
body: the comment next to the assertion names the line in `brief_render.py`
that, if deleted, would make the test fail (verified by hand before this
file was written — deleting the `if x is None: ... NOT_MEASURED` branch for
each case turns the assertion into a KeyError or an empty string).
"""

from __future__ import annotations

import pytest

from app.integrations.system import brief_render as br

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------


def test_alerts_all_ok():
    payload = {
        "alerts": {
            "status": "all_ok",
            "alerts": [],
            "recent_runs": {"counts": {"ok": 12}},
        }
    }
    out = br.render_alerts(payload)
    assert out.startswith("## System Alerts")
    assert "all_ok" in out
    assert "All systems OK." in out
    assert "12 ok" in out


def test_alerts_with_issues():
    payload = {
        "alerts": {
            "status": "degraded",
            "alerts": [{"integration": "lastfm", "issues": ["data stale (no records)"]}],
            "recent_runs": {"counts": {"ok": 3, "error": 1}},
        }
    }
    out = br.render_alerts(payload)
    assert "⚠️ **lastfm**: data stale (no records)" in out


def test_alerts_missing_key_renders_not_measured():
    # No "alerts" key at all — deleting the `if not isinstance(alerts, dict):
    # return _section(...)` branch in render_alerts would make this raise
    # instead of returning the not-measured line.
    out = br.render_alerts({})
    assert br.NOT_MEASURED in out


def test_alerts_error_is_visible():
    out = br.render_alerts({"alerts": {"error": "boom"}})
    assert "⚠️" in out and "boom" in out


# ---------------------------------------------------------------------------
# pulse
# ---------------------------------------------------------------------------


def _pulse_payload():
    return {
        "health_sleep": {
            "total_hours": 7.5,
            "stage_breakdown": {"asleepDeep": 1.2, "asleepREM": 1.5},
            "sessions": [{"start": "2026-09-06T23:10:00+00:00", "end": "2026-09-07T06:40:00+00:00"}],
        },
        "health_trends": {
            "daily": {"2026-09-07": {"resting_hr_bpm": 52, "hrv_ms": 61, "steps": 4200}},
            "period_averages": {"steps": 6000},
        },
        "health_workouts": {"workouts": [{"type": "run", "duration_min": 32, "avg_hr_bpm": 141}]},
    }


def test_pulse_happy_path():
    out = br.render_pulse(_pulse_payload())
    assert out.startswith("## Pulse")
    assert "7h30m" in out
    assert "HRV 61ms" in out
    assert "4200 steps" in out or "4200" in out
    assert "run" in out


def test_pulse_absent_entirely_is_omitted():
    # No health_* keys at all — gated off, no data for this user.
    assert br.render_pulse({}) == ""


def test_pulse_sleep_key_missing_renders_not_measured():
    # health_sleep absent but the user has other health data (so the section
    # as a whole still renders) — deleting the `if sleep is None: lines.append(
    # not-measured)` branch would make this KeyError instead.
    payload = _pulse_payload()
    del payload["health_sleep"]
    out = br.render_pulse(payload)
    assert br.NOT_MEASURED in out


# ---------------------------------------------------------------------------
# pulse — workouts: Strava merge (issue #195b)
# ---------------------------------------------------------------------------


def _no_sleep_or_trends(**extra):
    """A minimal Pulse payload isolating the workouts line — no sleep/trends
    keys, so only the 💪 line's text needs asserting on."""
    return {"health_sleep": None, "health_trends": None, **extra}


def test_pulse_workouts_health_only():
    """No `strava_activities` key at all (e.g. filtered by `sections`, or a
    pre-#195 caller) — behaves exactly as before the merge."""
    payload = _no_sleep_or_trends(
        health_workouts={
            "workouts": [
                {"type": "run", "start": "2026-09-09T07:00:00+00:00", "duration_min": 32, "avg_hr_bpm": 141}
            ]
        },
    )
    out = br.render_pulse(payload)
    assert "💪 run — 32 min, avg HR 141" in out
    assert "Strava" not in out


def test_pulse_workouts_strava_only():
    """Apple Health ran and found nothing this week; Strava (independently)
    has an activity. Must not read as a blanket 'none' — the whole point of
    #195, where Strava's Evening Walk never appeared anywhere in the brief."""
    payload = _no_sleep_or_trends(
        health_workouts={"workouts": []},
        strava_activities={
            "connected": True,
            "count": 1,
            "activities": [
                {
                    "type": "Walk",
                    "name": "Evening Walk",
                    "start": "2026-09-09T19:03:00+00:00",
                    "duration_min": 22.8,
                    "distance_km": 1.83,
                }
            ],
        },
    )
    out = br.render_pulse(payload)
    assert "none in the last week" not in out
    assert "Evening Walk" in out
    assert "(Strava)" in out


def test_pulse_workouts_both_with_overlap_dedupes():
    """The same session reached both Apple Health and Strava (e.g. a watch
    that syncs to both) — must render as ONE line, not two, and note both
    sources rather than silently dropping the Strava side."""
    payload = _no_sleep_or_trends(
        health_workouts={
            "workouts": [
                {
                    "type": "strength_training",
                    "start": "2026-09-03T09:02:00+00:00",
                    "duration_min": 45,
                    "avg_hr_bpm": 128,
                }
            ]
        },
        strava_activities={
            "connected": True,
            "count": 1,
            "activities": [
                {
                    "type": "WeightTraining",
                    "name": "Morning Workout",
                    "start": "2026-09-03T09:05:00+00:00",  # within 10 min
                    "duration_min": 43,  # within tolerance of 45
                }
            ],
        },
    )
    out = br.render_pulse(payload)
    assert out.count("💪") == 1, f"expected exactly one workout line, got: {out!r}"
    assert "strength_training" in out
    assert "also on Strava" in out
    assert "Morning Workout" not in out  # the Strava-only line must not also render


def test_pulse_workouts_strava_unconfigured_says_so():
    """Strava ran but the caller has no OAuth token — `{"connected": False}`.
    Must be distinguishable from a real empty week, not folded into the same
    'none' text."""
    payload = _no_sleep_or_trends(
        health_workouts={"workouts": []},
        strava_activities={"connected": False},
    )
    out = br.render_pulse(payload)
    assert "not connected" in out.lower()
    assert "none in the last week" not in out


# ---------------------------------------------------------------------------
# coffee
# ---------------------------------------------------------------------------


def test_coffee_happy_path():
    payload = {
        "coffee_current": {
            "count": 1,
            "coffees": [{"name": "Colombia Viani", "roaster": "Cloud Picker", "process": "washed"}],
        }
    }
    out = br.render_coffee(payload)
    assert out.startswith("## Coffee")
    assert "Colombia Viani" in out
    assert "Cloud Picker" in out


def test_coffee_empty_is_omitted():
    assert br.render_coffee({"coffee_current": {"count": 0, "coffees": []}}) == ""
    assert br.render_coffee({}) == ""


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


def test_transport_happy_path():
    payload = {
        "_meta": {"is_weekend": False},
        "rail": {
            "station": "GSTNS",
            "departures": [
                {"scheduled_departure": "10:04", "expected_departure": "10:04", "destination": "Dublin"},
                {"scheduled_departure": "10:34", "expected_departure": "10:38", "destination": "Dublin"},
            ],
        },
    }
    out = br.render_transport(payload)
    assert out.startswith("## Transport")
    assert "10:04 → Dublin" in out
    assert "10:34 → Dublin (exp 10:38)" in out


def test_transport_empty_on_weekend():
    payload = {"_meta": {"is_weekend": True}, "rail": {"departures": [{"scheduled_departure": "x", "destination": "y"}]}}
    assert br.render_transport(payload) == ""


def test_transport_absent_on_weekday_is_omitted():
    assert br.render_transport({"_meta": {"is_weekend": False}}) == ""


# ---------------------------------------------------------------------------
# consumables (House)
# ---------------------------------------------------------------------------


def test_consumables_happy_path():
    payload = {
        "home": {
            "sections": {
                "appliances": [
                    {"entity_id": "binary_sensor.dishwasher_salt_nearly_empty", "state": "on", "friendly_name": "Dishwasher Salt"},
                    {"entity_id": "sensor.dishwasher_operation_state", "state": "Run", "friendly_name": "Dishwasher"},
                ]
            }
        }
    }
    out = br.render_consumables(payload)
    assert out.startswith("## House")
    assert "Dishwasher Salt" in out
    assert "#quick" in out


def test_consumables_empty_when_none_on():
    payload = {"home": {"sections": {"appliances": [{"entity_id": "x_nearly_empty", "state": "off"}]}}}
    assert br.render_consumables(payload) == ""


def test_consumables_absent_when_no_home_key():
    assert br.render_consumables({}) == ""


# ---------------------------------------------------------------------------
# snags
# ---------------------------------------------------------------------------


def test_snags_happy_path():
    payload = {
        "snags_created": 1,
        "snags": [{"uid": "SNAG-0163", "room": "Outdoor", "description": "Wrong thermostats", "trade": "electrician"}],
        "sheet_url": "https://example.invalid/sheet",
    }
    out = br.render_snags(payload)
    assert out.startswith("## Snags")
    assert "SNAG-0163 · Outdoor · Wrong thermostats (electrician)" in out
    assert "📋 Sheet: https://example.invalid/sheet" in out


def test_snags_empty_when_none_created():
    assert br.render_snags({}) == ""
    assert br.render_snags({"snags_created": 0}) == ""


# ---------------------------------------------------------------------------
# calendar (Today)
# ---------------------------------------------------------------------------


def test_calendar_happy_path():
    events = [
        {
            "summary": "Dentist", "all_day": False,
            "start": "2026-09-07T09:00:00+00:00", "end": "2026-09-07T09:45:00+00:00",
        },
        {"summary": "Bank Holiday", "all_day": True},
    ]
    out = br.render_calendar({"calendar": events})
    assert out.startswith("## Today")
    assert "ALL DAY  Bank Holiday" in out
    assert "09:00–09:45  Dentist" in out


def test_calendar_busy_event_renders_time_range_and_calendar_label():
    # Free/busy-only work events carry summary "(busy)" and the owning
    # calendar in `calendar`, not a real title. Deleting the
    # `summary == "(busy)"` branch in render_calendar would make this fall
    # through to the plain-summary branch and assert a bare "(busy)" with no
    # time range and no calendar label.
    events = [
        {
            "summary": "(busy)", "calendar": "work@example.com", "all_day": False,
            "start": "2026-09-07T10:00:00+00:00", "end": "2026-09-07T10:30:00+00:00",
        },
    ]
    out = br.render_calendar({"calendar": events})
    assert "- 10:00–10:30  busy (work@example.com)" in out
    assert "- (busy)" not in out


def test_calendar_empty_still_renders_heading():
    out = br.render_calendar({"calendar": []})
    assert out.startswith("## Today")
    assert "Nothing on the calendar" in out


def test_calendar_missing_key_renders_not_measured():
    # No "calendar" key — deleting the `if events is None: return _section(...
    # NOT_MEASURED)` branch would raise on `for e in events` below instead.
    out = br.render_calendar({})
    assert br.NOT_MEASURED in out


# ---------------------------------------------------------------------------
# listening
# ---------------------------------------------------------------------------


def test_listening_happy_path():
    payload = {
        "lastfm_recent": [{"track": "A"}, {"track": "B"}],
        "lastfm_stats": {
            "total_scrobbles": 42,
            "top_artists": [{"artist": "Radiohead", "play_count": 5}],
            "top_genres": [{"genre": "rock", "play_count": 10}],
        },
    }
    out = br.render_listening(payload)
    assert out.startswith("## Listening")
    assert "42 scrobbles" in out
    assert "Radiohead (5)" in out
    assert "rock" in out


def test_listening_absent_when_gated_off():
    assert br.render_listening({}) == ""


# ---------------------------------------------------------------------------
# freshness
# ---------------------------------------------------------------------------


def test_freshness_happy_path():
    payload = {
        "alerts": {
            "status": "degraded",
            "alerts": [{"integration": "lastfm", "issues": ["data stale (no records in table)"]}],
            "data_freshness": [
                {"integration": "whatsapp", "latest": "2026-09-07T08:12:00+00:00", "age": "47m", "threshold": "6h"},
                {"integration": "lastfm", "latest": None, "age": None, "threshold": "7d"},
            ],
            "unmeasured": ["finance", "obsidian"],
        }
    }
    out = br.render_freshness(payload)
    assert out.startswith("## Data freshness")
    assert "| whatsapp |" in out
    assert "| lastfm |" in out
    assert "never | 7d | ⚠️" in out
    assert "_Unmeasured (no probe): finance, obsidian._" in out
    # The stale row (lastfm — no records) sorts before the healthy one.
    assert out.index("lastfm") < out.index("whatsapp")


def test_freshness_empty_rows():
    out = br.render_freshness({"alerts": {"status": "all_ok", "data_freshness": []}})
    assert "_No freshness probes reported._" in out


def test_freshness_missing_alerts_key_renders_not_measured():
    # No "alerts" key at all — deleting the `if not isinstance(alerts, dict):
    # return _section(..., NOT_MEASURED)` branch would raise on
    # `alerts.get(...)` below instead.
    out = br.render_freshness({})
    assert br.NOT_MEASURED in out


# ---------------------------------------------------------------------------
# render_all
# ---------------------------------------------------------------------------


def test_render_all_returns_every_requested_section():
    payload = {"_meta": {"is_weekend": True}}
    out = br.render_all(payload, ("alerts", "transport"))
    assert set(out) == {"alerts", "transport"}
    assert out["transport"] == ""  # weekend
    assert br.NOT_MEASURED in out["alerts"]
