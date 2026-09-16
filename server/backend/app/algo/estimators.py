"""Estimators that fit with scikit-learn and serve from JSON.

Two halves, deliberately asymmetric:

  `fit()`   may import scikit-learn, take its time, and be run by hand or on a
            weekly cron. It returns a plain dict of JSON-serialisable numbers.
  `predict()` may import numpy and nothing else. It reads that dict.

That asymmetry is the whole design. Serving never touches scikit-learn, so a
scikit-learn upgrade cannot change a live prediction, and a model is a row of
readable numbers rather than an opaque blob bound to the library version that
wrote it. This repo already caps every dependency's major *because* an
unpinned bump broke the tool surface once (requirements.txt says so at
length); a pickled estimator would convert that class of accident from a loud
import error into a silently wrong forecast.

The cost of the rule is honest: only models whose parameters are a handful of
numbers can ship. In practice that is linear/ridge regression and bucketed
means, which is most of what a household predictive layer needs — hour-of-day
and weather-driven signals are close to linear once bucketed. A deriver that
genuinely needs a gradient-boosted forest should add its own estimator here
with a real JSON serialisation of the trees, or make the case for relaxing the
rule. It should not reach for a pickle quietly.

`register_estimator()` is the extension point: a deriver can supply its own
estimator from its own package without editing this file.
"""

from __future__ import annotations

from typing import Callable, Protocol

Params = dict


class Estimator(Protocol):
    """`fit` returns JSON-serialisable params; `predict` consumes them."""

    kind: str

    def fit(self, X: list[list[float]], y: list[float], names: list[str]) -> Params: ...

    def predict(self, params: Params, values: list[float], names: list[str]) -> float: ...


class MeanEstimator:
    """Predicts the training mean, ignoring every feature.

    Not a placeholder — it is the floor any real model has to clear, and having
    it as a first-class estimator means "our forecaster beats the mean" is a
    measured claim rather than an assumption. A deriver whose ridge fit does not
    beat this on holdout should ship this instead and say so.
    """

    kind = "mean"

    def fit(self, X, y, names) -> Params:
        n = len(y)
        mean = sum(y) / n if n else 0.0
        return {"mean": float(mean), "n": n}

    def predict(self, params, values, names) -> float:
        return float(params.get("mean", 0.0))


class RidgeEstimator:
    """Standardised ridge regression. Coefficients out, coefficients in.

    Standardisation is folded into the params (per-column mean and scale)
    rather than left to a separate sklearn pipeline, so `predict()` needs
    nothing but arithmetic. Ridge rather than plain least squares because
    household features are strongly collinear — temperature, irradiance and
    hour-of-day all move together — and unregularised coefficients on
    collinear inputs swing wildly between refits, which reads as a broken
    model even when predictions are fine.
    """

    kind = "ridge"

    def __init__(self, alpha: float = 1.0):
        self.alpha = alpha

    def fit(self, X, y, names) -> Params:
        import numpy as np
        from sklearn.linear_model import Ridge

        Xa = np.asarray(X, dtype=float)
        ya = np.asarray(y, dtype=float)
        mean = Xa.mean(axis=0)
        scale = Xa.std(axis=0)
        # A constant column has zero variance; dividing by it yields inf/NaN
        # coefficients that then produce NaN predictions forever. Treat it as
        # scale 1 — the column simply contributes nothing after centring.
        scale = np.where(scale < 1e-12, 1.0, scale)
        Z = (Xa - mean) / scale

        model = Ridge(alpha=self.alpha)
        model.fit(Z, ya)
        return {
            "alpha": float(self.alpha),
            "mean": [float(v) for v in mean],
            "scale": [float(v) for v in scale],
            "coef": [float(v) for v in model.coef_],
            "intercept": float(model.intercept_),
        }

    def predict(self, params, values, names) -> float:
        import numpy as np

        v = np.asarray(values, dtype=float)
        mean = np.asarray(params["mean"], dtype=float)
        scale = np.asarray(params["scale"], dtype=float)
        coef = np.asarray(params["coef"], dtype=float)
        if not (len(v) == len(mean) == len(scale) == len(coef)):
            raise ValueError(
                f"ridge params are for {len(coef)} features, got {len(v)} — "
                "the model version and the feature set disagree; refit."
            )
        z = (v - mean) / scale
        return float(z @ coef + params["intercept"])


class BucketMeanEstimator:
    """Mean of the target within each bucket of one chosen feature.

    The cheapest way to capture a strong non-linearity without leaving JSON:
    generation by hour of day, delay by day of week, consumption by outside
    temperature band. Buckets are integer `floor(value / width)` keys, and an
    unseen bucket falls back to the global mean rather than raising — a bucket
    with no history is a normal condition (the first cold snap of the year),
    not an error, and a fallback that is visibly the global mean is easier to
    reason about than a refusal.
    """

    kind = "bucket_mean"

    def __init__(self, feature: str, width: float = 1.0):
        self.feature = feature
        self.width = width

    def fit(self, X, y, names) -> Params:
        if self.feature not in names:
            raise ValueError(
                f"bucket_mean is keyed on feature {self.feature!r}, which is "
                f"not in the feature set {sorted(names)}"
            )
        idx = names.index(self.feature)
        sums: dict[str, float] = {}
        counts: dict[str, int] = {}
        for row, target in zip(X, y):
            key = str(int(row[idx] // self.width))
            sums[key] = sums.get(key, 0.0) + float(target)
            counts[key] = counts.get(key, 0) + 1
        return {
            "feature": self.feature,
            "width": float(self.width),
            "buckets": {k: sums[k] / counts[k] for k in sums},
            "counts": counts,
            "global_mean": (sum(y) / len(y)) if y else 0.0,
        }

    def predict(self, params, values, names) -> float:
        feature = params["feature"]
        if feature not in names:
            raise ValueError(f"bucket_mean feature {feature!r} missing at serve time")
        key = str(int(values[names.index(feature)] // params["width"]))
        return float(params["buckets"].get(key, params["global_mean"]))


#: kind -> factory. A factory (not an instance) because estimators with
#: hyperparameters — the alpha on ridge, the bucket width — are configured per
#: deriver, and a shared mutable instance would let one algo's tuning leak
#: into another's.
ESTIMATORS: dict[str, Callable[..., Estimator]] = {
    MeanEstimator.kind: MeanEstimator,
    RidgeEstimator.kind: RidgeEstimator,
    BucketMeanEstimator.kind: BucketMeanEstimator,
}


def register_estimator(kind: str, factory: Callable[..., Estimator]) -> None:
    """Add an estimator from a deriver's own package.

    Refuses to overwrite an existing kind: a model version stores only the
    `kind` string, so silently rebinding a name would make every stored
    artifact under that name deserialise against different maths.
    """
    if kind in ESTIMATORS and ESTIMATORS[kind] is not factory:
        raise ValueError(
            f"estimator kind {kind!r} is already registered — stored model "
            f"versions reference kinds by name, so rebinding one changes how "
            f"existing artifacts are interpreted. Pick another name."
        )
    ESTIMATORS[kind] = factory


def build(kind: str, **kwargs) -> Estimator:
    if kind not in ESTIMATORS:
        raise KeyError(f"unknown estimator {kind!r} — known: {sorted(ESTIMATORS)}")
    return ESTIMATORS[kind](**kwargs)
