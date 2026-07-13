"""Home Assistant REST API client.

Talks to the house HA instance (settings.ha_url) with a long-lived access
token. Only the read endpoints comar needs:
- GET  /api/states     — all entity states
- POST /api/template   — render a Jinja template (used for the entity → area
  map, which /api/states doesn't carry)
"""

import json
import logging

import httpx

from app.config import settings

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
    return {"Authorization": f"Bearer {settings.ha_token}"}


def fetch_states() -> list[dict]:
    """Fetch the current state of every entity. Empty list on failure."""
    url = f"{settings.ha_url.rstrip('/')}/api/states"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.get(url, headers=_headers())
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError):
        logger.exception("Failed to fetch Home Assistant states")
        return []


def render_template(template: str) -> str | None:
    """Render a Jinja template on the HA instance. None on failure."""
    url = f"{settings.ha_url.rstrip('/')}/api/template"
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
