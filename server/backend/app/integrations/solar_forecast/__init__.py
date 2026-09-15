"""PV generation forecast — the first real tenant of the algo harness.

Predicts `pv_power` (the inverter's PV DC input, in W) one to twelve hours
ahead, and is graded hourly against what the panels actually produced.

**Why forecast something Home Assistant already forecasts.** Forecast.Solar is
already installed and publishing estimates. This is not a replacement for it —
it *consumes* it. Its recorded estimate is the strongest single feature here,
and the thing this deriver can learn that a generic model cannot is that
forecast's **local** bias: this roof's pitch, this azimuth, the tree line, the
inverter's clipping at 5.5 kW. A generic irradiance model knows the sky; it
does not know that the chimney shades string 2 until ten in the morning.

That framing also settles the honest question — is it worth running? The
`solar_accuracy` tool answers it against a real baseline, and if skill
is negative the answer is to leave `publish_entity` empty and stop.

**Why the incumbent forecast is a feature and not the baseline.** A baseline has
to be knowable at prediction time. Forecast.Solar's *curve* would be — it is a
forecast — but comar's HA cache stores only its scalar "now"/"next hour"
sensors, not the hourly series in its attributes. So at `made_at` we can read
what it believed about `made_at` and about `made_at + 1h`, and nothing about six
hours out. Those are legitimate features. The baseline is instead recent
climatology: the mean observed output at this hour of day over the trailing
fortnight, computable at any historical moment, and a much stronger bar for
solar than persistence (which at a six-hour horizon is nearly meaningless).

**⚠️ What this needs before it can do anything.** Its features come from
`ha_state_changes`, which keeps numeric history only for entities in
`homeassistant.ha_numeric_history_entities`. On a fresh deployment that list is
empty, so there is no history, `train()` reports `insufficient_rows`, and
`run_predict()` is a clean no-op. The setup order is in `README.md` — allowlist
the four entities, run `ha_backfill_history` to import HA's own recorder (~10
days), then train.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.algo import AlgoIntegration, AlgoSpec, Quantity
from app.integrations.solar_forecast.solar import solar_elevation_deg
from app.plugin.config_store import plugin_config

logger = logging.getLogger(__name__)

#: How far either side of a target moment counts as an observation of it. The
#: inverter reports every few seconds, so this is generous — but a gap (the
#: solar-gateway Pi rebooting, the inverter's AP dropping) should read as "not
#: observable" rather than silently attributing a reading half an hour away.
OBSERVE_WINDOW_MIN = 15

#: Trailing window for the climatology baseline.
CLIMATOLOGY_DAYS = 14


class SolarForecastIntegration(AlgoIntegration):
    SPEC = AlgoSpec(
        algo="solar_forecast",
        quantities=[
            Quantity(
                name="pv_power",
                unit="W",
                # Resolved from config — an entity id is household-specific.
                ha_entity=None,
                description="Forecast PV generation",
                round_to=0,
            )
        ],
        # One hour to twelve. Twelve because that is the span a household
        # decision actually covers ("run the dishwasher this afternoon?"), and
        # because beyond it this deriver has no features the incumbent forecast
        # doesn't already have — its day-level sensors are the only long-range
        # signal available.
        horizons=[60, 180, 360, 720],
        # observe() averages over target_at ± OBSERVE_WINDOW_MIN, so a row is
        # not scoreable until the forward half of that window has happened.
        score_grace_min=OBSERVE_WINDOW_MIN,
        estimator="ridge",
        # Otherwise the generated tools would be `solar_forecast_forecast` and
        # `solar_forecast_accuracy`.
        tool_prefix="solar",
        train_window_days=60,
        # Hourly. Denser adds near-duplicate rows from a signal that does not
        # move meaningfully in ten minutes, which inflates the row count without
        # adding information — and `min_train_rows` would then be satisfied by
        # data that isn't really there.
        train_stride_min=60,
        # Four horizons x daylight hours, so roughly a fortnight of real days.
        min_train_rows=300,
        holdout_fraction=0.2,
    )

    @property
    def name(self) -> str:
        return "solar_forecast"

    @property
    def display_name(self) -> str:
        return "Solar Forecast"

    # ---- the deriver contract ------------------------------------------

    def features(
        self, session: Session, made_at: datetime, target_at: datetime
    ) -> dict[str, float]:
        """What was knowable at `made_at`, plus the geometry of `target_at`.

        Every `recent_`/`incumbent_` value is read from history strictly
        *before* `made_at`, so this returns the same thing whether it is called
        for last March during training or for now during serving. Nothing here
        reads the target moment's own output — that is the leak that makes a
        model score beautifully offline and predict nothing live.
        """
        cfg = plugin_config(self.name)
        recent = self._history(session, cfg.pv_power_entity, made_at - timedelta(hours=2), made_at)
        last = recent[-1] if recent else 0.0

        feats: dict[str, float] = {
            "recent_pv_w": last,
            "recent_pv_mean_2h": (sum(recent) / len(recent)) if recent else last,
            "recent_pv_max_2h": max(recent) if recent else last,
            # Sun geometry at the target moment. This is the physics the model
            # does not have to learn: elevation carries hour-of-day and season
            # together and is exactly proportional to the air mass the light
            # travels through, which a raw hour number is not.
            "target_elevation_deg": self._elevation(target_at),
            "target_elevation_sin": math.sin(math.radians(max(0.0, self._elevation(target_at)))),
            # Elevation now, so the model can read "how much of the day is
            # left" without being told the hour.
            "made_elevation_deg": self._elevation(made_at),
            "horizon_min": (target_at - made_at).total_seconds() / 60.0,
        }

        # The incumbent forecast's own beliefs at made_at. Each is optional:
        # a household without Forecast.Solar installed still gets a working
        # (if weaker) model rather than a crash, and a missing entity yields a
        # column of zeros rather than a missing column — the feature set has to
        # stay the same shape across every training row and every serve.
        for key, entity in (
            ("incumbent_now_w", cfg.incumbent_power_entity),
            ("incumbent_next_hour_kwh", cfg.incumbent_next_hour_entity),
            ("incumbent_remaining_kwh", cfg.incumbent_remaining_today_entity),
        ):
            feats[key] = self._latest_before(session, entity, made_at)

        return feats

    def observe(self, session: Session, quantity: str, at: datetime) -> float | None:
        """Recorded PV power nearest `at`, or None if history doesn't reach it.

        None rather than 0.0 — a zero here is an error the full size of the
        prediction, and on a sunny afternoon it would be a 4 kW error attributed
        to the model rather than to a gap in the data.
        """
        entity = (plugin_config(self.name).pv_power_entity or "").strip()
        if not entity:
            return None
        window = self._history(
            session,
            entity,
            at - timedelta(minutes=OBSERVE_WINDOW_MIN),
            at + timedelta(minutes=OBSERVE_WINDOW_MIN),
        )
        if not window:
            return None
        return sum(window) / len(window)

    def baseline(
        self, session: Session, quantity: str, made_at: datetime, target_at: datetime
    ) -> float | None:
        """Recent climatology: mean output at this hour of day over the trailing
        fortnight, computed from history strictly before `made_at`.

        Persistence — `AlgoIntegration`'s default — is the wrong bar for solar.
        "As much as right now, in six hours' time" is nearly meaningless, so
        beating it would prove nothing. This is what a forecast actually has to
        beat: knowing the time of year and the time of day and nothing about
        the weather.
        """
        entity = (plugin_config(self.name).pv_power_entity or "").strip()
        if not entity:
            return None
        rows = self._history_rows(
            session, entity, made_at - timedelta(days=CLIMATOLOGY_DAYS), made_at
        )
        same_hour = [v for at, v in rows if at.hour == target_at.hour]
        if not same_hour:
            return None
        return sum(same_hour) / len(same_hour)

    def predict(self, session, quantity, made_at, target_at):
        """Predict, but skip the dark and never return negative generation.

        Two guards, both about honesty rather than correctness:

        - **Below the elevation floor, return None.** Nights are trivially zero
          on both sides, so including them makes MAE look excellent and skill
          look like nothing — the metrics stop meaning anything. Skipping them
          means `solar_accuracy` describes the hours that matter.
        - **Clamp at zero.** A linear model extrapolating past dawn will happily
          predict −300 W, which is not a small error but a physically impossible
          answer, and publishing one to HA would be worse than publishing
          nothing.
        """
        cfg = plugin_config(self.name)
        if self._elevation(target_at) < float(cfg.min_solar_elevation_deg or 0):
            return None
        result = super().predict(session, quantity, made_at, target_at)
        if result is None:
            return None
        value, feature_hash, version = result
        return max(0.0, value), feature_hash, version

    def ha_entity_for(self, quantity: str) -> str | None:
        return (plugin_config(self.name).publish_entity or "").strip() or None

    def training_pairs(self, session: Session, horizon_min: int):
        """The default grid, minus target moments the sun is not up for.

        Filtered here as well as in `predict()` so the fit is not diluted by
        thousands of trivially-zero night rows. A model fitted mostly on nights
        learns mostly about nights, and its coefficients on the daylight
        features get correspondingly weaker — this is not only about the
        metrics.
        """
        floor = float(plugin_config(self.name).min_solar_elevation_deg or 0)
        for made_at, target_at in super().training_pairs(session, horizon_min):
            if self._elevation(target_at) >= floor:
                yield made_at, target_at

    # ---- helpers -------------------------------------------------------

    def _elevation(self, at: datetime) -> float:
        """Sun elevation, using the coordinates weather is already configured
        with. Reading another integration's config via `plugin_config()` is the
        pattern `commute` uses for `ha_url`/`ha_token`; a second copy of the
        house's latitude would just be a second thing to get wrong."""
        cfg = plugin_config("weather")
        lat = float(cfg.weather_latitude or 0.0)
        lon = float(cfg.weather_longitude or 0.0)
        if not lat and not lon:
            # Unconfigured coordinates would put the house in the Gulf of
            # Guinea and shift the day by hours. Report the sun as always up so
            # the elevation filter degrades to "no filter" rather than to
            # "predict nothing, ever, silently".
            return 90.0
        return solar_elevation_deg(at, lat, lon)

    def _history_rows(
        self, session: Session, entity: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        if not (entity or "").strip():
            return []
        from app.plugin.capabilities import get_capability

        ha = get_capability("homeassistant.entities")
        return ha.numeric_history(session, entity.strip(), start, end)

    def _history(self, session, entity, start, end) -> list[float]:
        return [v for _at, v in self._history_rows(session, entity, start, end)]

    def _latest_before(self, session: Session, entity: str, at: datetime) -> float:
        """Last recorded numeric value strictly before `at`, or 0.0.

        A six-hour lookback rather than unbounded: an entity that stopped
        reporting a week ago should read as absent, not contribute a week-old
        number that the model will treat as current.
        """
        rows = self._history_rows(session, entity, at - timedelta(hours=6), at)
        return rows[-1][1] if rows else 0.0

    async def dashboard_data(self) -> dict[str, Any]:
        data = await super().dashboard_data()
        cfg = plugin_config(self.name)
        # Surfaced because "no predictions" has two very different causes and
        # the dashboard should not make you guess which: no model yet, or no
        # history to have fitted one from.
        data["source_entity"] = (cfg.pv_power_entity or "") or None
        data["publishing_to"] = self.ha_entity_for("pv_power")
        return data
