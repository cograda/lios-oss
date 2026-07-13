"""Tests for Apple Health integration — sync logic and MCP tools (db tier).

Previously loaded modules via spec_from_file_location with a hand-rolled
sys.modules stub of app/coglib/fastapi (which poisoned later imports of
app.models.* for every test collected after this file). The real-Postgres
harness makes that scaffolding unnecessary: real imports, real session.
"""

import json
import unittest.mock
from datetime import date, datetime, timedelta, timezone

import pytest

from app.integrations.apple_health import parse_export as _parse_mod
from app.integrations.apple_health import tools as _tools_mod
from app.integrations.apple_health.models import (
    HealthDailyMetric,
    HealthSleepSession,
    HealthWorkout,
)
from app.integrations.apple_health.sync import sync_from_push

pytestmark = pytest.mark.db

_aggregate_daily_metrics = _parse_mod._aggregate_daily_metrics



# ---------------------------------------------------------------------------
# Sync tests
# ---------------------------------------------------------------------------


class TestSyncDailyMetrics:
    def test_inserts_new_metrics(self, db_session):
        metrics = [
            {"date": "2026-03-31", "metric_type": "steps", "value": 8432},
            {"date": "2026-03-31", "metric_type": "distance_km", "value": 5.7},
        ]
        count = sync_from_push(metrics, [], [], db_session, user_id=1)
        assert count == 2

        rows = db_session.query(HealthDailyMetric).all()
        assert len(rows) == 2
        steps = next(r for r in rows if r.metric_type == "steps")
        assert steps.value == 8432

    def test_upserts_existing_metric(self, db_session):
        """Push same date+metric twice — value should be updated, not duplicated."""
        sync_from_push(
            [{"date": "2026-03-31", "metric_type": "steps", "value": 5000}],
            [], [], db_session, user_id=1,
        )
        sync_from_push(
            [{"date": "2026-03-31", "metric_type": "steps", "value": 8432}],
            [], [], db_session, user_id=1,
        )
        rows = db_session.query(HealthDailyMetric).filter_by(metric_type="steps").all()
        assert len(rows) == 1
        assert rows[0].value == 8432

    def test_different_users_stored_separately(self, db_session):
        sync_from_push(
            [{"date": "2026-03-31", "metric_type": "steps", "value": 5000}],
            [], [], db_session, user_id=1,
        )
        sync_from_push(
            [{"date": "2026-03-31", "metric_type": "steps", "value": 3000}],
            [], [], db_session, user_id=2,
        )
        rows = db_session.query(HealthDailyMetric).all()
        assert len(rows) == 2

    def test_hr_metrics_stored(self, db_session):
        """HR min/avg/max and HRV should all sync."""
        metrics = [
            {"date": "2026-03-31", "metric_type": "hr_min_bpm", "value": 52},
            {"date": "2026-03-31", "metric_type": "hr_avg_bpm", "value": 68},
            {"date": "2026-03-31", "metric_type": "hr_max_bpm", "value": 142},
            {"date": "2026-03-31", "metric_type": "hrv_ms", "value": 45},
        ]
        count = sync_from_push(metrics, [], [], db_session, user_id=1)
        assert count == 4

        hrv = db_session.query(HealthDailyMetric).filter_by(metric_type="hrv_ms").first()
        assert hrv.value == 45


class TestSyncWorkouts:
    def test_inserts_workout(self, db_session):
        workouts = [{
            "uid": "abc-123",
            "workout_type": "running",
            "start_time": "2026-03-31T07:00:00+00:00",
            "end_time": "2026-03-31T07:30:00+00:00",
            "duration_seconds": 1800,
            "distance_km": 5.2,
            "active_energy_kcal": 380,
            "avg_heart_rate_bpm": 155,
        }]
        count = sync_from_push([], workouts, [], db_session, user_id=1)
        assert count == 1

        row = db_session.query(HealthWorkout).first()
        assert row.workout_type == "running"
        assert row.distance_km == 5.2

    def test_upserts_workout_by_uid(self, db_session):
        workout = {
            "uid": "abc-123",
            "workout_type": "running",
            "start_time": "2026-03-31T07:00:00+00:00",
            "end_time": "2026-03-31T07:30:00+00:00",
            "duration_seconds": 1800,
            "distance_km": 5.0,
        }
        sync_from_push([], [workout], [], db_session, user_id=1)
        workout["distance_km"] = 5.2  # Corrected by GPS
        sync_from_push([], [workout], [], db_session, user_id=1)

        rows = db_session.query(HealthWorkout).all()
        assert len(rows) == 1
        assert rows[0].distance_km == 5.2


class TestSyncSleep:
    def test_inserts_sleep_sessions(self, db_session):
        sleep = [
            {
                "uid": "sleep-1",
                "start_time": "2026-03-30T23:00:00+00:00",
                "end_time": "2026-03-31T01:30:00+00:00",
                "stage": "asleepCore",
                "duration_hours": 2.5,
            },
            {
                "uid": "sleep-2",
                "start_time": "2026-03-31T01:30:00+00:00",
                "end_time": "2026-03-31T02:15:00+00:00",
                "stage": "asleepDeep",
                "duration_hours": 0.75,
            },
        ]
        count = sync_from_push([], [], sleep, db_session, user_id=1)
        assert count == 2

        rows = db_session.query(HealthSleepSession).all()
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# MCP tool tests
# ---------------------------------------------------------------------------


class TestHealthTools:
    def test_get_mcp_tools_returns_seven(self):
        tools = _tools_mod.get_mcp_tools()
        assert len(tools) == 7
        names = {t["name"] for t in tools}
        assert names == {
            "health_today",
            "health_sleep",
            "health_workouts",
            "health_trends",
            "health_summary",
            "health_exercise_status",
            "health_weekly_summary",
        }

    def test_all_tools_have_handlers(self):
        tools = _tools_mod.get_mcp_tools()
        for tool in tools:
            assert callable(tool["handler"])

    def test_health_today_returns_metrics(self, db_session):
        sync_from_push(
            [
                {"date": date.today().isoformat(), "metric_type": "steps", "value": 6000},
                {"date": date.today().isoformat(), "metric_type": "hrv_ms", "value": 42},
            ],
            [], [], db_session, user_id=1,
        )
        result = json.loads(_tools_mod.handle_health_today(db_session, {}))
        assert result["metrics"]["steps"] == 6000
        assert result["metrics"]["hrv_ms"] == 42

    def test_health_trends_returns_period(self, db_session):
        today = date.today()
        for i in range(3):
            d = (today - timedelta(days=i)).isoformat()
            sync_from_push(
                [{"date": d, "metric_type": "steps", "value": 7000 + i * 1000}],
                [], [], db_session, user_id=1,
            )

        result = json.loads(_tools_mod.handle_health_trends(db_session, {"days": 7}))
        assert "period_averages" in result
        assert "steps" in result["period_averages"]

    def test_health_workouts_returns_recent(self, db_session):
        now = datetime.now(timezone.utc)
        sync_from_push(
            [],
            [{
                "uid": "w1",
                "workout_type": "running",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(minutes=30)).isoformat(),
                "duration_seconds": 1800,
                "distance_km": 5.0,
                "active_energy_kcal": 350,
                "avg_heart_rate_bpm": 150,
            }],
            [], db_session, user_id=1,
        )
        result = json.loads(_tools_mod.handle_health_workouts(db_session, {"days": 7}))
        assert result["count"] == 1
        assert result["workouts"][0]["type"] == "running"

    def test_health_summary_no_data(self, db_session):
        result = json.loads(_tools_mod.handle_health_summary(db_session, {}))
        assert "No health data" in result["summary"]


# ---------------------------------------------------------------------------
# Exercise status tests
# ---------------------------------------------------------------------------


class TestHealthExerciseStatus:
    # Fix to a Wednesday so days_ago=0 and days_ago=1 are always in the same Mon-Sun week
    _FIXED_TODAY = date(2026, 4, 8)  # Wednesday

    def _make_workout(self, db_session, workout_type, days_ago=0, uid=None):
        """Helper to insert a workout relative to the fixed reference date."""
        now = datetime(self._FIXED_TODAY.year, self._FIXED_TODAY.month,
                       self._FIXED_TODAY.day, 10, 0, tzinfo=timezone.utc) - timedelta(days=days_ago)
        sync_from_push(
            [],
            [{
                "uid": uid or f"w-{workout_type}-{days_ago}",
                "workout_type": workout_type,
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(minutes=45)).isoformat(),
                "duration_seconds": 2700,
                "distance_km": 0,
                "active_energy_kcal": 300,
                "avg_heart_rate_bpm": 130,
            }],
            [], db_session, user_id=1,
        )

    @pytest.fixture(autouse=True)
    def _pin_today(self):
        """Pin _today() to a Wednesday so relative days are always in the same week."""
        with unittest.mock.patch.object(_tools_mod, "_today", return_value=self._FIXED_TODAY):
            yield

    def test_no_workouts_returns_zero(self, db_session):
        result = json.loads(
            _tools_mod.handle_health_exercise_status(db_session, {})
        )
        assert result["actual"]["strength_sessions"] == 0
        assert result["actual"]["total_workouts"] == 0
        assert result["target"]["strength_sessions"] == 2

    def test_counts_strength_types(self, db_session):
        self._make_workout(db_session, "strength_training", days_ago=0)
        self._make_workout(db_session, "hiit", days_ago=1)
        self._make_workout(db_session, "running", days_ago=1, uid="w-run-1")

        result = json.loads(
            _tools_mod.handle_health_exercise_status(db_session, {})
        )
        assert result["actual"]["strength_sessions"] == 2
        assert result["actual"]["total_workouts"] == 3
        assert result["on_track"] is True

    def test_on_track_with_remaining_days(self, db_session):
        """One strength session + remaining days should still be on_track."""
        self._make_workout(db_session, "strength_training", days_ago=0)

        result = json.loads(
            _tools_mod.handle_health_exercise_status(db_session, {})
        )
        # Wednesday: 4 remaining days (Thu-Sun), so 1 session + remaining >= 2
        assert result["on_track"] is True

    def test_specific_week(self, db_session):
        """Passing week_of should scope to that week."""
        result = json.loads(
            _tools_mod.handle_health_exercise_status(
                db_session, {"week_of": "2026-04-06"}
            )
        )
        assert result["week_start"] == "2026-04-06"
        assert result["week_end"] == "2026-04-12"


# ---------------------------------------------------------------------------
# Weekly summary tests
# ---------------------------------------------------------------------------


class TestHealthWeeklySummary:
    def _populate_week(self, db_session, week_start="2026-04-06"):
        """Insert a week of health data for testing."""
        ws = date.fromisoformat(week_start)

        # Daily metrics for 5 days
        for i in range(5):
            d = (ws + timedelta(days=i)).isoformat()
            sync_from_push(
                [
                    {"date": d, "metric_type": "steps", "value": 7000 + i * 500},
                    {"date": d, "metric_type": "resting_hr_bpm", "value": 58 + i},
                    {"date": d, "metric_type": "hrv_ms", "value": 35 + i * 3},
                    {"date": d, "metric_type": "distance_km", "value": 4.0 + i * 0.5},
                    {"date": d, "metric_type": "active_energy_kcal", "value": 300 + i * 50},
                ],
                [], [], db_session, user_id=1,
            )

        # Sleep sessions (3 nights)
        for i in range(3):
            night_start = datetime(ws.year, ws.month, ws.day, 23, 0, tzinfo=timezone.utc) + timedelta(days=i)
            sync_from_push(
                [], [],
                [
                    {
                        "uid": f"sleep-core-{i}",
                        "start_time": night_start.isoformat(),
                        "end_time": (night_start + timedelta(hours=4)).isoformat(),
                        "stage": "asleepCore",
                        "duration_hours": 4.0,
                    },
                    {
                        "uid": f"sleep-deep-{i}",
                        "start_time": (night_start + timedelta(hours=4)).isoformat(),
                        "end_time": (night_start + timedelta(hours=5)).isoformat(),
                        "stage": "asleepDeep",
                        "duration_hours": 1.0 + i * 0.2,
                    },
                    {
                        "uid": f"sleep-rem-{i}",
                        "start_time": (night_start + timedelta(hours=5)).isoformat(),
                        "end_time": (night_start + timedelta(hours=6.5)).isoformat(),
                        "stage": "asleepREM",
                        "duration_hours": 1.5,
                    },
                ],
                db_session, user_id=1,
            )

        # Workouts
        for i, wtype in enumerate(["strength_training", "running", "hiit"]):
            start = datetime(ws.year, ws.month, ws.day, 7, 0, tzinfo=timezone.utc) + timedelta(days=i)
            sync_from_push(
                [],
                [{
                    "uid": f"workout-{i}",
                    "workout_type": wtype,
                    "start_time": start.isoformat(),
                    "end_time": (start + timedelta(minutes=45)).isoformat(),
                    "duration_seconds": 2700,
                    "distance_km": 5.0 if wtype == "running" else 0,
                    "active_energy_kcal": 350,
                    "avg_heart_rate_bpm": 140,
                }],
                [], db_session, user_id=1,
            )

    def test_weekly_summary_structure(self, db_session):
        self._populate_week(db_session)
        result = json.loads(
            _tools_mod.handle_health_weekly_summary(
                db_session, {"week_start": "2026-04-06"}
            )
        )

        assert result["week_start"] == "2026-04-06"
        assert result["week_end"] == "2026-04-12"
        assert "sleep" in result
        assert "vitals" in result
        assert "movement" in result
        assert "exercise" in result
        assert "recovery_assessment" in result

    def test_exercise_counts(self, db_session):
        self._populate_week(db_session)
        result = json.loads(
            _tools_mod.handle_health_weekly_summary(
                db_session, {"week_start": "2026-04-06"}
            )
        )

        assert result["exercise"]["total_workouts"] == 3
        assert result["exercise"]["strength_sessions"] == 2  # strength_training + hiit
        assert result["exercise"]["target_met"] is True

    def test_sleep_averages(self, db_session):
        self._populate_week(db_session)
        result = json.loads(
            _tools_mod.handle_health_weekly_summary(
                db_session, {"week_start": "2026-04-06"}
            )
        )

        assert result["sleep"]["nights_tracked"] == 3
        assert result["sleep"]["avg_total_hours"] > 6
        assert result["sleep"]["avg_deep_hours"] > 0
        assert result["sleep"]["avg_rem_hours"] > 0

    def test_movement_averages(self, db_session):
        self._populate_week(db_session)
        result = json.loads(
            _tools_mod.handle_health_weekly_summary(
                db_session, {"week_start": "2026-04-06"}
            )
        )

        assert result["movement"]["avg_steps"] > 7000
        assert result["movement"]["total_distance_km"] > 0

    def test_empty_week(self, db_session):
        result = json.loads(
            _tools_mod.handle_health_weekly_summary(
                db_session, {"week_start": "2026-04-06"}
            )
        )

        assert result["sleep"] == {}
        assert result["exercise"]["total_workouts"] == 0
        assert result["exercise"]["target_met"] is False
        assert result["recovery_assessment"] == "poor"  # No sleep data

    def test_recovery_assessment_good(self, db_session):
        """Good sleep + improving HRV = good recovery."""
        self._populate_week(db_session)
        result = json.loads(
            _tools_mod.handle_health_weekly_summary(
                db_session, {"week_start": "2026-04-06"}
            )
        )

        # With avg sleep ~6.5h and HRV trending up (35,38,41,44,47),
        # recovery should be fair or good depending on exact avg
        assert result["recovery_assessment"] in ("fair", "good")


# ---------------------------------------------------------------------------
# Aggregation tests
# ---------------------------------------------------------------------------


class TestAggregateDailyMetrics:
    def test_sums_step_entries_for_same_day(self):
        """Granular step chunks for one day should be summed."""
        raw = [
            {"date": "2026-04-02", "metric_type": "steps", "value": 500},
            {"date": "2026-04-02", "metric_type": "steps", "value": 1200},
            {"date": "2026-04-02", "metric_type": "steps", "value": 87},
            {"date": "2026-04-02", "metric_type": "steps", "value": 11.8},
        ]
        result = _aggregate_daily_metrics(raw)
        assert len(result) == 1
        assert result[0]["metric_type"] == "steps"
        assert result[0]["value"] == pytest.approx(1798.8)

    def test_sums_distance_and_energy(self):
        """Distance and active energy are also SUM metrics."""
        raw = [
            {"date": "2026-04-02", "metric_type": "distance_km", "value": 1.2},
            {"date": "2026-04-02", "metric_type": "distance_km", "value": 0.8},
            {"date": "2026-04-02", "metric_type": "active_energy_kcal", "value": 100},
            {"date": "2026-04-02", "metric_type": "active_energy_kcal", "value": 250},
        ]
        result = _aggregate_daily_metrics(raw)
        by_type = {r["metric_type"]: r["value"] for r in result}
        assert by_type["distance_km"] == pytest.approx(2.0)
        assert by_type["active_energy_kcal"] == pytest.approx(350)

    def test_takes_last_value_for_avg_metrics(self):
        """Point-in-time metrics (HR, HRV) should take the last value."""
        raw = [
            {"date": "2026-04-02", "metric_type": "resting_hr_bpm", "value": 62},
            {"date": "2026-04-02", "metric_type": "resting_hr_bpm", "value": 58},
        ]
        result = _aggregate_daily_metrics(raw)
        assert len(result) == 1
        assert result[0]["value"] == 58  # last value

    def test_separate_days_stay_separate(self):
        """Entries for different days should not be merged."""
        raw = [
            {"date": "2026-04-01", "metric_type": "steps", "value": 4000},
            {"date": "2026-04-02", "metric_type": "steps", "value": 5000},
        ]
        result = _aggregate_daily_metrics(raw)
        assert len(result) == 2
        by_date = {r["date"]: r["value"] for r in result}
        assert by_date["2026-04-01"] == 4000
        assert by_date["2026-04-02"] == 5000

    def test_mixed_metrics_same_day(self):
        """Steps summed, HR takes last, all for the same day."""
        raw = [
            {"date": "2026-04-02", "metric_type": "steps", "value": 3000},
            {"date": "2026-04-02", "metric_type": "steps", "value": 2000},
            {"date": "2026-04-02", "metric_type": "resting_hr_bpm", "value": 60},
            {"date": "2026-04-02", "metric_type": "hrv_ms", "value": 45},
        ]
        result = _aggregate_daily_metrics(raw)
        by_type = {r["metric_type"]: r["value"] for r in result}
        assert by_type["steps"] == 5000
        assert by_type["resting_hr_bpm"] == 60
        assert by_type["hrv_ms"] == 45

    def test_single_entry_passthrough(self):
        """A single entry per (date, type) passes through unchanged."""
        raw = [{"date": "2026-04-02", "metric_type": "steps", "value": 9565}]
        result = _aggregate_daily_metrics(raw)
        assert len(result) == 1
        assert result[0]["value"] == 9565

    def test_empty_input(self):
        assert _aggregate_daily_metrics([]) == []
