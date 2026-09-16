"""`AlgoIntegration` — the base class a deriver subclasses.

A deriver writes four things and inherits everything else:

    class SolarForecast(AlgoIntegration):
        SPEC = AlgoSpec(algo="solar_forecast", quantities=[...], horizons=[...])

        def features(self, session, made_at, target_at) -> dict[str, float]
        def observe(self, session, quantity, at) -> float | None
        def baseline(self, session, quantity, made_at, target_at) -> float | None
        # ... and `name`/`display_name`, per BaseIntegration

Inherited: the prediction cycle, the training loop, holdout evaluation,
artifact save/activate, the HA push, two MCP tools, the run ledger, and
scoring. Adding a deriver touches zero kernel files — the same north-star
property `tests/test_drop_in_integration.py` proves for ordinary integrations,
and `tests/test_algo_harness.py` proves for this.

**`features()` is called by both the prediction path and the training path.**
That is the single most important property of this class, and it is
structural, not documentary: there is one abstract method, so there is nothing
for a second implementation to drift from. Train/serve skew — the fit seeing a
feature computed one way and the live path computing it another — is the
characteristic silent failure of a predictive system, and it survives every
offline metric because both halves are individually correct.

`sync()` maps to the prediction cycle, so the kernel's existing per-integration
cron drives prediction with no new scheduling concept. Training is a manifest
`background_tasks` cron entry pointing at `train_algo`; scoring is a kernel job
that walks every deriver. Three cadences, because they genuinely differ:
predict often, train rarely, score continuously.
"""

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterable

from sqlalchemy.orm import Session

from app.algo import artifacts, estimators, predictions, scoring, sinks
from app.algo.features import FeatureVector
from app.algo.llm import AlgoLLM
from app.algo.spec import AlgoSpec
from app.integrations.base import BaseIntegration

if TYPE_CHECKING:  # pragma: no cover
    from app.models.algo import AlgoRun

logger = logging.getLogger(__name__)

# Model imports are LAZY throughout `app.algo` — see the note in
# `app/algo/artifacts.py`. A module-level `from app.models.algo import ...`
# makes `import app.algo` circular as soon as any deriver exists.


class AlgoIntegration(BaseIntegration):
    """Template-method base for `type="deriver"` integrations."""

    #: Subclasses must set this. Its `algo` must equal `self.name`.
    SPEC: AlgoSpec

    # ---- what a deriver must implement ---------------------------------

    @abstractmethod
    def features(
        self, session: Session, made_at: datetime, target_at: datetime
    ) -> dict[str, float]:
        """The feature vector for a prediction made at `made_at` about
        `target_at`.

        Both timestamps are passed because a forecast's features legitimately
        span them: what is known *now* (current generation, current cloud
        cover) and what is claimed *about the target* (the forecast irradiance
        at that hour, its hour-of-day, whether it is a weekend). Mixing them up
        is the classic leak — a feature that reads the target moment's *actual*
        value is a model that scores perfectly offline and predicts nothing.

        Must be pure with respect to wall-clock time: called with a historical
        `made_at` during training, it has to return what would have been known
        then. If a source table only holds current state, the honest answer is
        that this deriver cannot be trained on it, and it should predict from a
        hand-written rule instead of a fit.
        """

    @abstractmethod
    def observe(self, session: Session, quantity: str, at: datetime) -> float | None:
        """The ground truth for `quantity` at `at`, or None if not observable.

        None means "cannot see it yet" — the scoring pass leaves the row
        unscored and retries, then writes it off after
        `scoring.ABANDON_AFTER_DAYS`. Never return 0.0 to mean "unknown": that
        is an error of the prediction's full magnitude and it will drag every
        average it touches.
        """

    # ---- optional overrides --------------------------------------------

    def baseline(
        self, session: Session, quantity: str, made_at: datetime, target_at: datetime
    ) -> float | None:
        """The dumb answer this prediction has to beat.

        Default: persistence — the most recent observable value. It is the
        right default because it is the honest one for almost every household
        signal (tomorrow is usually like today), and because a forecast that
        cannot beat persistence is a forecast worth switching off. Recorded per
        row at prediction time so it cannot be chosen afterwards to flatter the
        model.
        """
        return self.observe(session, quantity, made_at)

    def predict(
        self, session: Session, quantity: str, made_at: datetime, target_at: datetime
    ) -> tuple[float, str | None, int | None] | None:
        """Return `(value, features_hash, model_version)` — or None to skip.

        Default: align the live features against the active model version and
        predict. Override for a deriver with no fitted model at all (a solver,
        a rule) — `SPEC.estimator = None` and this method is the whole algo.

        Returning None is a legitimate outcome, not an error: no active model
        yet, or a quantity that cannot be predicted for this target (before
        sunrise, outside a commute window). It is logged and skipped, and the
        run is still `ok`.
        """
        artifact = self._artifact(session)
        if artifact is None:
            return None
        feats = self.features(session, made_at, target_at)
        value, feature_hash = artifact.predict(feats)
        return value, feature_hash, artifact.version

    def training_pairs(
        self, session: Session, horizon_min: int
    ) -> Iterable[tuple[datetime, datetime]]:
        """Historical `(made_at, target_at)` pairs to build training rows from.

        Default: a regular grid back over `SPEC.train_window_days` at
        `SPEC.train_stride_min`. Override to restrict it — a commute model has
        no business training on Sunday nights, and rows from moments the algo
        would never be asked about make the fit worse, not more general.
        """
        # Aligned to the stride, NOT to the clock. This matters more than it
        # looks: source data lands on a grid of its own (an hourly sensor
        # records on the hour), and a training grid offset by however many
        # minutes past the hour the job happened to start can miss every
        # sample. `observe()` then returns None for every pair, and the only
        # symptom is `insufficient_rows` — a forecaster that silently never
        # trains, with nothing in the logs to say why. Found exactly that way
        # while building `solar_forecast`.
        now = _floor_to_stride(datetime.now(timezone.utc), self.SPEC.train_stride_min)
        start = now - timedelta(days=self.SPEC.train_window_days)
        stride = timedelta(minutes=self.SPEC.train_stride_min)
        horizon = timedelta(minutes=horizon_min)
        made = start
        while made + horizon <= now:
            yield made, made + horizon
            made += stride

    def estimator(self):
        """The estimator instance used for fitting. Override to pass
        hyperparameters (`estimators.build("ridge", alpha=5.0)`)."""
        if self.SPEC.estimator is None:
            raise ValueError(
                f"{self.name}: SPEC.estimator is None — this deriver does not "
                f"fit a model, so train() should never be called on it"
            )
        return estimators.build(self.SPEC.estimator)

    def ha_entity_for(self, quantity: str) -> str | None:
        """Which HA entity a quantity publishes to. Default: `Quantity.ha_entity`.

        Override when the entity is deployment config rather than a constant.
        An entity_id is often household-specific, and a room name in a
        committed `AlgoSpec` is precisely what
        `tests/test_personalisation_guard.py` exists to catch — the fix there is
        always a config key, never an allowlist entry.
        """
        return self.SPEC.quantity(quantity).ha_entity

    def llm(self) -> AlgoLLM:
        """An LLM handle whose cost lands on this run's ledger."""
        if not self.SPEC.llm_model:
            raise ValueError(f"{self.name}: SPEC.llm_model is not set")
        return AlgoLLM(self.SPEC.algo, self.SPEC.llm_model)

    # ---- inherited machinery -------------------------------------------

    def __init__(self) -> None:
        spec = getattr(type(self), "SPEC", None)
        if spec is None:
            raise TypeError(f"{type(self).__name__} must define SPEC")
        # Checked here rather than at first use: a copy-pasted SPEC would
        # otherwise write predictions under another algo's name, and the rows
        # would look entirely plausible.
        if spec.algo != self.name:
            raise ValueError(
                f"{type(self).__name__}: SPEC.algo={spec.algo!r} but name={self.name!r} "
                f"— they must match, or predictions land under the wrong algo"
            )
        if not spec.horizons:
            raise ValueError(f"{spec.algo}: SPEC.horizons is empty — nothing to predict")
        if not spec.quantities:
            raise ValueError(f"{spec.algo}: SPEC.quantities is empty")

    def mcp_tools(self) -> list[dict[str, Any]]:
        """The two generated tools, plus anything the deriver adds itself."""
        return sinks.mcp_tools(self.SPEC) + self.extra_mcp_tools()

    def extra_mcp_tools(self) -> list[dict[str, Any]]:
        return []

    def sync(self) -> None:
        """The prediction cycle — what the kernel's cron for this integration runs."""
        from app.db import get_db

        db = get_db()
        with db.session() as session:
            self.run_predict(session)

    def run_predict(self, session: Session) -> dict:
        """One prediction cycle: every quantity at every declared horizon.

        The Postgres write commits before the HA push is attempted, and the
        push can fail without failing the run. Same ordering as commute, for
        the same reason: the durable record is the product, the sensor is a
        projection of it.
        """
        spec = self.SPEC
        made_at = _floor_minute(datetime.now(timezone.utc))
        run = _start_run(session, spec.algo, "predict")
        started = time.monotonic()
        written = skipped = 0
        version_seen: int | None = None

        try:
            for q in spec.quantities:
                for horizon in spec.horizons:
                    target_at = made_at + timedelta(minutes=horizon)
                    result = self.predict(session, q.name, made_at, target_at)
                    if result is None:
                        skipped += 1
                        continue
                    value, feature_hash, version = result
                    version_seen = version if version is not None else version_seen
                    predictions.record(
                        session,
                        algo=spec.algo,
                        quantity=q.name,
                        made_at=made_at,
                        target_at=target_at,
                        value=round(float(value), q.round_to),
                        unit=q.unit,
                        algo_version=version,
                        features_hash=feature_hash,
                        run_id=run.id,
                        baseline=self._safe_baseline(session, q.name, made_at, target_at),
                    )
                    written += 1

            run.n_predictions = written
            run.model_version = version_seen
            run.ok = True
            run.duration_ms = int((time.monotonic() - started) * 1000)
            run.detail = {"skipped": skipped}
            session.commit()
        except Exception as exc:
            session.rollback()
            _fail_run(session, spec.algo, "predict", exc, started)
            raise

        published, failed = sinks.publish_to_ha(session, spec, self.ha_entity_for)
        # The push result is recorded on the already-committed run rather than
        # gating it: a dead HA must not turn a good prediction cycle into an
        # error the scheduler retries.
        run.detail = {**(run.detail or {}), "ha_published": published, "ha_failed": failed}
        session.commit()

        logger.info(
            f"{spec.algo}: {written} prediction(s), {skipped} skipped, "
            f"{published} sensor(s) pushed"
        )
        return {"written": written, "skipped": skipped, "ha_published": published, "ha_failed": failed}

    def train(self, session: Session, *, activate: bool = True) -> dict:
        """Fit one model per algo across all horizons, evaluate, maybe activate.

        One model, with the horizon available as a feature, rather than one
        model per horizon: household datasets are small, and splitting a
        thousand rows five ways produces five weak models instead of one
        adequate one. A deriver that genuinely needs per-horizon models can
        override this.

        The holdout is the most recent `SPEC.holdout_fraction` of rows **by
        time**, never a random split. A random split lets a row's neighbours —
        which are nearly the same moment — sit on both sides, so the model
        validates against data it has effectively already seen and scores far
        too well.
        """
        spec = self.SPEC
        run = _start_run(session, spec.algo, "train")
        started = time.monotonic()

        try:
            rows = self._collect_training_rows(session)
            if len(rows) < spec.min_train_rows:
                run.ok = True
                run.duration_ms = int((time.monotonic() - started) * 1000)
                run.detail = {"skipped": "insufficient_rows", "rows": len(rows), "need": spec.min_train_rows}
                session.commit()
                logger.info(
                    f"{spec.algo}: {len(rows)} labelled row(s), need "
                    f"{spec.min_train_rows} — not fitting"
                )
                return dict(run.detail)

            rows.sort(key=lambda r: r[0])  # chronological
            names = sorted(rows[0][1])
            X = [[r[1][n] for n in names] for r in rows]
            y = [r[2] for r in rows]

            split = max(1, int(len(rows) * (1 - spec.holdout_fraction)))
            est = self.estimator()
            params = est.fit(X[:split], y[:split], names)
            metrics = {
                "in_sample": _fit_error(est, params, X[:split], y[:split], names),
                "holdout": _fit_error(est, params, X[split:], y[split:], names),
                "rows": len(rows),
                "holdout_rows": len(rows) - split,
            }

            version = artifacts.save(
                session,
                algo=spec.algo,
                kind=est.kind,
                params=params,
                feature_names=names,
                trained_rows=len(rows),
                metrics=metrics,
                train_window=(rows[0][0], rows[-1][0]),
            )
            if activate:
                artifacts.activate(session, spec.algo, version.version)

            run.ok = True
            run.model_version = version.version
            run.duration_ms = int((time.monotonic() - started) * 1000)
            run.detail = {"version": version.version, "activated": activate, **metrics}
            session.commit()
            logger.info(f"{spec.algo}: fitted v{version.version} on {len(rows)} rows — {metrics['holdout']}")
            return dict(run.detail)
        except Exception as exc:
            session.rollback()
            _fail_run(session, spec.algo, "train", exc, started)
            raise

    def run_score(self, session: Session) -> dict:
        """Grade every scoreable prediction. Called by the kernel scoring job."""
        run = _start_run(session, self.SPEC.algo, "score")
        started = time.monotonic()
        try:
            result = scoring.score_predictions(
                session, self.SPEC.algo, self.observe,
                grace_min=self.SPEC.score_grace_min,
            )
            run.ok = True
            run.n_scored = result["scored"]
            run.duration_ms = int((time.monotonic() - started) * 1000)
            run.detail = result
            session.commit()
            return result
        except Exception as exc:
            session.rollback()
            _fail_run(session, self.SPEC.algo, "score", exc, started)
            raise

    async def dashboard_data(self) -> dict[str, Any]:
        from app.db import get_db

        db = get_db()
        with db.session() as session:
            artifact = artifacts.load_active(session, self.SPEC.algo)
            return {
                "algo": self.SPEC.algo,
                "model_version": artifact.version if artifact else None,
                "estimator": artifact.kind if artifact else None,
                "quantities": {
                    q.name: (lambda row: {
                        "value": row.value,
                        "target_at": row.target_at.isoformat(),
                        "made_at": row.made_at.isoformat() if row.made_at else None,
                    } if row else None)(predictions.latest(session, self.SPEC.algo, q.name))
                    for q in self.SPEC.quantities
                },
                "accuracy": scoring.metrics(session, self.SPEC.algo, days=30),
            }

    # ---- internals -----------------------------------------------------

    def _artifact(self, session: Session):
        artifact = artifacts.load_active(session, self.SPEC.algo)
        if artifact is None:
            logger.info(f"{self.SPEC.algo}: no active model version — nothing to predict from")
        return artifact

    def _safe_baseline(self, session, quantity, made_at, target_at) -> float | None:
        """A broken baseline must not stop a prediction being recorded — it is
        the comparison, not the answer."""
        try:
            value = self.baseline(session, quantity, made_at, target_at)
            return float(value) if value is not None else None
        except Exception as exc:
            logger.warning(f"{self.SPEC.algo}: baseline() failed for {quantity}: {exc}")
            return None

    def _collect_training_rows(
        self, session: Session
    ) -> list[tuple[datetime, dict[str, float], float]]:
        """`(made_at, features, label)` for every labelled historical pair.

        Pairs whose label is unobservable are dropped, not zero-filled. Feature
        computation failures are dropped too, with one warning per horizon
        rather than per row — a source that cannot reconstruct history will fail
        on thousands of pairs and would otherwise bury the log.
        """
        out: list[tuple[datetime, dict[str, float], float]] = []
        for horizon in self.SPEC.horizons:
            feature_failures = 0
            for made_at, target_at in self.training_pairs(session, horizon):
                for q in self.SPEC.quantities:
                    label = self.observe(session, q.name, target_at)
                    if label is None:
                        continue
                    try:
                        feats = self.features(session, made_at, target_at)
                        vec = FeatureVector.from_dict(feats)
                        vec.validate_finite()
                    except Exception:
                        feature_failures += 1
                        continue
                    out.append((made_at, vec.as_dict(), float(label)))
            if feature_failures:
                logger.warning(
                    f"{self.SPEC.algo}: dropped {feature_failures} training pair(s) at "
                    f"horizon {horizon}m — features() could not reconstruct that moment"
                )
        return out


def _floor_minute(dt: datetime) -> datetime:
    """Prediction cycles are minute-aligned so `target_at` values from
    successive runs land on the same grid — otherwise the unique constraint
    never matches and every retry inserts a near-duplicate row."""
    return dt.replace(second=0, microsecond=0)


def _floor_to_stride(dt: datetime, stride_min: int) -> datetime:
    """Floor to a multiple of `stride_min` minutes past midnight UTC.

    Midnight rather than the epoch so the grid falls on round times a human
    recognises (a 60-minute stride lands on the hour), which also means it
    coincides with the grid most sources record on.
    """
    if stride_min <= 0:
        return _floor_minute(dt)
    minutes = dt.hour * 60 + dt.minute
    floored = (minutes // stride_min) * stride_min
    return dt.replace(
        hour=floored // 60, minute=floored % 60, second=0, microsecond=0
    )


def _fit_error(est, params, X, y, names) -> dict:
    if not X:
        return {"n": 0}
    errors = [abs(est.predict(params, row, names) - target) for row, target in zip(X, y)]
    return {
        "n": len(errors),
        "mae": round(sum(errors) / len(errors), 4),
        "rmse": round((sum(e * e for e in errors) / len(errors)) ** 0.5, 4),
    }


def _start_run(session: Session, algo: str, kind: str) -> "AlgoRun":
    from app.models.algo import AlgoRun

    run = AlgoRun(algo=algo, kind=kind, started_at=datetime.now(timezone.utc))
    session.add(run)
    session.flush()
    return run


def _fail_run(session: Session, algo: str, kind: str, exc: Exception, started: float) -> None:
    """Record the failure on its own row after a rollback.

    A new row rather than the rolled-back one: the original `AlgoRun` insert is
    gone with the transaction, and re-attaching a detached object here is how
    you get a second, more confusing error on top of the real one.
    """
    from app.models.algo import AlgoRun

    session.add(
        AlgoRun(
            algo=algo,
            kind=kind,
            started_at=datetime.now(timezone.utc),
            ok=False,
            error=f"{type(exc).__name__}: {exc}"[:2000],
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    )
    session.commit()
    logger.error(f"{algo}: {kind} failed — {type(exc).__name__}: {exc}")


def train_algo(algo: str, *, activate: bool = True) -> dict:
    """Module-level entry point for a manifest `background_tasks` cron target.

    Manifests resolve dotted refs to functions, not bound methods, so a
    deriver's training cron points at `app.algo.base:train_algo` — but
    `TaskSpec` cron targets take no arguments, so a deriver declares a tiny
    zero-arg wrapper in its own package (see `_algo_template/training.py`).
    """
    from app.db import get_db
    from app.integrations import get_all

    integration = get_all().get(algo)
    if integration is None or not isinstance(integration, AlgoIntegration):
        raise KeyError(f"{algo} is not a registered deriver")
    db = get_db()
    with db.session() as session:
        return integration.train(session, activate=activate)
