"""Recording and reading predictions.

One function does the writing (`record`) and it upserts on
`(algo, quantity, target_at, horizon_min)`. That grain is the reason the table
is useful: many rows for the same `target_at` at different horizons is the
signal — it is what makes "how much better are we one hour out than six?"
answerable — while a retry, a manual trigger or an APScheduler misfire
catch-up re-running the same cycle must not stack duplicates at the same
horizon.

Same idempotency shape as financier's content-addressed transaction ids, for
the same reason: the cheapest way to make re-ingestion safe is to make the
natural key of the thing be the key of the row.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

if TYPE_CHECKING:  # pragma: no cover
    from app.models.algo import AlgoPrediction

logger = logging.getLogger(__name__)

# Model imports are LAZY throughout `app.algo` — inside functions, never at
# module scope. `app/models/__init__.py` runs `discover_integration_models()`
# at import time, which imports every integration package, which imports
# `app.algo` — so a module-level `from app.models.algo import ...` here makes
# `import app.algo` circular the moment any deriver exists. It did: adding
# `solar_forecast` broke collection of the whole test suite. Same rule
# `app/plugin/kernel_jobs.py` already follows.


def record(
    session: Session,
    *,
    algo: str,
    quantity: str,
    made_at: datetime,
    target_at: datetime,
    value: float,
    unit: str | None = None,
    lower: float | None = None,
    upper: float | None = None,
    algo_version: int | None = None,
    features_hash: str | None = None,
    run_id: int | None = None,
    baseline: float | None = None,
) -> None:
    """Upsert one prediction.

    `horizon_min` is derived here rather than passed in, so it can never
    disagree with the two timestamps it is supposed to summarise — a
    hand-passed horizon that drifts from `target_at - made_at` would silently
    bucket predictions under the wrong horizon and corrupt every skill-by-
    horizon number computed afterwards.

    On conflict, the new value wins but `actual`/`error`/`scored_at` are left
    alone: a re-run of a cycle whose target has already been scored must not
    erase the observation. In practice that combination is rare, and quietly
    discarding the ground truth would be much worse than a slightly stale
    prediction sitting next to it.
    """
    from app.models.algo import AlgoPrediction

    horizon_min = int(round((target_at - made_at).total_seconds() / 60))
    stmt = (
        pg_insert(AlgoPrediction)
        .values(
            algo=algo,
            quantity=quantity,
            made_at=made_at,
            target_at=target_at,
            horizon_min=horizon_min,
            value=float(value),
            unit=unit,
            lower=lower,
            upper=upper,
            algo_version=algo_version,
            features_hash=features_hash,
            run_id=run_id,
            baseline=baseline,
        )
        .on_conflict_do_update(
            constraint="uq_algo_predictions_target",
            set_={
                "made_at": made_at,
                "value": float(value),
                "lower": lower,
                "upper": upper,
                "algo_version": algo_version,
                "features_hash": features_hash,
                "run_id": run_id,
                "baseline": baseline,
            },
        )
    )
    session.execute(stmt)


def latest(
    session: Session, algo: str, quantity: str, *, horizon_min: int | None = None
) -> "AlgoPrediction | None":
    """Most recently made prediction for a quantity (optionally one horizon)."""
    from app.models.algo import AlgoPrediction

    q = session.query(AlgoPrediction).filter(
        AlgoPrediction.algo == algo, AlgoPrediction.quantity == quantity
    )
    if horizon_min is not None:
        q = q.filter(AlgoPrediction.horizon_min == horizon_min)
    return q.order_by(AlgoPrediction.made_at.desc(), AlgoPrediction.target_at.asc()).first()


def curve(
    session: Session, algo: str, quantity: str, *, made_after: datetime | None = None
) -> list["AlgoPrediction"]:
    """The current forecast series: the newest prediction for each target moment.

    Reads the latest cycle's rows — a curve assembled from several cycles would
    be a mix of one-hour-old and six-hour-old claims presented as one line,
    which is exactly the kind of thing that makes a forecast look better than
    it is. Ordered by target time, ready to publish.
    """
    from app.models.algo import AlgoPrediction

    newest = (
        session.query(AlgoPrediction.made_at)
        .filter(AlgoPrediction.algo == algo, AlgoPrediction.quantity == quantity)
        .order_by(AlgoPrediction.made_at.desc())
        .limit(1)
        .scalar()
    )
    if newest is None:
        return []
    if made_after is not None and newest < made_after:
        return []
    return (
        session.query(AlgoPrediction)
        .filter(
            AlgoPrediction.algo == algo,
            AlgoPrediction.quantity == quantity,
            AlgoPrediction.made_at == newest,
        )
        .order_by(AlgoPrediction.target_at.asc())
        .all()
    )


def due_for_scoring(
    session: Session, algo: str, *, grace_min: int = 5, limit: int = 500
) -> list["AlgoPrediction"]:
    """Unscored predictions whose target moment has passed.

    `grace_min` keeps the scorer off the edge: a target moment one second in
    the past is not yet observable in a system whose sources poll on their own
    cadence, and asking too early gets a NULL that would be indistinguishable
    from "we can never observe this".
    """
    from app.models.algo import AlgoPrediction

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=grace_min)
    return (
        session.query(AlgoPrediction)
        .filter(
            AlgoPrediction.algo == algo,
            AlgoPrediction.scored_at.is_(None),
            AlgoPrediction.target_at <= cutoff,
        )
        .order_by(AlgoPrediction.target_at.asc())
        .limit(limit)
        .all()
    )
