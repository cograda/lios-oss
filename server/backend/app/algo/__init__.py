"""The algo harness — a shared environment for predictive and algorithmic work.

Peer of `app/tools/` (the declarative MCP tool DSL): kernel infrastructure that
integrations import, not an integration itself. Where `app/tools/` answers "how
does an integration expose a tool", this answers "how does something that
*computes* an answer get run, recorded, published and graded".

    from app.algo import AlgoIntegration, AlgoSpec, Quantity

    class SolarForecast(AlgoIntegration):
        SPEC = AlgoSpec(
            algo="solar_forecast",
            quantities=[Quantity("pv_power", unit="W", ha_entity="sensor.solar_forecast_pv")],
            horizons=[60, 180, 360, 720],
            estimator="ridge",
        )
        def features(self, session, made_at, target_at): ...
        def observe(self, session, quantity, at): ...

Why a shared environment rather than each algo doing its own thing: the
commute solver and its ancestor in `hardware/homeassistant/commute/` are what
happens without one — the same solver forked into two runtimes, two feed
adapters and two config sets, and the fork went stale without anything
complaining. The parts every algo needs are identical and none of them are the
interesting part of an algo:

  input      one `features()` used by both training and serving, so train/serve
             skew has no seam to open in (`features.py`)
  models     fitted parameters as JSON, versioned, activated deliberately
             (`estimators.py`, `artifacts.py`)
  output     Postgres rows, a Home Assistant sensor, two MCP tools —
             all three from one declaration (`predictions.py`, `sinks.py`)
  judgement  an LLM call whose cost lands in one ledger (`llm.py`)
  proof      predictions graded against what happened, with skill measured
             against a baseline recorded up front (`scoring.py`)

That last one is the reason to build this rather than write each algo by hand.
An algo nobody scores is an algo being trusted for no reason.
"""

from app.algo.base import AlgoIntegration, train_algo
from app.algo.estimators import ESTIMATORS, build, register_estimator
from app.algo.features import FeatureMismatch, FeatureVector
from app.algo.llm import AlgoLLM, LLMUnavailable
from app.algo.spec import AlgoSpec, Quantity

__all__ = [
    "AlgoIntegration",
    "AlgoLLM",
    "AlgoSpec",
    "ESTIMATORS",
    "FeatureMismatch",
    "FeatureVector",
    "LLMUnavailable",
    "Quantity",
    "build",
    "register_estimator",
    "train_algo",
]
