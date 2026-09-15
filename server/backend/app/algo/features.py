"""Feature vectors, and the one rule that matters about them.

**Training and serving must read features through the same function.** Not the
same logic, restated in two places — the same call. Train/serve skew is the
characteristic way a predictive system fails: the fit sees a feature computed
one way, the live path computes it very slightly differently, every offline
metric looks fine, and the live predictions are quietly wrong. Nothing
detects it, because both halves are individually correct.

The defence here is structural rather than documentary: `AlgoIntegration`
declares one abstract `features(session, made_at, target_at)`, and the harness
calls it from both `run_predict()` and `train()`. There is no second entry
point to drift from. `tests/test_algo_harness.py` asserts that property by
counting calls through a spy, so removing it fails a test rather than a
review.

`FeatureVector` exists to make the ordering explicit. A fitted coefficient
array is meaningless without the column order that produced it, so the order
is stored on the model version and re-imposed here at serve time — a feature
added to `features()` after a fit raises rather than silently shifting every
coefficient one column to the left.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass


class FeatureMismatch(RuntimeError):
    """The live feature dict does not match the fitted model's columns.

    Raised rather than tolerated. The tempting alternative — fill missing
    columns with zero — produces a number that looks like a prediction and
    is not one.
    """


@dataclass(frozen=True)
class FeatureVector:
    names: tuple[str, ...]
    values: tuple[float, ...]

    @classmethod
    def from_dict(cls, features: dict[str, float]) -> "FeatureVector":
        """Deterministic ordering (sorted by name) for a fresh fit."""
        names = tuple(sorted(features))
        return cls(names=names, values=tuple(float(features[n]) for n in names))

    @classmethod
    def aligned(cls, features: dict[str, float], names: list[str] | tuple[str, ...]) -> "FeatureVector":
        """Impose a fitted model's column order on a live feature dict."""
        missing = [n for n in names if n not in features]
        extra = [n for n in features if n not in names]
        if missing or extra:
            raise FeatureMismatch(
                f"feature drift since this model was fitted: "
                f"missing={missing or '-'} unexpected={extra or '-'}. "
                f"Refit before serving — do not pad with zeros."
            )
        return cls(names=tuple(names), values=tuple(float(features[n]) for n in names))

    def validate_finite(self) -> None:
        """Reject NaN/inf at the boundary.

        A NaN feature propagates to a NaN prediction, which SQLAlchemy will
        happily store as a float and which then poisons every mean error the
        scoring pass computes over that window. Fail at the one place the bad
        value enters.
        """
        bad = [n for n, v in zip(self.names, self.values) if not math.isfinite(v)]
        if bad:
            raise FeatureMismatch(f"non-finite feature values: {bad}")

    @property
    def hash(self) -> str:
        """Stable digest of this exact input, stored on the prediction row.

        Values are rounded to 6 places first so that float noise from an
        unrelated library upgrade does not make yesterday's identical input
        look like a different one.
        """
        payload = json.dumps(
            {n: round(v, 6) for n, v in zip(self.names, self.values)},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def as_dict(self) -> dict[str, float]:
        return dict(zip(self.names, self.values))
