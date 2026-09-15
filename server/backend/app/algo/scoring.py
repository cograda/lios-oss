"""Grading predictions against what actually happened.

This module is the reason the harness exists. A solver can be reviewed by
reading it; a predictor cannot — its correctness is a claim about the world,
and the only way to know is to write down what it said and check later. An
algo nobody scores is an algo being trusted for no reason, which is the same
mistake as an unverified sanitiser: the check is cheap, its absence is
invisible, and the failure is silent.

Two things are computed, and the second is the one that matters:

  **Error** — MAE, RMSE and bias, per horizon. Absolute error answers "how
  wrong", bias answers "wrong in which direction", and separating them matters
  because a model that is 10% low every single time is trivially fixable while
  one that is 10% out in random directions is not.

  **Skill** — error relative to the baseline recorded at prediction time. A
  forecast is only worth running if it beats the dumb answer (yesterday's
  value, the seasonal mean), and the baseline is stored per-row precisely so it
  cannot be chosen afterwards to flatter the model. Negative skill means stop
  running the model, which is a conclusion a raw MAE will never hand you.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:  # pragma: no cover
    from app.models.algo import AlgoPrediction

logger = logging.getLogger(__name__)

# Model imports are LAZY throughout `app.algo` — see the note in
# `app/algo/artifacts.py`. A module-level `from app.models.algo import ...`
# makes `import app.algo` circular as soon as any deriver exists.

#: How long a prediction stays eligible for scoring before it is written off as
#: unobservable. Without a cap, every prediction whose target moment has no
#: recoverable ground truth (a sensor that was offline, a source backfill that
#: never came) is re-queried on every scoring pass forever — the same unbounded
#: retry shape as the reminders reaper that would have flooded EventKit with
#: 107k rows. A written-off row gets `scored_at` set with `actual` left NULL,
#: which is a distinguishable, queryable state rather than a silent drop.
ABANDON_AFTER_DAYS = 7


def score_predictions(
    session: Session,
    algo: str,
    observe,
    *,
    grace_min: int = 5,
    limit: int = 500,
    abandon_after_days: int = ABANDON_AFTER_DAYS,
) -> dict:
    """Fill in `actual`/`error`/`scored_at` for every scoreable prediction.

    `observe(session, quantity, at) -> float | None` is the deriver's own
    ground-truth reader. Returning None means "cannot see it yet", which is
    left unscored until the abandon cutoff — not treated as a zero, which
    would be an error of the full magnitude of the prediction and would drag
    every average with it.
    """
    from app.algo import predictions as pred_store

    rows = pred_store.due_for_scoring(session, algo, grace_min=grace_min, limit=limit)
    now = datetime.now(timezone.utc)
    abandon_cutoff = now - timedelta(days=abandon_after_days)

    scored = 0
    abandoned = 0
    unobserved = 0

    for row in rows:
        try:
            actual = observe(session, row.quantity, row.target_at)
        except Exception as exc:  # a broken observer must not abort the pass
            logger.warning(f"{algo}: observe() failed for {row.quantity}@{row.target_at}: {exc}")
            actual = None

        if actual is None:
            if row.target_at < abandon_cutoff:
                row.scored_at = now  # actual stays NULL — written off, not scored
                abandoned += 1
            else:
                unobserved += 1
            continue

        row.actual = float(actual)
        row.error = float(actual) - row.value
        row.scored_at = now
        scored += 1

    if abandoned:
        logger.info(
            f"{algo}: wrote off {abandoned} prediction(s) older than "
            f"{abandon_after_days}d with no observable ground truth"
        )
    return {
        "algo": algo,
        "candidates": len(rows),
        "scored": scored,
        "waiting": unobserved,
        "abandoned": abandoned,
    }


def metrics(
    session: Session,
    algo: str,
    *,
    quantity: str | None = None,
    days: int = 30,
) -> dict:
    """Error and skill over a recent window, overall and per horizon.

    Reads only rows with an `actual`, so written-off rows neither count as
    successes nor drag the averages down; `unscored` reports them separately
    so a collapsing coverage rate is visible rather than absorbed.
    """
    from app.models.algo import AlgoPrediction

    since = datetime.now(timezone.utc) - timedelta(days=days)
    q = session.query(AlgoPrediction).filter(
        AlgoPrediction.algo == algo, AlgoPrediction.target_at >= since
    )
    if quantity is not None:
        q = q.filter(AlgoPrediction.quantity == quantity)
    rows = q.all()

    graded = [r for r in rows if r.actual is not None]
    out: dict = {
        "algo": algo,
        "quantity": quantity,
        "window_days": days,
        "predictions": len(rows),
        "graded": len(graded),
        "unscored": len(rows) - len(graded),
    }
    if not graded:
        out["note"] = "nothing graded yet in this window"
        return out

    out.update(_error_stats(graded))
    out["by_horizon"] = {
        str(h): _error_stats([r for r in graded if r.horizon_min == h])
        for h in sorted({r.horizon_min for r in graded})
    }
    return out


def _error_stats(rows: "list[AlgoPrediction]") -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    errors = [abs(r.error) for r in rows if r.error is not None]
    signed = [r.error for r in rows if r.error is not None]
    mae = sum(errors) / len(errors) if errors else None
    rmse = (sum(e * e for e in errors) / len(errors)) ** 0.5 if errors else None
    bias = sum(signed) / len(signed) if signed else None

    stats: dict = {
        "n": n,
        "mae": round(mae, 4) if mae is not None else None,
        "rmse": round(rmse, 4) if rmse is not None else None,
        "bias": round(bias, 4) if bias is not None else None,
    }

    # Skill is only computed over rows that actually carry a baseline, and the
    # count is reported alongside it — a skill number over three of four
    # hundred rows is not a skill number, and silently averaging it against the
    # full set would hide that.
    paired = [r for r in rows if r.baseline is not None and r.actual is not None]
    if paired:
        model_mae = sum(abs(r.actual - r.value) for r in paired) / len(paired)
        base_mae = sum(abs(r.actual - r.baseline) for r in paired) / len(paired)
        stats["baseline_n"] = len(paired)
        stats["baseline_mae"] = round(base_mae, 4)
        stats["skill"] = round(1 - (model_mae / base_mae), 4) if base_mae > 0 else None
    return stats
