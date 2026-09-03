"""`solar_forecast` — the first real deriver, and the plumbing it needed.

Three things are exercised here, and the first two are prerequisites that only
exist because this deriver needed them:

1. **The numeric-history allowlist.** A deriver trains on history, and comar
   records numeric→numeric transitions for nothing by default. Turning the
   global flag on for ~1,700 entities to feed one forecaster is the wrong
   trade, so `ha_numeric_history_entities` is a per-entity opt-in.
2. **The recorder backfill.** comar starts keeping an entity's history the
   moment it is allowlisted; HA's recorder has been keeping it all along.
   Without the import, a new forecaster is blind for its whole training window.
3. **The deriver itself** — solar geometry, the feature contract, the
   climatology baseline, the night filter.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.integrations.solar_forecast.solar import solar_elevation_deg

UTC = timezone.utc

# Malahide, the coordinates in weather's own config example.
LAT, LON = 53.1459, -6.0633


# ---------------------------------------------------------------------------
# Solar geometry
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSolarElevation:
    def test_solstice_noon_elevations_match_the_textbook_values(self):
        """Peak elevation at latitude L is 90 - L ± 23.44 (the Earth's axial
        tilt). Checking against the closed form rather than a fixture means this
        catches a sign error in the declination series, which a fixture copied
        from the same code would not."""
        summer = solar_elevation_deg(datetime(2026, 6, 21, 12, 0, tzinfo=UTC), LAT, LON)
        winter = solar_elevation_deg(datetime(2026, 12, 21, 12, 0, tzinfo=UTC), LAT, LON)
        assert summer == pytest.approx(90 - LAT + 23.44, abs=1.0)
        assert winter == pytest.approx(90 - LAT - 23.44, abs=1.0)

    def test_the_sun_is_down_at_local_midnight_and_up_at_local_noon(self):
        assert solar_elevation_deg(datetime(2026, 6, 21, 0, 30, tzinfo=UTC), LAT, LON) < 0
        assert solar_elevation_deg(datetime(2026, 12, 21, 12, 0, tzinfo=UTC), LAT, LON) > 0

    def test_a_naive_timestamp_is_treated_as_utc_not_local(self):
        """Everything in the algo harness stores tz-aware UTC. Reinterpreting a
        naive value as local time would shift the whole day by an hour for half
        the year — a bug that would look like a badly-fitted model."""
        naive = datetime(2026, 6, 21, 12, 0)
        aware = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
        assert solar_elevation_deg(naive, LAT, LON) == pytest.approx(
            solar_elevation_deg(aware, LAT, LON)
        )

    def test_polar_latitudes_do_not_raise(self):
        """`acos` of a value float error pushed a hair past ±1 raises, and
        inside the Arctic circle is exactly where that happens."""
        for month in (6, 12):
            value = solar_elevation_deg(datetime(2026, month, 21, 12, 0, tzinfo=UTC), 89.9, 0.0)
            assert math.isfinite(value)


# ---------------------------------------------------------------------------
# The numeric-history allowlist
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNumericHistoryAllowlist:
    @pytest.fixture
    def gate(self, monkeypatch):
        def _configure(*, flag=False, allowlist=()):
            monkeypatch.setattr(
                "app.integrations.homeassistant.sync.plugin_config",
                lambda _n: type(
                    "C",
                    (),
                    {
                        "ha_record_numeric_history": flag,
                        "ha_numeric_history_entities": list(allowlist),
                    },
                )(),
            )
            from app.integrations.homeassistant.sync import should_record_transition

            return should_record_transition

        return _configure

    def test_numeric_ticks_are_dropped_for_an_entity_that_is_not_allowlisted(self, gate):
        should = gate()
        assert should("100", "101", "sensor.inverter_input_power") is False

    def test_numeric_ticks_are_kept_for_an_allowlisted_entity(self, gate):
        should = gate(allowlist=["sensor.inverter_input_power"])
        assert should("100", "101", "sensor.inverter_input_power") is True
        # …and only for that one. The whole point is that this stays narrow.
        assert should("20.1", "20.2", "sensor.kitchen_temperature") is False

    def test_a_transition_to_unavailable_is_always_recorded(self, gate):
        """Unchanged behaviour, and load-bearing: an inverter dropping offline
        is exactly the gap that must not be silently interpolated over."""
        should = gate()
        assert should("100", "unavailable", "sensor.inverter_input_power") is True
        assert should("unknown", "100", "sensor.inverter_input_power") is True

    def test_the_global_flag_still_works_as_the_firehose(self, gate):
        should = gate(flag=True)
        assert should("20.1", "20.2", "sensor.anything_at_all") is True

    def test_the_allowlist_cannot_match_without_an_entity_id(self, gate):
        """The parameter is optional for back-compat, but a caller that omits it
        gets flag-only behaviour — so both real call sites must pass it, which
        `test_homeassistant.py` covers for the sync path."""
        should = gate(allowlist=["sensor.inverter_input_power"])
        assert should("100", "101") is False


# ---------------------------------------------------------------------------
# The recorder backfill
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestRecorderBackfill:
    @pytest.fixture
    def ha_rows(self, monkeypatch):
        """Stand in for HA's `/api/history/period` response."""

        def _install(rows):
            monkeypatch.setattr(
                "app.integrations.homeassistant.backfill.fetch_history",
                lambda *a, **k: rows,
            )

        return _install

    @staticmethod
    def _row(minutes_ago: int, state: str) -> dict:
        at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
        return {"state": state, "last_changed": at.isoformat()}

    def test_numeric_rows_are_imported_and_non_numeric_ones_are_not(
        self, db_session, ha_rows
    ):
        """`unavailable`/`unknown` rows are already captured by the live sync;
        importing them here would add rows nothing reads while making the
        counts harder to read."""
        from app.integrations.homeassistant.backfill import backfill_entity
        from app.integrations.homeassistant.models import HAStateChange

        ha_rows([self._row(30, "100"), self._row(20, "unavailable"), self._row(10, "300")])
        result = backfill_entity(db_session, "sensor.pv", days=1)

        assert (result["fetched"], result["inserted"], result["skipped"]) == (3, 2, 1)
        stored = db_session.query(HAStateChange).all()
        assert sorted(r.new_state for r in stored) == ["100", "300"]

    def test_rerunning_tops_up_rather_than_duplicating(self, db_session, ha_rows):
        """The natural usage is to run it again whenever a gap appears, so
        idempotency isn't a nicety here."""
        from app.integrations.homeassistant.backfill import backfill_entity
        from app.integrations.homeassistant.models import HAStateChange

        first = [self._row(30, "100"), self._row(20, "200")]
        ha_rows(first)
        backfill_entity(db_session, "sensor.pv", days=1)

        ha_rows(first + [self._row(10, "300")])
        second = backfill_entity(db_session, "sensor.pv", days=1)

        assert second["inserted"] == 1
        assert db_session.query(HAStateChange).count() == 3

    def test_duplicate_timestamps_within_one_response_are_collapsed(
        self, db_session, ha_rows
    ):
        """The recorder emits several rows on one timestamp when multiple
        attributes change. There is no DB constraint to catch those, so they
        would become duplicate training rows."""
        from app.integrations.homeassistant.backfill import backfill_entity
        from app.integrations.homeassistant.models import HAStateChange

        at = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
        ha_rows([{"state": "100", "last_changed": at}, {"state": "100", "last_changed": at}])
        result = backfill_entity(db_session, "sensor.pv", days=1)

        assert result["inserted"] == 1
        assert db_session.query(HAStateChange).count() == 1

    def test_the_backfill_is_scoped_to_the_allowlist(self, db_session, ha_rows, monkeypatch):
        """Backfilling an entity comar is *not* keeping history for would import
        a series that then silently stops — one that looks complete and just
        ends."""
        from app.integrations.homeassistant.backfill import backfill_allowlisted

        ha_rows([self._row(30, "100")])
        monkeypatch.setattr(
            "app.plugin.config_store.plugin_config",
            lambda _n: type("C", (), {"ha_numeric_history_entities": ["sensor.pv"]})(),
        )
        result = backfill_allowlisted(db_session, days=1)
        assert result["entities"] == 1 and result["inserted"] == 1

    def test_an_empty_allowlist_explains_itself_rather_than_doing_nothing_quietly(
        self, db_session, monkeypatch
    ):
        from app.integrations.homeassistant.backfill import backfill_allowlisted

        monkeypatch.setattr(
            "app.plugin.config_store.plugin_config",
            lambda _n: type("C", (), {"ha_numeric_history_entities": []})(),
        )
        result = backfill_allowlisted(db_session)
        assert result["entities"] == 0 and "note" in result

    def test_a_failed_history_fetch_is_zero_rows_not_an_exception(self, db_session, ha_rows):
        """This is a backfill helper — a failure means "nothing to import", not
        "abort"."""
        from app.integrations.homeassistant.backfill import backfill_entity

        ha_rows([])
        assert backfill_entity(db_session, "sensor.pv", days=1)["inserted"] == 0


# ---------------------------------------------------------------------------
# The deriver
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestSolarForecastDeriver:
    """Driven against a synthetic-but-physical PV day: a sine bell peaking at
    solar noon, written into `ha_state_changes` as the allowlisted entity would
    be. Everything the deriver reads goes through the same path it uses in
    production."""

    ENTITY = "sensor.test_inverter_input_power"
    INCUMBENT = "sensor.test_power_production_now"

    @pytest.fixture
    def configured(self, monkeypatch):
        """Config for both `solar_forecast` and `weather` (coordinates)."""
        solar = type(
            "S",
            (),
            {
                "pv_power_entity": self.ENTITY,
                "incumbent_power_entity": self.INCUMBENT,
                "incumbent_next_hour_entity": "",
                "incumbent_remaining_today_entity": "",
                "publish_entity": "",
                "min_solar_elevation_deg": 5,
            },
        )()
        weather = type("W", (), {"weather_latitude": LAT, "weather_longitude": LON})()
        monkeypatch.setattr(
            "app.integrations.solar_forecast.plugin_config",
            lambda name: weather if name == "weather" else solar,
        )
        return solar

    def _write_history(self, session, days=30):
        """A physical-ish PV series: output proportional to sun elevation, zero
        at night, with a per-day cloud factor so the model has something to be
        wrong about."""
        from app.integrations.homeassistant.models import HAStateChange

        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        start = now - timedelta(days=days)
        t = start
        while t <= now:
            elevation = solar_elevation_deg(t, LAT, LON)
            cloud = 0.6 + 0.4 * math.sin(t.timetuple().tm_yday / 3.0)
            value = max(0.0, 5500.0 * math.sin(math.radians(max(0.0, elevation))) * cloud)
            session.add(
                HAStateChange(
                    entity_id=self.ENTITY,
                    old_state=None,
                    new_state=f"{value:.0f}",
                    changed_at=t,
                )
            )
            session.add(
                HAStateChange(
                    entity_id=self.INCUMBENT,
                    old_state=None,
                    # The incumbent forecast, with a deliberate systematic
                    # 20% over-estimate — the local bias this deriver exists
                    # to learn.
                    new_state=f"{value * 1.2:.0f}",
                    changed_at=t,
                )
            )
            t += timedelta(hours=1)
        session.commit()
        return now

    @pytest.fixture
    def algo(self, configured):
        from app.integrations.solar_forecast import SolarForecastIntegration

        return SolarForecastIntegration()

    def test_features_never_read_the_target_moments_own_output(
        self, db_session, algo
    ):
        """The leak that makes a model score beautifully offline and predict
        nothing. Asserted by giving the target moment an absurd value and
        checking no feature moved."""
        from app.integrations.homeassistant.models import HAStateChange

        now = self._write_history(db_session, days=3)
        made_at = now - timedelta(hours=6)
        target_at = made_at + timedelta(hours=3)

        before = algo.features(db_session, made_at, target_at)
        db_session.add(
            HAStateChange(
                entity_id=self.ENTITY,
                old_state=None,
                new_state="999999",
                changed_at=target_at,
            )
        )
        db_session.commit()

        assert algo.features(db_session, made_at, target_at) == before

    def test_the_feature_set_is_the_same_shape_with_an_unconfigured_incumbent(
        self, db_session, algo, configured
    ):
        """A missing entity must yield a zero column, not a missing column — the
        feature set has to stay the same shape across every training row and
        every serve, or `FeatureVector.aligned` rejects it at predict time."""
        self._write_history(db_session, days=2)
        made_at = datetime.now(UTC) - timedelta(hours=4)
        target_at = made_at + timedelta(hours=1)

        full = algo.features(db_session, made_at, target_at)
        configured.incumbent_power_entity = ""
        stripped = algo.features(db_session, made_at, target_at)

        assert set(full) == set(stripped)
        assert stripped["incumbent_now_w"] == 0.0

    def test_the_baseline_is_climatology_and_is_computed_only_from_the_past(
        self, db_session, algo
    ):
        """Persistence is the wrong bar for solar — "as much as right now, in
        six hours" is nearly meaningless. This is what a forecast has to beat:
        knowing the hour and the season and nothing about the weather."""
        now = self._write_history(db_session, days=20)
        made_at = now - timedelta(days=1)
        target_at = made_at + timedelta(hours=6)

        value = algo.baseline(db_session, "pv_power", made_at, target_at)
        assert value is not None and value >= 0

        # Nothing after made_at may contribute: a baseline that peeks is not a
        # baseline. Wiping the future leaves it unchanged.
        from app.integrations.homeassistant.models import HAStateChange

        db_session.query(HAStateChange).filter(
            HAStateChange.changed_at > made_at
        ).delete(synchronize_session=False)
        db_session.commit()
        assert algo.baseline(db_session, "pv_power", made_at, target_at) == pytest.approx(value)

    def test_night_targets_are_skipped_entirely(self, db_session, algo, monkeypatch):
        """Nights are trivially zero on both sides. Including them makes MAE
        look excellent and skill look like nothing, so the metrics stop meaning
        anything."""
        monkeypatch.setattr("app.algo.sinks.publish_to_ha", lambda *a, **k: (0, []))
        self._write_history(db_session, days=25)
        algo.train(db_session)

        midnight = datetime.now(UTC).replace(hour=1, minute=0, second=0, microsecond=0)
        assert algo.predict(db_session, "pv_power", midnight - timedelta(hours=3), midnight) is None

    def test_a_prediction_is_never_negative(self, db_session, algo, monkeypatch):
        """A linear model extrapolating past dawn will happily predict -300 W,
        which is not a small error but a physically impossible answer.

        ⚠️ The clock is pinned, and that is load-bearing rather than tidiness.
        `run_predict` reads `datetime.now()` and offers all four horizons
        (+1h/+3h/+6h/+12h) to `predict`, which returns None whenever the
        target sits below `min_solar_elevation_deg` — **5° here, not the
        horizon**. There is therefore a band each day where all four targets
        fall under 5° and nothing at all is written, and the test fails on
        "expected at least one daylight prediction": a failure about what time
        CI happened to start, not about the code under test.

        Measured on 2026-08-29, which is how narrow the band is: at 17:52 UTC
        the horizons were 3.7 / -12.8 / -27.0 / 2.6° — the two "daylight" ones
        both under the floor — and it failed, blocking the image build. At
        19:32 UTC the +12h target had swung round to 17.4° and it passed. So
        this was never "it fails at night"; it fails around dusk, and passes
        again later, which is exactly the shape that gets diagnosed as
        something else.

        09:00 UTC is chosen, not arbitrary. It sits inside the history just
        written, and it was checked against every day of the year at these
        coordinates: the worst case is 23 December, where the horizons are
        7.4 / 13.3 / 6.2 / -41.8° — still three above the floor. So the
        assertion below now depends only on the model.
        """
        from app.models.algo import AlgoPrediction

        monkeypatch.setattr("app.algo.sinks.publish_to_ha", lambda *a, **k: (0, []))
        now = self._write_history(db_session, days=25)
        algo.train(db_session)

        frozen = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if frozen > now:
            # Before 09:00 UTC today, so anchor on yesterday's — still well
            # inside the 25 days of history written above.
            frozen -= timedelta(days=1)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen if tz is not None else frozen.replace(tzinfo=None)

        monkeypatch.setattr("app.algo.base.datetime", _FrozenDatetime)
        algo.run_predict(db_session)

        values = [r.value for r in db_session.query(AlgoPrediction).all()]
        assert values, "expected at least one daylight prediction"
        assert all(v >= 0 for v in values)

    def test_it_trains_predicts_and_beats_its_own_climatology(
        self, db_session, algo, monkeypatch
    ):
        """The end-to-end claim, measured rather than assumed: on a signal with
        a real day-to-day cloud factor, a model that can see the incumbent
        forecast should beat hour-of-day climatology, which cannot."""
        from app.algo import predictions as pred_store
        from app.algo import scoring

        monkeypatch.setattr("app.algo.sinks.publish_to_ha", lambda *a, **k: (0, []))
        now = self._write_history(db_session, days=40)
        result = algo.train(db_session)
        assert "version" in result, f"training did not fit: {result}"

        # Predict from a series of past moments so scoring has observable
        # targets, and so skill is averaged over many days rather than one.
        made = 0
        for day in range(1, 8):
            for hour in (9, 12, 15):
                made_at = (now - timedelta(days=day)).replace(
                    hour=hour, minute=0, second=0, microsecond=0
                )
                for horizon in algo.SPEC.horizons:
                    target_at = made_at + timedelta(minutes=horizon)
                    got = algo.predict(db_session, "pv_power", made_at, target_at)
                    if got is None:
                        continue
                    value, fhash, version = got
                    pred_store.record(
                        db_session,
                        algo="solar_forecast",
                        quantity="pv_power",
                        made_at=made_at,
                        target_at=target_at,
                        value=value,
                        unit="W",
                        features_hash=fhash,
                        algo_version=version,
                        baseline=algo.baseline(db_session, "pv_power", made_at, target_at),
                    )
                    made += 1
        db_session.commit()
        assert made > 20, f"only {made} daylight predictions — check the elevation filter"

        algo.run_score(db_session)
        stats = scoring.metrics(db_session, "solar_forecast", days=30)
        assert stats["graded"] == made
        assert stats["skill"] is not None and stats["skill"] > 0, f"no skill: {stats}"

    def test_unconfigured_coordinates_degrade_to_no_filter_rather_than_silence(
        self, db_session, algo, monkeypatch
    ):
        """Coordinates left at 0,0 would put the house in the Gulf of Guinea and
        shift the day by hours. Failing open is right: predicting nothing, ever,
        silently is the worse outcome."""
        monkeypatch.setattr(
            "app.integrations.solar_forecast.plugin_config",
            lambda name: type("C", (), {
                "weather_latitude": 0.0, "weather_longitude": 0.0,
                "pv_power_entity": self.ENTITY, "incumbent_power_entity": "",
                "incumbent_next_hour_entity": "", "incumbent_remaining_today_entity": "",
                "publish_entity": "", "min_solar_elevation_deg": 5,
            })(),
        )
        assert algo._elevation(datetime.now(UTC)) == 90.0
