"""What a deriver declares about itself.

An `AlgoSpec` is to a deriver what `IntegrationManifest` is to an integration:
one literal, read by the harness, describing the shape of the thing rather
than its behaviour. It is deliberately separate from the manifest — the
manifest is the *kernel's* contract (models, schedule, config, capabilities)
and every integration has one; this is the *harness's* contract, and only
derivers have one.

The field that does the most work is `horizons`. A predictor without declared
horizons cannot be scored honestly: "the forecast was 8% out" means nothing
without "…at six hours' notice", and an algo quietly reporting its one-hour
skill as its headline number is the easiest way to look better than it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Quantity:
    """One thing a deriver predicts.

    `ha_entity`, when set, is the Home Assistant entity the latest value is
    published to. That is the whole HA integration story for a deriver: comar
    computes, HA displays and automates. There is no second execution
    environment — `hardware/docs/hardening-2026-08.md` settled that the seam
    between the device fleet and comar *is* Home Assistant, and a deriver
    writing a sensor is that seam being used as intended rather than a new
    coupling.

    `higher_is_better` is unset for most quantities and only matters to
    presentation (a forecast of generation reads differently from a forecast
    of a delay); nothing in scoring depends on it.
    """

    name: str
    unit: str | None = None
    ha_entity: str | None = None
    description: str = ""
    higher_is_better: bool | None = None
    #: Rounding applied before publishing/storing. Keeps a float32 model from
    #: implying six significant figures of confidence.
    round_to: int = 2


@dataclass(frozen=True)
class AlgoSpec:
    """The declaration a deriver's class carries as `SPEC`."""

    #: Must equal the integration package name, same rule as `MANIFEST.name`.
    #: `app.algo.base` checks it, so a copy-pasted spec fails loudly at boot
    #: rather than writing predictions under the wrong algo name.
    algo: str
    quantities: list[Quantity]

    #: Minutes ahead this algo predicts, ascending. Every prediction cycle
    #: emits one row per (quantity, horizon).
    horizons: list[int]

    #: Estimator kind from `app.algo.estimators.ESTIMATORS`. `None` means the
    #: deriver overrides `predict()` itself and never fits a model — the right
    #: answer for a pure solver (a timetable chain, a constraint solve) that
    #: still wants its output scored.
    estimator: str | None = None

    #: How far back training draws labelled examples.
    train_window_days: int = 90

    #: Cadence, in minutes, of the historical (made_at, target_at) grid the
    #: default `training_pairs()` walks. Denser is not automatically better:
    #: adjacent examples from a slow-moving signal are near-duplicates that
    #: inflate the row count without adding information.
    train_stride_min: int = 60

    #: Minimum labelled rows before a fit is allowed to become active. A model
    #: fitted on eleven examples will still produce confident numbers, which is
    #: exactly why this has a floor rather than a warning.
    min_train_rows: int = 200

    #: Fraction of the most recent examples held out of the fit. Held out by
    #: *time*, never at random: a random split lets a model see the future of
    #: its own validation rows and score itself far too well.
    holdout_fraction: float = 0.2

    #: Minutes after `target_at` before a prediction may be scored. Must be
    #: at least as wide as the forward half of the deriver's `observe()`
    #: window: solar_forecast averages PV over target_at ± 15 min, and with
    #: the old fixed 5-minute grace a row scored between +5 and +15 saw a
    #: half-window mean — a plausible wattage, not the truth — and the
    #: correctness of every grade rested on two cron expressions happening
    #: to sit more than 15 minutes apart. Declared here, next to `baseline`,
    #: because it is a fact about *this deriver's* observe(), and the next
    #: deriver meets the question at the point where it writes one.
    score_grace_min: int = 5

    llm_model: str | None = None
    tags: list[str] = field(default_factory=list)

    #: Prefix for the two generated MCP tools (`<prefix>_forecast`,
    #: `<prefix>_accuracy`). Defaults to `algo`, which is right for most
    #: derivers — but an algo whose own name ends in the word "forecast" would
    #: otherwise get `solar_forecast_forecast`. A tool name is public API, so
    #: it is worth a field rather than a rename later.
    tool_prefix: str | None = None

    def quantity(self, name: str) -> Quantity:
        for q in self.quantities:
            if q.name == name:
                return q
        raise KeyError(f"{self.algo}: no declared quantity {name!r}")

    @property
    def tools_named(self) -> str:
        return self.tool_prefix or self.algo

    @property
    def quantity_names(self) -> list[str]:
        return [q.name for q in self.quantities]
