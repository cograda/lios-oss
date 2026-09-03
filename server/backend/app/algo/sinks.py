"""The two output surfaces every deriver gets for free: Home Assistant and MCP.

The question this answers is "who consumes an algo". Two consumers, and they
want different shapes:

  **Home Assistant** wants entities. An automation wants a scalar it can put a
  numeric threshold on; a dashboard wants the whole curve. So a quantity
  publishes as one `sensor.<algo>_<quantity>` whose *state* is the next
  forecast value and whose *attributes* carry the series. That is what HA's own
  solar-forecast integrations do, and it means one entity serves both without a
  second concept. Comar computes, HA displays and automates — the seam
  `hardware/docs/hardening-2026-08.md` settled on, used as intended.

  **Claude / the comar app** wants a tool. Two are generated per deriver from
  its `AlgoSpec`: `<algo>_forecast` (what do we currently expect) and
  `<algo>_accuracy` (how well has this been doing). The second exists because a
  forecast presented without its track record invites more confidence than it
  has earned, and because "is this model still any good" should be answerable
  in the same place the forecast is read rather than by going to the database.

The HA push is best-effort throughout — never let a failed sensor write stop a
prediction from being committed to Postgres. That ordering is deliberate and
copied from commute: the durable record is the point, the sensor is a
projection of it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.algo import predictions as pred_store
from app.algo import scoring
from app.algo.spec import AlgoSpec
from app.tools import CustomTool, ToolAnnotations

logger = logging.getLogger(__name__)

#: Cap on how many forecast points ride along in HA attributes. HA stores the
#: full attribute blob in its recorder database on *every* state change, so an
#: unbounded series is a slow, invisible way to grow that database — the same
#: class of mistake as keeping 105k timelapse JPEGs in restic. 48 points covers
#: two days hourly, which is more than any dashboard reads.
MAX_HA_SERIES_POINTS = 48


def publish_to_ha(
    session: Session, spec: AlgoSpec, resolve_entity=None
) -> tuple[int, list[str]]:
    """Publish each quantity's current forecast to its Home Assistant entity.

    Returns `(published, failed_entities)`. Quantities that resolve to no
    entity are skipped silently — publishing is opt-in per quantity, because an
    intermediate quantity a deriver predicts for its own use has no business
    creating an entity somebody then builds an automation on.

    `resolve_entity(quantity_name) -> str | None` overrides `Quantity.ha_entity`.
    It exists because an entity_id is frequently household-specific
    (`sensor.living_room_*`), and `tests/test_personalisation_guard.py` sweeps
    committed defaults for exactly that — so a deriver whose target entity is
    deployment config resolves it through this hook instead of writing a room
    name into a committed `AlgoSpec`.
    """
    from app.plugin.capabilities import get_capability
    from app.plugin.config_store import plugin_config

    ha_cfg = plugin_config("homeassistant")
    if not (ha_cfg.ha_url and ha_cfg.ha_token):
        logger.debug(f"{spec.algo}: no HA credentials configured — skipping push")
        return 0, []

    set_state = get_capability("homeassistant.entities").set_state
    now = datetime.now(timezone.utc)

    published = 0
    failed: list[str] = []
    for q in spec.quantities:
        entity_id = resolve_entity(q.name) if resolve_entity else q.ha_entity
        if not entity_id:
            continue
        rows = pred_store.curve(session, spec.algo, q.name)
        if not rows:
            continue

        # The state is the nearest future point — the number an automation
        # means when it says "the forecast". Past points from the same cycle
        # stay in the series (a dashboard wants them) but must not become the
        # headline value.
        future = [r for r in rows if r.target_at > now] or rows
        head = future[0]

        series = [
            {"at": r.target_at.isoformat(), "value": round(r.value, q.round_to)}
            for r in rows[:MAX_HA_SERIES_POINTS]
        ]
        ok = set_state(
            entity_id,
            round(head.value, q.round_to),
            {
                "friendly_name": q.description or f"{spec.algo} {q.name}",
                "unit_of_measurement": q.unit,
                "algo": spec.algo,
                "algo_version": head.algo_version,
                "made_at": head.made_at.isoformat() if head.made_at else None,
                "target_at": head.target_at.isoformat(),
                "horizon_min": head.horizon_min,
                "forecast": series,
                "truncated": len(rows) > MAX_HA_SERIES_POINTS,
            },
        )
        if ok:
            published += 1
        else:
            failed.append(entity_id)

    if failed:
        logger.warning(f"{spec.algo}: HA push failed for {failed}")
    return published, failed


def mcp_tools(spec: AlgoSpec) -> list[dict]:
    """Generate this deriver's `<algo>_forecast` and `<algo>_accuracy` tools.

    Generated rather than hand-written so the two surfaces cannot drift from
    the spec: a quantity added to `AlgoSpec` is immediately queryable, and its
    enum in the input schema is the same list the predictor iterates over.
    """

    def _forecast(session: Session, arguments: dict) -> str:
        quantity = arguments.get("quantity") or spec.quantity_names[0]
        spec.quantity(quantity)  # raises KeyError on an undeclared quantity
        rows = pred_store.curve(session, spec.algo, quantity)
        q = spec.quantity(quantity)
        return json.dumps(
            {
                "algo": spec.algo,
                "quantity": quantity,
                "unit": q.unit,
                "made_at": rows[0].made_at.isoformat() if rows else None,
                "model_version": rows[0].algo_version if rows else None,
                "points": [
                    {
                        "target_at": r.target_at.isoformat(),
                        "horizon_min": r.horizon_min,
                        "value": round(r.value, q.round_to),
                        "baseline": round(r.baseline, q.round_to) if r.baseline is not None else None,
                    }
                    for r in rows
                ],
                # Surfaced next to the forecast on purpose: a prediction read
                # without its track record invites unearned confidence.
                "recent_accuracy": scoring.metrics(session, spec.algo, quantity=quantity, days=14),
            },
            indent=2,
        )

    def _accuracy(session: Session, arguments: dict) -> str:
        return json.dumps(
            scoring.metrics(
                session,
                spec.algo,
                quantity=arguments.get("quantity"),
                days=int(arguments.get("days", 30)),
            ),
            indent=2,
        )

    quantity_prop = {
        "type": "string",
        "enum": spec.quantity_names,
        "description": "Which predicted quantity. Defaults to the first declared.",
    }
    read_only = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        # Both generated tools read cached Postgres rows; neither touches a
        # network. HA publishing happens in the prediction cycle, not here.
        open_world_hint=False,
    )

    return [
        CustomTool(
            name=f"{spec.tools_named}_forecast",
            description=(
                f"Current {spec.algo} forecast, with the recent accuracy of this "
                f"model alongside it. Quantities: {', '.join(spec.quantity_names)}."
            ),
            input_schema={"type": "object", "properties": {"quantity": quantity_prop}},
            handler=_forecast,
            category="algo",
            annotations=read_only,
        ).build(),
        CustomTool(
            name=f"{spec.tools_named}_accuracy",
            description=(
                f"How well {spec.algo} has actually been predicting: MAE, RMSE, "
                f"bias and skill against its baseline, overall and per horizon."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "quantity": quantity_prop,
                    "days": {"type": "integer", "default": 30, "description": "Lookback window."},
                },
            },
            handler=_accuracy,
            category="algo",
            annotations=read_only,
        ).build(),
    ]
