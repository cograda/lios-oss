"""Saving, activating and loading fitted models.

A fit does not become live by being good. `save()` always writes a new,
*inactive* version; `activate()` is a separate call the trainer makes only
after the holdout metrics clear the bar. The reason is the failure shape: a
model that silently becomes live on every cron tick means a bad week of input
data quietly replaces a working model, and the first sign is a user noticing
the number is wrong. An inactive row is a fit nobody reads.

Versions are monotone per algo and never deleted here. An old version is the
only way to answer "what would the model we had in June have said?", which is
the question that separates a real regression from a change in the world.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.algo import estimators
from app.algo.features import FeatureVector

if TYPE_CHECKING:  # pragma: no cover
    from app.models.algo import AlgoModelVersion

logger = logging.getLogger(__name__)

# Model imports are LAZY throughout `app.algo` — inside functions, never at
# module scope. `app/models/__init__.py` runs `discover_integration_models()`
# at import time, which imports every integration package, which imports
# `app.algo` — so a module-level `from app.models.algo import ...` here makes
# `import app.algo` circular the moment any deriver exists. It did: adding
# `solar_forecast` broke collection of the whole test suite. Same rule
# `app/plugin/kernel_jobs.py` already follows.


@dataclass(frozen=True)
class ModelArtifact:
    """An immutable, loaded model version, ready to predict."""

    algo: str
    version: int
    kind: str
    params: dict
    feature_names: tuple[str, ...]
    metrics: dict

    def predict(self, features: dict[str, float]) -> tuple[float, str]:
        """Predict from a live feature dict. Returns `(value, features_hash)`.

        Alignment against `feature_names` happens here rather than in the
        caller, so every serving path gets the drift check whether it
        remembered to ask for it or not.
        """
        vec = FeatureVector.aligned(features, self.feature_names)
        vec.validate_finite()
        est = estimators.build(self.kind, **self._hyperparams())
        return est.predict(self.params, list(vec.values), list(vec.names)), vec.hash

    def _hyperparams(self) -> dict:
        """Reconstruct the constructor kwargs an estimator needs at predict time.

        Only `bucket_mean` needs any (it is keyed on a named feature), and it
        reads both from `params` — so the stored artifact is self-describing
        and a hyperparameter cannot drift away from the fit that used it.
        """
        if self.kind == "bucket_mean":
            return {"feature": self.params["feature"], "width": self.params["width"]}
        return {}


def save(
    session: Session,
    *,
    algo: str,
    kind: str,
    params: dict,
    feature_names: list[str],
    trained_rows: int,
    metrics: dict,
    train_window: tuple[datetime | None, datetime | None] = (None, None),
    notes: str | None = None,
) -> "AlgoModelVersion":
    """Write a new, inactive version. Returns the row (flushed, id populated)."""
    from app.models.algo import AlgoModelVersion

    latest = (
        session.query(AlgoModelVersion)
        .filter(AlgoModelVersion.algo == algo)
        .order_by(AlgoModelVersion.version.desc())
        .first()
    )
    row = AlgoModelVersion(
        algo=algo,
        version=(latest.version + 1) if latest else 1,
        kind=kind,
        params=params,
        feature_names=list(feature_names),
        trained_rows=trained_rows,
        train_window_start=train_window[0],
        train_window_end=train_window[1],
        metrics=metrics,
        is_active=False,
        notes=notes,
    )
    session.add(row)
    session.flush()
    logger.info(
        f"{algo}: saved model v{row.version} ({kind}, {trained_rows} rows) — inactive"
    )
    return row


def activate(session: Session, algo: str, version: int) -> None:
    """Make one version the serving version, deactivating the rest.

    Deactivate-then-activate in one transaction: two active versions would make
    `load_active()` return whichever the query ordering happened to pick, which
    is the kind of nondeterminism that shows up as an unreproducible prediction
    weeks later.
    """
    from app.models.algo import AlgoModelVersion

    session.query(AlgoModelVersion).filter(
        AlgoModelVersion.algo == algo, AlgoModelVersion.is_active.is_(True)
    ).update({"is_active": False}, synchronize_session=False)

    updated = (
        session.query(AlgoModelVersion)
        .filter(AlgoModelVersion.algo == algo, AlgoModelVersion.version == version)
        .update({"is_active": True}, synchronize_session=False)
    )
    if not updated:
        raise KeyError(f"{algo}: no model version {version} to activate")
    logger.info(f"{algo}: model v{version} is now active")


def load_active(session: Session, algo: str) -> ModelArtifact | None:
    """The serving model, or None if this algo has never activated one.

    None is a normal state, not an error: a deriver's first prediction cycle
    can legitimately run before its first training cycle. Callers report it as
    `no_model` rather than failing — see `app.algo.base.AlgoIntegration`.
    """
    from app.models.algo import AlgoModelVersion

    row = (
        session.query(AlgoModelVersion)
        .filter(AlgoModelVersion.algo == algo, AlgoModelVersion.is_active.is_(True))
        .order_by(AlgoModelVersion.version.desc())
        .first()
    )
    if row is None:
        return None
    return ModelArtifact(
        algo=row.algo,
        version=row.version,
        kind=row.kind,
        params=dict(row.params or {}),
        feature_names=tuple(row.feature_names or ()),
        metrics=dict(row.metrics or {}),
    )
