"""Home Assistant REST API client.

Talks to the house HA instance (settings.ha_url) with a long-lived access
token:
- GET  /api/states           — all entity states
- POST /api/template         — render a Jinja template (used for the entity →
  area map, which /api/states doesn't carry)
- POST /api/states/<id>      — create/update a stateless entity (used by the
  commute integration to publish sensor.commute_* back into HA)
- GET  /api/history/period/<start> — recorder history for one entity (used by
  `backfill.py` to seed numeric history for a newly-allowlisted entity)
- POST /api/services/<domain>/<service> — call any HA service (used by
  `call_service`/`facade.notify` to trigger `notify.<mobile_app_target>` for
  the `notifications` integration's push sink)
"""

import json
import logging

import httpx

from app.errors import PermanentError, TransientError
from app.plugin.config_store import plugin_config

logger = logging.getLogger(__name__)

TIMEOUT = 10.0

# /api/states doesn't include area assignments; render one Jinja template
# that builds an entity_id → area dict and emits it as JSON. (A literal `{`
# followed by `{%` lexes as `{{` + `%` in Jinja — hence dict + tojson rather
# than hand-emitting braces.)
_AREA_MAP_TEMPLATE = (
    "{% set ns = namespace(m={}) %}"
    "{% for s in states %}"
    "{% set a = area_name(s.entity_id) %}"
    "{% if a %}{% set ns.m = dict(ns.m, **{s.entity_id: a}) %}{% endif %}"
    "{% endfor %}"
    "{{ ns.m | tojson }}"
)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {plugin_config('homeassistant').ha_token}"}


def fetch_states() -> list[dict]:
    """Fetch the current state of every entity. Empty list on failure."""
    url = f"{plugin_config('homeassistant').ha_url.rstrip('/')}/api/states"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.get(url, headers=_headers())
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError):
        logger.exception("Failed to fetch Home Assistant states")
        return []


def fetch_history(entity_id: str, start, end=None) -> list[dict]:
    """Recorded state history for one entity from HA's own recorder.

    `GET /api/history/period/<start>?filter_entity_id=<id>&end_time=<end>`.
    Returns the raw list of `{state, last_changed, ...}` dicts, oldest first;
    an empty list on any failure (this is a backfill helper, not a live path —
    a failure means "no rows to import", not "abort the sync").

    Exists so a newly-configured deriver is not blind for three weeks. comar's
    `ha_state_changes` only starts collecting numeric history the moment an
    entity is added to `ha_numeric_history_entities`, but HA's recorder has
    been keeping it all along — so the first thing to do after allowlisting an
    entity is import what already exists.

    ⚠️ Bounded by HA's own recorder retention (`purge_keep_days`, default 10),
    so this imports days, not months. `minimal_response` is deliberately NOT
    requested: it omits `last_changed` on repeated states, and the timestamp is
    the entire point here.
    """
    cfg = plugin_config("homeassistant")
    base = cfg.ha_url.rstrip("/")
    url = f"{base}/api/history/period/{start.isoformat()}"
    params = {"filter_entity_id": entity_id, "no_attributes": "true"}
    if end is not None:
        params["end_time"] = end.isoformat()
    try:
        # Generous timeout: ten days of a 5-second sensor is a large response,
        # and this runs on demand rather than on the sync's clock.
        with httpx.Client(timeout=120.0) as client:
            response = client.get(url, headers=_headers(), params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        logger.exception(f"Failed to fetch HA history for {entity_id}")
        return []

    # HA returns a list of per-entity lists. Asking for one entity yields
    # either [[...]] or [] — never a bare list of states, which is worth
    # flattening explicitly rather than indexing [0] and hoping.
    if not payload:
        return []
    if isinstance(payload[0], list):
        return [row for group in payload for row in group]
    return payload


def render_template(template: str) -> str | None:
    """Render a Jinja template on the HA instance. None on failure."""
    url = f"{plugin_config('homeassistant').ha_url.rstrip('/')}/api/template"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(
                url, headers=_headers(), json={"template": template}
            )
            response.raise_for_status()
            return response.text
    except httpx.HTTPError:
        logger.exception("Failed to render Home Assistant template")
        return None


def set_state(entity_id: str, state, attributes: dict | None = None) -> bool:
    """Create/update a (typically stateless) HA entity. False on failure.

    Note: states pushed this way don't survive an HA restart — they're
    recreated the next time the caller writes them (the commute integration
    re-pushes every minute in its sync window).
    """
    url = f"{plugin_config('homeassistant').ha_url.rstrip('/')}/api/states/{entity_id}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(
                url, headers=_headers(),
                json={"state": str(state), "attributes": attributes or {}},
            )
            response.raise_for_status()
            return True
    except httpx.HTTPStatusError as exc:
        # Expected-shaped failure (HA down, entity rejected, auth) — the
        # commute sync pushes this every minute during its morning window,
        # so an HA outage would otherwise emit a stack trace per sensor per
        # minute. A status code is all a caller needs; no traceback.
        logger.warning(
            "Failed to set Home Assistant state for %s: HTTP %s",
            entity_id, exc.response.status_code,
        )
        return False
    except httpx.HTTPError as exc:
        logger.warning("Failed to set Home Assistant state for %s: %s", entity_id, exc)
        return False


def call_service(domain: str, service: str, data: dict) -> bool:
    """Call any Home Assistant service: POST /api/services/<domain>/<service>.

    Unlike the other functions in this module (which log-and-swallow, because
    their callers are best-effort background pushes), this one raises typed
    errors. Its first caller (`notifications`' HA push sink, via `facade.notify`)
    sits behind the same `TransientError`/`PermanentError` contract every other
    integration's `client.py` uses so the scheduler/sweep retry logic can tell
    "HA hiccuped, try again" from "this call will never succeed as configured"
    apart — network failures and 5xx/429 are transient; a 4xx (bad service
    name, malformed data, auth rejected) is permanent.
    """
    url = f"{plugin_config('homeassistant').ha_url.rstrip('/')}/api/services/{domain}/{service}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(url, headers=_headers(), json=data)
    except httpx.HTTPError as exc:
        raise TransientError(f"HA service call {domain}.{service} failed: {exc}") from exc

    if response.status_code == 429 or response.status_code >= 500:
        raise TransientError(
            f"HA service call {domain}.{service} failed: HTTP {response.status_code}"
        )
    if response.status_code >= 400:
        raise PermanentError(
            f"HA service call {domain}.{service} rejected: "
            f"HTTP {response.status_code} {response.text[:200]}"
        )
    return True


def fetch_area_map() -> dict[str, str]:
    """Map entity_id → area name (entities without an area are omitted)."""
    rendered = render_template(_AREA_MAP_TEMPLATE)
    if rendered is None:
        return {}
    try:
        raw = json.loads(rendered)
    except json.JSONDecodeError:
        logger.exception("Failed to parse Home Assistant area map")
        return {}
    return {entity_id: area for entity_id, area in raw.items() if area}
