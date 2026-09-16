"""The `_algo_template` deriver — a worked example, and the scaffold to copy.

It forecasts one numeric Home Assistant sensor from its own recent history:
plausible enough to be instructive (this is the shape of a room-temperature or
generation forecast) and small enough to read in one sitting.

Four methods carry all of it. Everything else — the prediction cycle, the
training loop, the holdout split, artifact versioning, the HA push, two MCP
tools, the run ledger and scoring — is inherited from `AlgoIntegration`.

⚠️ **The trap this example exists to warn about.** `features()` is called with
a *historical* `made_at` during training and with *now* during serving. It must
return what would have been known at `made_at` — so it may only read tables
that keep history. `ha_entities` holds the **latest** state per entity and
nothing else, so reading it here would make every training row see today's
value while claiming to be last March, and the model would score beautifully
and predict nothing. `ha_state_changes` is the append-only table, so that is
what this reads.

⚠️ And the gotcha underneath that: `ha_state_changes` records numeric→numeric
transitions **only if `homeassistant.ha_record_numeric_history` is true**, and
it defaults to false. A numeric deriver on a fresh deployment therefore has no
history to train on at all — `train()` reports `insufficient_rows` rather than
fitting, which is the right outcome but is easy to misread as a bug. Turn the
flag on and wait for history to accumulate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.algo import AlgoIntegration, AlgoSpec, Quantity
from app.plugin.config_store import plugin_config

logger = logging.getLogger(__name__)


class AlgoTemplateIntegration(AlgoIntegration):
    SPEC = AlgoSpec(
        algo="__ALGO_NAME__",
        quantities=[
            Quantity(
                name="value",
                unit=None,
                # Resolved from config at push time instead — see
                # `ha_entity_for()`. A hardcoded entity_id here would be
                # exactly the household-specific value the personalisation
                # guard test sweeps for.
                ha_entity=None,
                description="Forecast value",
                round_to=2,
            )
        ],
        # One hour to twelve. The set is declared rather than implied because
        # accuracy is meaningless without it: "we were 0.4 out" needs "…at
        # twelve hours' notice" to mean anything.
        horizons=[60, 180, 360, 720],
        estimator="ridge",
        train_window_days=60,
        train_stride_min=60,
        min_train_rows=200,
    )

    @property
    def name(self) -> str:
        return "__ALGO_NAME__"

    @property
    def display_name(self) -> str:
        return "Algo Template"

    # ---- the four methods a deriver writes -----------------------------

    def features(self, session: Session, made_at: datetime, target_at: datetime) -> dict[str, float]:
        """What was knowable at `made_at`, plus what is claimed about `target_at`.

        Deliberately mixed, and deliberately labelled: `recent_*` are readings
        from before `made_at`, `target_*` describe the moment being predicted.
        The one thing that must never appear here is the target moment's own
        observed value — that is the leak that makes a model look perfect
        offline and useless live.
        """
        recent = self._history(session, made_at - timedelta(hours=6), made_at)
        last = recent[-1] if recent else 0.0
        return {
            "recent_last": last,
            "recent_mean_6h": (sum(recent) / len(recent)) if recent else last,
            "recent_delta_6h": (last - recent[0]) if len(recent) > 1 else 0.0,
            # Hour of day as sin/cos rather than 0-23: a raw hour tells a
            # linear model that 23:00 and 00:00 are as far apart as possible,
            # when they are adjacent. Two smooth features fix that without
            # needing a non-linear estimator.
            "target_hour_sin": _sin_hour(target_at),
            "target_hour_cos": _cos_hour(target_at),
            "target_is_weekend": 1.0 if target_at.weekday() >= 5 else 0.0,
            # Horizon as a feature is what lets one model serve every horizon:
            # it can learn that a twelve-hour claim should lean harder on the
            # daily cycle and less on the last reading.
            "horizon_min": float((target_at - made_at).total_seconds() / 60.0),
        }

    def observe(self, session: Session, quantity: str, at: datetime) -> float | None:
        """The recorded value nearest `at`, or None if history doesn't reach it.

        None, not 0.0. A zero here would be an error the full size of the
        prediction and would drag every average that touched it.
        """
        window = self._history(session, at - timedelta(minutes=30), at + timedelta(minutes=30))
        return window[-1] if window else None

    # `baseline()` is not overridden: the default is persistence (the last
    # observable value), which is the honest bar for a signal like this. A
    # forecast that cannot beat "same as it is now" is a forecast to switch off.

    def extra_mcp_tools(self) -> list[dict[str, Any]]:
        """`<algo>_forecast` and `<algo>_accuracy` come for free. Add more here
        only if this deriver has something to say that a forecast and a track
        record do not cover."""
        return []

    # ---- helpers -------------------------------------------------------

    def ha_entity_for(self, quantity: str) -> str | None:
        return (plugin_config(self.name).publish_entity or "").strip() or None

    def _history(self, session: Session, start: datetime, end: datetime) -> list[float]:
        """Numeric readings for the configured source entity, in time order.

        Read through the `homeassistant.entities` capability rather than by
        importing HA's models: `tests/test_capability_boundaries.py` enforces
        that `<pkg>.facade` is the only thing one integration may import from
        another, and it caught this file doing the raw import while this
        template was being written. The facade method it uses
        (`numeric_history`) documents the two traps — the
        `ha_record_numeric_history` flag, and why `ha_entities` is the wrong
        table for a feature builder.
        """
        from app.plugin.capabilities import get_capability

        entity = (plugin_config(self.name).source_entity or "").strip()
        if not entity:
            return []
        ha = get_capability("homeassistant.entities")
        return [value for _at, value in ha.numeric_history(session, entity, start, end)]


def _sin_hour(dt: datetime) -> float:
    import math

    return math.sin(2 * math.pi * (dt.hour + dt.minute / 60) / 24)


def _cos_hour(dt: datetime) -> float:
    import math

    return math.cos(2 * math.pi * (dt.hour + dt.minute / 60) / 24)
