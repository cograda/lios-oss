"""The algo harness, exercised end to end.

A harness with no tenant is a harness nobody has verified — the same mistake as
a sanitiser you do not check. So this file builds a real deriver (`_Synthetic`,
below) against a deliberately learnable signal and drives the whole loop:
features -> train -> JSON artifact -> activate -> predict -> record -> publish
to HA -> score against observed reality -> metrics with skill against a
baseline. If the harness is broken, these fail; nothing here asserts on
implementation detail that could pass while the loop is dead.

Tiering follows the project rule — marks at class/test level, never
module-level, so a `pytest -m "not db"` run still executes everything that does
not genuinely need Postgres. The pure maths (estimators, feature alignment,
scoring arithmetic) is unit tier; anything writing a row is `db`.
"""

from __future__ import annotations

import contextlib
import math
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dataclasses import replace

from app.algo import estimators
from app.algo.base import AlgoIntegration
from app.algo.features import FeatureMismatch, FeatureVector
from app.algo.spec import AlgoSpec, Quantity

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Estimators — fit with scikit-learn, serve from JSON
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEstimators:
    def test_ridge_params_survive_a_json_round_trip(self):
        """The whole reason artifacts are JSON: the served model must be the
        fitted model after a trip through Postgres."""
        import json

        names = ["a", "b"]
        X = [[float(i), float(i % 3)] for i in range(60)]
        y = [3.0 * r[0] - 2.0 * r[1] + 1.0 for r in X]

        est = estimators.RidgeEstimator(alpha=0.01)
        params = est.fit(X, y, names)
        direct = est.predict(params, [10.0, 1.0], names)
        round_tripped = est.predict(json.loads(json.dumps(params)), [10.0, 1.0], names)

        assert direct == pytest.approx(round_tripped)
        assert direct == pytest.approx(3.0 * 10.0 - 2.0 * 1.0 + 1.0, abs=0.5)

    def test_a_constant_column_does_not_produce_nan_predictions(self):
        """Zero-variance columns are the classic standardisation trap: dividing
        by a zero scale yields inf coefficients and then NaN forever."""
        names = ["varies", "constant"]
        X = [[float(i), 7.0] for i in range(40)]
        y = [2.0 * r[0] for r in X]

        est = estimators.RidgeEstimator(alpha=0.01)
        params = est.fit(X, y, names)
        assert all(math.isfinite(c) for c in params["coef"])
        assert math.isfinite(est.predict(params, [5.0, 7.0], names))

    def test_bucket_mean_falls_back_to_the_global_mean_for_an_unseen_bucket(self):
        """An unseen bucket is a normal condition — the first cold snap of the
        year — not an error, so it must not raise."""
        names = ["hour"]
        X = [[float(h)] for h in range(10)]
        y = [float(h) * 10 for h in range(10)]

        est = estimators.BucketMeanEstimator(feature="hour", width=1.0)
        params = est.fit(X, y, names)
        assert est.predict(params, [3.0], names) == pytest.approx(30.0)
        assert est.predict(params, [23.0], names) == pytest.approx(params["global_mean"])

    def test_bucket_mean_refuses_a_feature_it_is_not_given(self):
        with pytest.raises(ValueError, match="not in the feature set"):
            estimators.BucketMeanEstimator(feature="absent").fit([[1.0]], [1.0], ["hour"])

    def test_ridge_refuses_a_vector_of_the_wrong_width(self):
        """A model version and a feature set that disagree must fail loudly.
        Silently predicting from a truncated vector is a wrong number that
        looks like a right one."""
        est = estimators.RidgeEstimator()
        params = est.fit([[1.0, 2.0], [2.0, 1.0], [3.0, 5.0]], [1.0, 2.0, 3.0], ["a", "b"])
        with pytest.raises(ValueError, match="the model version and the feature set disagree"):
            est.predict(params, [1.0], ["a"])

    def test_registering_over_an_existing_estimator_kind_is_refused(self):
        """Stored artifacts reference estimators by name only, so rebinding a
        name silently changes how existing models are interpreted."""
        with pytest.raises(ValueError, match="already registered"):
            estimators.register_estimator("ridge", lambda: None)

    def test_a_new_estimator_can_be_registered_from_outside_the_kernel(self):
        class _Constant:
            kind = "test_constant"

            def fit(self, X, y, names):
                return {"c": 42.0}

            def predict(self, params, values, names):
                return params["c"]

        try:
            estimators.register_estimator("test_constant", _Constant)
            assert estimators.build("test_constant").predict({"c": 42.0}, [], []) == 42.0
        finally:
            estimators.ESTIMATORS.pop("test_constant", None)


# ---------------------------------------------------------------------------
# Features — the train/serve-skew defence
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFeatureVector:
    def test_alignment_imposes_the_fitted_column_order(self):
        """A dict has no order; a coefficient vector does. Serving must use the
        order the fit produced, not whatever the dict happens to iterate in."""
        vec = FeatureVector.aligned({"b": 2.0, "a": 1.0}, ["a", "b"])
        assert vec.names == ("a", "b")
        assert vec.values == (1.0, 2.0)

    def test_a_feature_added_since_the_fit_raises_rather_than_being_ignored(self):
        with pytest.raises(FeatureMismatch, match="unexpected"):
            FeatureVector.aligned({"a": 1.0, "new": 9.0}, ["a"])

    def test_a_missing_feature_is_not_padded_with_zero(self):
        """Zero-filling produces a number that looks like a prediction and is
        not one — the single most tempting wrong answer here."""
        with pytest.raises(FeatureMismatch, match="missing"):
            FeatureVector.aligned({"a": 1.0}, ["a", "b"])

    def test_non_finite_values_are_rejected_at_the_boundary(self):
        with pytest.raises(FeatureMismatch, match="non-finite"):
            FeatureVector.from_dict({"a": float("nan")}).validate_finite()

    def test_the_hash_ignores_float_noise_but_not_real_change(self):
        base = FeatureVector.from_dict({"a": 1.0})
        assert base.hash == FeatureVector.from_dict({"a": 1.0 + 1e-9}).hash
        assert base.hash != FeatureVector.from_dict({"a": 1.001}).hash

    def test_the_hash_is_order_independent(self):
        assert (
            FeatureVector.from_dict({"a": 1.0, "b": 2.0}).hash
            == FeatureVector.from_dict({"b": 2.0, "a": 1.0}).hash
        )


# ---------------------------------------------------------------------------
# Scoring arithmetic
# ---------------------------------------------------------------------------


class _Row:
    """Minimal stand-in for AlgoPrediction — scoring's maths reads five fields
    and touching a real table for arithmetic would push this into the db tier
    for no gain."""

    def __init__(self, value, actual, baseline=None, horizon_min=60):
        self.value = value
        self.actual = actual
        self.baseline = baseline
        self.horizon_min = horizon_min
        self.error = None if actual is None else actual - value


@pytest.mark.unit
class TestScoringMaths:
    def test_bias_keeps_its_sign_where_mae_does_not(self):
        """The reason both exist: a model 10 low every time is trivially
        fixable, one 10 out in random directions is not, and MAE cannot tell
        them apart."""
        from app.algo.scoring import _error_stats

        consistent = _error_stats([_Row(10.0, 20.0), _Row(30.0, 40.0)])
        alternating = _error_stats([_Row(10.0, 20.0), _Row(30.0, 20.0)])

        assert consistent["mae"] == pytest.approx(10.0)
        assert alternating["mae"] == pytest.approx(10.0)
        assert consistent["bias"] == pytest.approx(10.0)
        assert alternating["bias"] == pytest.approx(0.0)

    def test_skill_is_positive_only_when_the_model_beats_its_baseline(self):
        from app.algo.scoring import _error_stats

        better = _error_stats([_Row(19.0, 20.0, baseline=10.0)])
        worse = _error_stats([_Row(10.0, 20.0, baseline=19.0)])
        assert better["skill"] > 0
        assert worse["skill"] < 0

    def test_skill_reports_how_many_rows_it_is_computed_over(self):
        """A skill number over three of four hundred rows is not a skill
        number, and averaging it silently against the full set hides that."""
        from app.algo.scoring import _error_stats

        stats = _error_stats([_Row(1.0, 2.0, baseline=5.0), _Row(1.0, 2.0)])
        assert stats["n"] == 2
        assert stats["baseline_n"] == 1


# ---------------------------------------------------------------------------
# The whole loop, against real Postgres
# ---------------------------------------------------------------------------


class _Synthetic(AlgoIntegration):
    """A deriver over a signal that is genuinely learnable and genuinely not
    trivial: a daily sine plus a linear trend.

    Deliberately *not* a signal persistence would nail, because the point of
    the skill metric is to catch a model that is merely echoing the last
    reading. `truth()` is the world; `observe()` is the deriver's window onto
    it, and `features()` may only look at things knowable at `made_at`.
    """

    SPEC = AlgoSpec(
        algo="synthetic_algo",
        quantities=[Quantity(name="level", unit="units", ha_entity="sensor.synthetic_level")],
        horizons=[60, 180],
        estimator="ridge",
        train_window_days=20,
        train_stride_min=120,
        min_train_rows=40,
        holdout_fraction=0.2,
    )

    #: Anything at or after this is "the future" and cannot be observed. Lets a
    #: test move the boundary rather than sleep.
    horizon_of_knowledge: datetime | None = None

    @property
    def name(self) -> str:
        return "synthetic_algo"

    @property
    def display_name(self) -> str:
        return "Synthetic Algo"

    @staticmethod
    def truth(at: datetime) -> float:
        hours = at.timestamp() / 3600.0
        return 50.0 + 20.0 * math.sin(2 * math.pi * hours / 24.0) + 0.01 * hours % 5

    def features(self, session, made_at, target_at):
        return {
            "recent": self.truth(made_at - timedelta(hours=1)),
            "target_hour_sin": math.sin(2 * math.pi * target_at.hour / 24.0),
            "target_hour_cos": math.cos(2 * math.pi * target_at.hour / 24.0),
            "horizon_min": (target_at - made_at).total_seconds() / 60.0,
        }

    def observe(self, session, quantity, at):
        if self.horizon_of_knowledge is not None and at >= self.horizon_of_knowledge:
            return None
        return self.truth(at)


@pytest.mark.db
class TestFullLoop:
    def _fit_and_activate(self, session, algo):
        result = algo.train(session)
        assert "version" in result, f"training did not fit: {result}"
        return result

    def test_train_predict_score_produces_graded_predictions(self, db_session, monkeypatch):
        """The end-to-end proof: a fit becomes an artifact, the artifact
        produces recorded predictions, and the scoring pass grades them against
        observed reality."""
        from app.algo import artifacts, scoring
        from app.models.algo import AlgoPrediction

        algo = _Synthetic()
        # No HA configured in the test environment, so the push is a no-op —
        # asserted separately below rather than mocked away here.
        monkeypatch.setattr("app.algo.sinks.publish_to_ha", lambda *a, **k: (0, []))

        self._fit_and_activate(db_session, algo)
        artifact = artifacts.load_active(db_session, "synthetic_algo")
        assert artifact is not None and artifact.kind == "ridge"
        # The artifact is JSON, not a pickle — the rule the whole estimator
        # design exists to keep.
        import json

        json.dumps(artifact.params)

        result = algo.run_predict(db_session)
        assert result["written"] == len(algo.SPEC.quantities) * len(algo.SPEC.horizons)

        rows = db_session.query(AlgoPrediction).all()
        assert {r.horizon_min for r in rows} == {60, 180}
        assert all(r.features_hash and r.algo_version == artifact.version for r in rows)
        assert all(r.baseline is not None for r in rows), "baseline must be recorded up front"

        # Move every target into the observable past and score.
        for row in rows:
            row.target_at = row.target_at - timedelta(days=2)
            row.made_at = row.made_at - timedelta(days=2)
        db_session.commit()

        scored = algo.run_score(db_session)
        assert scored["scored"] == len(rows)

        graded = db_session.query(AlgoPrediction).all()
        assert all(g.actual is not None and g.scored_at is not None for g in graded)
        assert all(g.error == pytest.approx(g.actual - g.value) for g in graded)

        metrics = scoring.metrics(db_session, "synthetic_algo", days=30)
        assert metrics["graded"] == len(rows)
        assert metrics["mae"] is not None
        assert set(metrics["by_horizon"]) == {"60", "180"}

    def test_the_model_beats_persistence_on_this_signal(self, db_session, monkeypatch):
        """Skill, measured rather than assumed.

        A harness that cannot demonstrate positive skill on a signal built to
        be learnable is not measuring skill correctly — this test is as much a
        check on the scoring code as on the estimator.
        """
        from app.algo import scoring
        from app.models.algo import AlgoPrediction

        algo = _Synthetic()
        monkeypatch.setattr("app.algo.sinks.publish_to_ha", lambda *a, **k: (0, []))
        self._fit_and_activate(db_session, algo)

        # Twelve cycles at two-hour spacing, all placed in the observable past,
        # so skill is averaged over a full day rather than one lucky moment.
        base = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(days=3)
        for i in range(12):
            made_at = base + timedelta(hours=2 * i)
            for horizon in algo.SPEC.horizons:
                target_at = made_at + timedelta(minutes=horizon)
                value, fhash, version = algo.predict(db_session, "level", made_at, target_at)
                from app.algo import predictions as pred_store

                pred_store.record(
                    db_session,
                    algo="synthetic_algo",
                    quantity="level",
                    made_at=made_at,
                    target_at=target_at,
                    value=value,
                    features_hash=fhash,
                    algo_version=version,
                    baseline=algo.baseline(db_session, "level", made_at, target_at),
                )
        db_session.commit()

        algo.run_score(db_session)
        stats = scoring.metrics(db_session, "synthetic_algo", days=30)
        assert stats["graded"] == 24
        assert stats["baseline_n"] == 24
        assert stats["skill"] > 0, f"no skill over persistence: {stats}"

    def test_a_rerun_of_the_same_cycle_updates_rather_than_duplicates(
        self, db_session, monkeypatch
    ):
        """A retry, a manual trigger or an APScheduler misfire catch-up must
        not stack duplicate rows at the same horizon."""
        from app.models.algo import AlgoPrediction

        algo = _Synthetic()
        monkeypatch.setattr("app.algo.sinks.publish_to_ha", lambda *a, **k: (0, []))
        self._fit_and_activate(db_session, algo)

        algo.run_predict(db_session)
        first = db_session.query(AlgoPrediction).count()
        algo.run_predict(db_session)
        assert db_session.query(AlgoPrediction).count() == first

    def test_scoring_does_not_erase_an_observation_when_a_cycle_reruns(
        self, db_session, monkeypatch
    ):
        """The upsert deliberately leaves actual/error/scored_at alone. Losing
        ground truth to a re-run would be far worse than a slightly stale
        prediction sitting next to it."""
        from app.algo import predictions as pred_store
        from app.models.algo import AlgoPrediction

        made_at = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(days=1)
        target_at = made_at + timedelta(hours=1)
        common = dict(algo="synthetic_algo", quantity="level", made_at=made_at, target_at=target_at)

        pred_store.record(db_session, value=10.0, **common)
        row = db_session.query(AlgoPrediction).one()
        row.actual, row.error, row.scored_at = 12.0, 2.0, datetime.now(UTC)
        db_session.commit()

        pred_store.record(db_session, value=11.0, **common)
        db_session.commit()

        row = db_session.query(AlgoPrediction).one()
        assert row.value == 11.0
        assert row.actual == 12.0 and row.scored_at is not None

    def test_scoring_waits_out_the_spec_grace_window(self, db_session):
        """The scoring-grace fix (plan of record, stock take B). A deriver whose
        observe() averages over target_at ± N minutes must not be scored
        before target_at + N, or it grades a half-window mean. The grace is
        the spec's, passed through run_score — not the harness default."""
        from dataclasses import replace

        from app.algo import predictions as pred_store
        from app.models.algo import AlgoPrediction

        class _Patient(_Synthetic):
            SPEC = replace(_Synthetic.SPEC, score_grace_min=15)

        algo = _Patient()
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        # Observable (nothing caps knowledge), 8 minutes past target: inside
        # the 15-minute grace, past the harness default of 5.
        pred_store.record(
            db_session, algo="synthetic_algo", quantity="level",
            made_at=now - timedelta(minutes=68), target_at=now - timedelta(minutes=8), value=1.0,
        )
        db_session.commit()

        # Inside the grace: not due, so neither scored nor counted as waiting.
        assert algo.run_score(db_session)["scored"] == 0
        assert db_session.query(AlgoPrediction).filter(AlgoPrediction.scored_at.isnot(None)).count() == 0

        # The same row under the harness default grace of 5 minutes IS due —
        # which is exactly the half-window grade the spec field exists to stop.
        class _Impatient(_Synthetic):
            SPEC = replace(_Synthetic.SPEC, score_grace_min=5)

        assert _Impatient().run_score(db_session)["scored"] == 1

    def test_an_unobservable_prediction_waits_then_is_written_off(self, db_session):
        """Unobservable is neither scored nor retried forever. Without a cap,
        every prediction with no recoverable ground truth is re-queried on
        every pass — the unbounded-retry shape that would have flooded
        EventKit with 107k reminder rows."""
        from app.algo import predictions as pred_store
        from app.models.algo import AlgoPrediction

        algo = _Synthetic()
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        # Nothing at or after (now - 30 days) is observable, so both rows below
        # are unobservable; only the older one is past the write-off cutoff.
        algo.horizon_of_knowledge = now - timedelta(days=30)

        for days, horizon in ((1, 60), (10, 180)):
            made_at = now - timedelta(days=days)
            pred_store.record(
                db_session,
                algo="synthetic_algo",
                quantity="level",
                made_at=made_at,
                target_at=made_at + timedelta(minutes=horizon),
                value=1.0,
            )
        db_session.commit()

        result = algo.run_score(db_session)
        assert result["scored"] == 0
        assert result["waiting"] == 1
        assert result["abandoned"] == 1

        # A written-off row is distinguishable: scored_at set, actual NULL.
        written_off = (
            db_session.query(AlgoPrediction)
            .filter(AlgoPrediction.scored_at.isnot(None))
            .all()
        )
        assert len(written_off) == 1 and written_off[0].actual is None

    def test_a_prediction_cycle_with_no_active_model_is_a_no_op_not_a_failure(
        self, db_session, monkeypatch
    ):
        """A deriver's first prediction cycle can legitimately run before its
        first training cycle."""
        from app.models.algo import AlgoPrediction, AlgoRun

        algo = _Synthetic()
        monkeypatch.setattr("app.algo.sinks.publish_to_ha", lambda *a, **k: (0, []))

        result = algo.run_predict(db_session)
        assert result["written"] == 0 and result["skipped"] == 2
        assert db_session.query(AlgoPrediction).count() == 0
        run = db_session.query(AlgoRun).filter(AlgoRun.kind == "predict").one()
        assert run.ok is True

    def test_training_refuses_to_fit_below_the_minimum_row_count(self, db_session):
        """A model fitted on eleven examples still produces confident numbers,
        which is exactly why this is a floor and not a warning."""
        from app.models.algo import AlgoModelVersion

        algo = _Synthetic()
        algo.SPEC = replace(_Synthetic.SPEC, min_train_rows=10_000)
        try:
            result = algo.train(db_session)
        finally:
            algo.SPEC = _Synthetic.SPEC
        assert result["skipped"] == "insufficient_rows"
        assert db_session.query(AlgoModelVersion).count() == 0

    def test_a_saved_model_is_inactive_until_activated(self, db_session):
        """A fit does not become live by existing. One bad week of input data
        must not replace a working model within the hour."""
        from app.algo import artifacts

        algo = _Synthetic()
        algo.train(db_session, activate=False)
        assert artifacts.load_active(db_session, "synthetic_algo") is None

        algo.train(db_session, activate=True)
        active = artifacts.load_active(db_session, "synthetic_algo")
        assert active is not None and active.version == 2

    def test_only_one_version_is_ever_active(self, db_session):
        """Two active versions would make load_active() return whichever the
        query ordering happened to pick — an unreproducible prediction weeks
        later."""
        from app.algo import artifacts
        from app.models.algo import AlgoModelVersion

        algo = _Synthetic()
        algo.train(db_session)
        algo.train(db_session)
        active = (
            db_session.query(AlgoModelVersion)
            .filter(AlgoModelVersion.is_active.is_(True))
            .all()
        )
        assert len(active) == 1 and active[0].version == 2

    def test_features_is_the_only_feature_path_for_both_train_and_serve(
        self, db_session, monkeypatch
    ):
        """The train/serve-skew defence, asserted rather than documented.

        Both paths must go through the same `features()`. If someone adds a
        second feature builder for the serving path, this fails.
        """
        algo = _Synthetic()
        monkeypatch.setattr("app.algo.sinks.publish_to_ha", lambda *a, **k: (0, []))
        calls = {"n": 0}
        original = _Synthetic.features

        def spy(self, session, made_at, target_at):
            calls["n"] += 1
            return original(self, session, made_at, target_at)

        monkeypatch.setattr(_Synthetic, "features", spy)

        algo.train(db_session)
        after_train = calls["n"]
        assert after_train > 0, "training did not go through features()"

        algo.run_predict(db_session)
        assert calls["n"] > after_train, "prediction did not go through features()"

    def test_the_forecast_and_accuracy_tools_answer_from_the_stored_rows(
        self, db_session, monkeypatch
    ):
        """The comar-app half of the output story: two MCP tools, generated
        from the spec, reading the same rows HA reads."""
        import json

        from app.algo import sinks

        algo = _Synthetic()
        monkeypatch.setattr("app.algo.sinks.publish_to_ha", lambda *a, **k: (0, []))
        algo.train(db_session)
        algo.run_predict(db_session)

        tools = {t["name"]: t for t in algo.mcp_tools()}
        assert set(tools) == {"synthetic_algo_forecast", "synthetic_algo_accuracy"}
        assert all(t["annotations"]["readOnlyHint"] for t in tools.values())

        forecast = json.loads(
            tools["synthetic_algo_forecast"]["handler"](db_session, {"quantity": "level"})
        )
        assert [p["horizon_min"] for p in forecast["points"]] == [60, 180]
        # Accuracy travels with the forecast on purpose: a prediction read
        # without its track record invites unearned confidence.
        assert "recent_accuracy" in forecast

        accuracy = json.loads(tools["synthetic_algo_accuracy"]["handler"](db_session, {}))
        assert accuracy["algo"] == "synthetic_algo"

    def test_an_undeclared_quantity_is_refused_by_the_generated_tool(self, db_session):
        tools = {t["name"]: t for t in _Synthetic().mcp_tools()}
        with pytest.raises(KeyError):
            tools["synthetic_algo_forecast"]["handler"](db_session, {"quantity": "nope"})

    def test_a_dead_home_assistant_does_not_fail_a_good_prediction_cycle(
        self, db_session, monkeypatch
    ):
        """Ordering copied from commute: the durable Postgres record is the
        product, the sensor is a projection of it."""
        from app.models.algo import AlgoPrediction

        algo = _Synthetic()
        algo.train(db_session)
        monkeypatch.setattr(
            "app.algo.sinks.publish_to_ha",
            lambda *a, **k: (0, ["sensor.synthetic_level"]),
        )

        result = algo.run_predict(db_session)
        assert result["ha_failed"] == ["sensor.synthetic_level"]
        assert db_session.query(AlgoPrediction).count() == 2

    def test_a_broken_baseline_does_not_stop_a_prediction_being_recorded(
        self, db_session, monkeypatch
    ):
        """The baseline is the comparison, not the answer."""
        from app.models.algo import AlgoPrediction

        algo = _Synthetic()
        monkeypatch.setattr("app.algo.sinks.publish_to_ha", lambda *a, **k: (0, []))
        algo.train(db_session)
        monkeypatch.setattr(
            _Synthetic, "baseline", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        )

        algo.run_predict(db_session)
        rows = db_session.query(AlgoPrediction).all()
        assert len(rows) == 2 and all(r.baseline is None for r in rows)

    def test_a_failed_prediction_cycle_records_a_failed_run(self, db_session, monkeypatch):
        from app.models.algo import AlgoRun

        algo = _Synthetic()
        algo.train(db_session)
        monkeypatch.setattr(
            _Synthetic, "features", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no data"))
        )

        with pytest.raises(RuntimeError):
            algo.run_predict(db_session)

        failed = db_session.query(AlgoRun).filter(AlgoRun.ok.is_(False)).all()
        assert len(failed) == 1 and "no data" in failed[0].error


@pytest.mark.unit
class TestNoModuleLevelModelImports:
    """`app.algo` must not import `app.models` at module scope.

    `app/models/__init__.py` runs `discover_integration_models()` at import
    time, which imports every integration package, which imports `app.algo` —
    so a module-level model import here makes `import app.algo` circular the
    moment any deriver exists. It did: adding `solar_forecast` broke collection
    of the entire test suite with a partially-initialised-module ImportError.

    Asserted structurally rather than by importing, because by the time a test
    runs the modules are already in `sys.modules` and the cycle no longer
    reproduces in-process — which is exactly how this would come back.
    """

    def test_app_algo_never_imports_app_models_at_module_scope(self):
        import ast

        algo_dir = Path(__file__).resolve().parent.parent / "app" / "algo"
        offenders = []
        for path in sorted(algo_dir.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in tree.body:  # module scope only — nested imports are fine
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.models"):
                    offenders.append(f"{path.name}:{node.lineno} from {node.module}")
                elif isinstance(node, ast.Import):
                    offenders += [
                        f"{path.name}:{node.lineno} import {a.name}"
                        for a in node.names
                        if a.name.startswith("app.models")
                    ]
        assert offenders == [], (
            "module-level app.models import(s) in app/algo — move them inside "
            "the function that needs them:\n" + "\n".join(offenders)
        )

    def test_the_typing_only_imports_are_guarded(self):
        """A `TYPE_CHECKING` block is the allowed way to reference a model in an
        annotation, and it must stay guarded — an unguarded one is a real import
        that would reintroduce the cycle."""
        import ast

        algo_dir = Path(__file__).resolve().parent.parent / "app" / "algo"
        for path in sorted(algo_dir.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.If) and ast.dump(node.test).find("TYPE_CHECKING") >= 0:
                    for stmt in node.body:
                        assert isinstance(stmt, (ast.Import, ast.ImportFrom)), (
                            f"{path.name}: TYPE_CHECKING block should contain only imports"
                        )


@pytest.mark.unit
class TestSpecValidation:
    def test_a_copy_pasted_spec_whose_algo_does_not_match_the_name_is_refused(self):
        """Otherwise predictions land under another algo's name and the rows
        look entirely plausible."""

        class _Mismatched(_Synthetic):
            @property
            def name(self):
                return "something_else"

        with pytest.raises(ValueError, match="they must match"):
            _Mismatched()

    def test_the_training_grid_is_aligned_to_the_stride_not_to_the_clock(self):
        """The bug this guards was real, silent, and found the hard way.

        Source data lands on a grid of its own — an hourly sensor records on the
        hour. A training grid offset by however many minutes past the hour the
        job happened to start can miss every single sample, `observe()` returns
        None for every pair, and the only symptom is `insufficient_rows`: a
        forecaster that never trains, with nothing in the log saying why. Found
        while building `solar_forecast`, whose history is hourly and whose
        observation window is ±15 minutes.
        """
        from app.algo.base import _floor_to_stride

        at = datetime(2026, 8, 22, 13, 47, 31, 500, tzinfo=UTC)
        assert _floor_to_stride(at, 60) == datetime(2026, 8, 22, 13, 0, tzinfo=UTC)
        assert _floor_to_stride(at, 30) == datetime(2026, 8, 22, 13, 30, tzinfo=UTC)
        assert _floor_to_stride(at, 15) == datetime(2026, 8, 22, 13, 45, tzinfo=UTC)
        # A degenerate stride must not divide by zero — fall back to the minute.
        assert _floor_to_stride(at, 0) == datetime(2026, 8, 22, 13, 47, tzinfo=UTC)

    def test_training_pairs_land_on_stride_boundaries(self):
        """The property the helper exists for, asserted through the real
        generator rather than only through the helper."""
        algo = _Synthetic()
        pairs = []
        for i, pair in enumerate(algo.training_pairs(None, 60)):
            if i >= 5:
                break
            pairs.append(pair)
        stride = _Synthetic.SPEC.train_stride_min
        for made_at, target_at in pairs:
            assert (made_at.hour * 60 + made_at.minute) % stride == 0
            assert made_at.second == 0 and made_at.microsecond == 0
            assert (target_at - made_at) == timedelta(minutes=60)

    def test_a_deriver_with_no_horizons_is_refused(self):
        class _NoHorizons(_Synthetic):
            SPEC = replace(_Synthetic.SPEC, horizons=[])

        with pytest.raises(ValueError, match="horizons is empty"):
            _NoHorizons()

    def test_a_deriver_with_no_quantities_is_refused(self):
        class _NoQuantities(_Synthetic):
            SPEC = replace(_Synthetic.SPEC, quantities=[])

        with pytest.raises(ValueError, match="quantities is empty"):
            _NoQuantities()

    def test_a_class_with_no_spec_is_refused(self):
        class _NoSpec(AlgoIntegration):
            SPEC = None

            @property
            def name(self):
                return "x"

            @property
            def display_name(self):
                return "X"

            def features(self, session, made_at, target_at):
                return {}

            def observe(self, session, quantity, at):
                return None

        with pytest.raises(TypeError, match="must define SPEC"):
            _NoSpec()


# ---------------------------------------------------------------------------
# The drop-in property: a new deriver touches zero kernel files
# ---------------------------------------------------------------------------

ALGO_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "app" / "integrations" / "_algo_template"
INTEGRATIONS_DIR = ALGO_TEMPLATE_DIR.parent
ALGO_PLACEHOLDER = "__ALGO_NAME__"


@contextlib.contextmanager
def _dropped_in_deriver():
    """Copy `_algo_template/` to a throwaway package name and register it.

    Much simpler than `tests/test_drop_in_integration.py`'s equivalent fixture,
    and the reason is the point of the design: a deriver declares `models=[]`,
    so nothing here registers a table into the process-global
    `coglib.Base.metadata`. That whole class of phantom-table cleanup — the
    long fixup documented in that file — does not apply, because predictions
    live in the shared kernel tables instead of one table per algo.
    """
    import importlib

    name = f"zz_deriver_{uuid.uuid4().hex[:10]}"
    dest = INTEGRATIONS_DIR / name
    assert not dest.exists()
    shutil.copytree(ALGO_TEMPLATE_DIR, dest, ignore=shutil.ignore_patterns("__pycache__"))
    try:
        for py_file in dest.rglob("*.py"):
            py_file.write_text(py_file.read_text().replace(ALGO_PLACEHOLDER, name))
            assert ALGO_PLACEHOLDER not in py_file.read_text()
        importlib.invalidate_caches()
        yield name
    finally:
        from app.integrations import INTEGRATIONS, register_all

        prefix = f"app.integrations.{name}"
        for mod in [m for m in sys.modules if m == prefix or m.startswith(prefix + ".")]:
            del sys.modules[mod]
        shutil.rmtree(dest, ignore_errors=True)
        importlib.invalidate_caches()
        INTEGRATIONS.clear()
        register_all()


@pytest.mark.unit
class TestDeriverDropsIn:
    def test_a_copied_deriver_registers_validates_and_schedules_itself(self, monkeypatch):
        """The north star, for derivers: package + manifest + config, no kernel
        edits. If this passes, adding a forecaster is a directory."""
        from app.integrations import INTEGRATIONS, get_all, register_all
        from app.plugin.validate import discover_manifests, validate_manifests

        with _dropped_in_deriver() as name:
            INTEGRATIONS.clear()
            register_all()

            integration = get_all()[name]
            assert isinstance(integration, AlgoIntegration)
            assert integration.SPEC.algo == name

            manifests = discover_manifests()
            assert manifests[name].type == "deriver"
            validate_manifests(manifests)  # raises on any inconsistency

            # Two tools, generated from the spec — no hand-written schema.
            assert {t["name"] for t in integration.mcp_tools()} == {
                f"{name}_forecast",
                f"{name}_accuracy",
            }

    def test_a_copied_deriver_gets_both_a_prediction_and_a_training_job(self, monkeypatch):
        """Two cadences, from one manifest: `schedule` drives prediction,
        `background_tasks` drives training. Conflating them is the mistake that
        makes every prediction unreproducible."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        import app.scheduler as sched_mod
        from app.integrations import INTEGRATIONS, register_all

        with _dropped_in_deriver() as name:
            INTEGRATIONS.clear()
            register_all()

            scheduler = AsyncIOScheduler()
            monkeypatch.setattr(sched_mod, "scheduler", scheduler)
            monkeypatch.setattr(scheduler, "start", lambda: None)
            # Both gates read the `integration_config` table, and this is a
            # unit-tier test with no Postgres. Patched rather than weakened:
            # an unconfigured integration genuinely should not be scheduled,
            # which is a rule this test relies on elsewhere. Same two patches
            # `tests/test_scheduler_jobs.py` uses for the same reason.
            for integration in INTEGRATIONS.values():
                monkeypatch.setattr(integration, "is_configured", lambda: True, raising=False)
            monkeypatch.setattr(sched_mod, "is_integration_enabled", lambda _n: True)

            sched_mod.setup_scheduler()
            job_ids = {j.id for j in scheduler.get_jobs()}
            assert f"sync_{name}" in job_ids
            assert f"{name}_train" in job_ids
            # And the kernel's scoring job exists once, for every deriver.
            assert "score_algo_predictions" in job_ids

    def test_the_kernel_scoring_job_finds_a_dropped_in_deriver(self):
        """Scoring is kernel-owned so a new deriver is graded from its first
        prediction without declaring anything."""
        from app.integrations import INTEGRATIONS, get_all, register_all

        with _dropped_in_deriver() as name:
            INTEGRATIONS.clear()
            register_all()
            derivers = [
                n for n, i in get_all().items() if isinstance(i, AlgoIntegration)
            ]
            assert name in derivers


# ---------------------------------------------------------------------------
# The Home Assistant sink
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestHomeAssistantSink:
    """Every other test in this file mocks `publish_to_ha` out, so this class
    is the only place the HA half is actually exercised. Written after
    noticing that gap — a sink asserted only by its absence is a sink nobody
    has run."""

    def _seed_curve(self, session, algo_name="synthetic_algo", quantity="level", n=3):
        from app.algo import predictions as pred_store

        made_at = datetime.now(UTC).replace(second=0, microsecond=0)
        for i in range(n):
            pred_store.record(
                session,
                algo=algo_name,
                quantity=quantity,
                made_at=made_at,
                target_at=made_at + timedelta(minutes=60 * (i + 1)),
                value=10.0 + i,
                unit="units",
                algo_version=1,
            )
        session.commit()
        return made_at

    @pytest.fixture
    def ha_spy(self, monkeypatch):
        """Stand in for the `homeassistant.entities` capability and its config."""
        calls = []

        class _Facade:
            def set_state(self, entity_id, state, attributes=None):
                calls.append((entity_id, state, attributes))
                return True

        monkeypatch.setattr(
            "app.plugin.capabilities.get_capability", lambda _name: _Facade()
        )
        monkeypatch.setattr(
            "app.plugin.config_store.plugin_config",
            lambda _n: type("C", (), {"ha_url": "http://ha", "ha_token": "t"})(),
        )
        return calls

    def test_the_state_is_the_next_future_point_and_the_series_is_an_attribute(
        self, db_session, ha_spy
    ):
        """An automation wants a scalar it can threshold; a dashboard wants the
        curve. One entity serves both, which is what HA's own solar-forecast
        integrations do."""
        from app.algo import sinks

        self._seed_curve(db_session)
        published, failed = sinks.publish_to_ha(db_session, _Synthetic.SPEC)

        assert (published, failed) == (1, [])
        entity_id, state, attrs = ha_spy[0]
        assert entity_id == "sensor.synthetic_level"
        assert state == pytest.approx(10.0)  # nearest future target, not the last
        assert [p["value"] for p in attrs["forecast"]] == [10.0, 11.0, 12.0]
        assert attrs["unit_of_measurement"] == "units"
        assert attrs["truncated"] is False

    def test_the_series_is_capped_so_it_cannot_bloat_has_recorder(
        self, db_session, ha_spy
    ):
        """HA stores the whole attribute blob on every state change, so an
        unbounded series is a slow, invisible way to grow its database — the
        same class of mistake as keeping 105k timelapse JPEGs in restic."""
        from app.algo import sinks

        self._seed_curve(db_session, n=sinks.MAX_HA_SERIES_POINTS + 5)
        sinks.publish_to_ha(db_session, _Synthetic.SPEC)

        _entity, _state, attrs = ha_spy[0]
        assert len(attrs["forecast"]) == sinks.MAX_HA_SERIES_POINTS
        assert attrs["truncated"] is True

    def test_a_quantity_with_no_entity_is_skipped_rather_than_invented(
        self, db_session, ha_spy
    ):
        """Publishing is opt-in per quantity: an intermediate quantity a
        deriver predicts for its own use has no business creating an entity
        somebody then builds an automation on."""
        from app.algo import sinks

        self._seed_curve(db_session)
        spec = replace(
            _Synthetic.SPEC,
            quantities=[Quantity(name="level", unit="units", ha_entity=None)],
        )
        assert sinks.publish_to_ha(db_session, spec) == (0, [])
        assert ha_spy == []

    def test_the_resolve_entity_hook_overrides_the_spec(self, db_session, ha_spy):
        """The path a real deriver uses: an entity_id is usually deployment
        config, and a room name in a committed AlgoSpec is what
        test_personalisation_guard sweeps for."""
        from app.algo import sinks

        self._seed_curve(db_session)
        sinks.publish_to_ha(
            db_session, _Synthetic.SPEC, lambda _q: "sensor.from_config"
        )
        assert ha_spy[0][0] == "sensor.from_config"

    def test_a_refused_write_is_reported_by_entity_name_not_swallowed(
        self, db_session, monkeypatch
    ):
        from app.algo import sinks

        class _Failing:
            def set_state(self, *a, **k):
                return False

        monkeypatch.setattr(
            "app.plugin.capabilities.get_capability", lambda _n: _Failing()
        )
        monkeypatch.setattr(
            "app.plugin.config_store.plugin_config",
            lambda _n: type("C", (), {"ha_url": "http://ha", "ha_token": "t"})(),
        )
        self._seed_curve(db_session)
        assert sinks.publish_to_ha(db_session, _Synthetic.SPEC) == (
            0,
            ["sensor.synthetic_level"],
        )

    def test_no_ha_credentials_means_no_push_and_no_error(self, db_session, monkeypatch):
        """HA is optional. A deriver on a machine with no HA still predicts."""
        from app.algo import sinks

        monkeypatch.setattr(
            "app.plugin.config_store.plugin_config",
            lambda _n: type("C", (), {"ha_url": "", "ha_token": ""})(),
        )
        self._seed_curve(db_session)
        assert sinks.publish_to_ha(db_session, _Synthetic.SPEC) == (0, [])

    def test_a_curve_mixes_no_cycles(self, db_session, ha_spy):
        """`curve()` returns only the newest cycle's rows. A line assembled
        from several cycles would present one-hour-old and six-hour-old claims
        as one forecast, which makes it look better than it is."""
        from app.algo import predictions as pred_store

        old_made = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=6)
        pred_store.record(
            db_session, algo="synthetic_algo", quantity="level",
            made_at=old_made, target_at=old_made + timedelta(hours=9), value=99.0,
        )
        self._seed_curve(db_session)

        rows = pred_store.curve(db_session, "synthetic_algo", "level")
        assert 99.0 not in [r.value for r in rows]
        assert len({r.made_at for r in rows}) == 1
