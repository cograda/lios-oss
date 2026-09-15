"""Strand A — dead-board alerting (`system_alerts` axis 11).

Plan: `vault/Projects/lios/Plans/house-management-2026-08.md` §"Strand A —
device-fleet visibility". `co2-and-environment` died silently before
2026-07-24 — no alert ever fired, because nothing consumed the liveness HA
already held. This closes that gap the same way F11 closed it for daemons.

Two hard rules from the plan, both non-negotiable:

1. **An empty/missing/unparseable registry is a hard refusal, never a
   silent pass.** `load_registry()` raises `FleetRegistryError` rather than
   returning `[]` — a compliance axis that loads zero contracts and reports
   a clean fleet is the exact failure the `lunchcloud-cli` blocklist lesson
   names ("a sanitiser silently running with an empty pattern list ...
   strictly worse than having no sanitiser at all"). The caller
   (`system/tools.py`'s alerts axis) must surface this as a loud alert, not
   swallow it into `status: "ok"`.
2. **No probe means `unknown`, never `passing`.** A board with no matching
   `HAEntity` rows (never conformant, never synced, or simply not yet
   flashed) reports `status: "unknown"` — the same three-valued honesty
   every other axis in this package already uses (`restore_drill`'s
   `never_run`, `daemon_status`'s null `last_heartbeat_age`, etc).

The discriminator is derived, never hand-listed: `contracts/fleet.md`'s "The
fleet" table (canonical board identity, reconciled against HA's live
registries — see that file's own header) is parsed for each board's entity
prefix. This is deliberate per the plan: *"a hand list is how the dead board
went unnoticed last time"*. The "Planned — not yet flashed" table is
excluded on purpose — those boards "own nothing yet ... no entity prefix, no
area" per fleet.md itself, so alerting on one would either always read
`unknown` or become a proxy for hardware that doesn't exist.

Liveness reads exactly the four contract entities from
`contracts/device-contract.md` §4 (`sensor.<prefix>_uptime`,
`_esphome_version`, `_wifi_signal`, `_ip_address`) — "consumes exactly what
the contract promises and nothing more" (Strand C's obligation on this
axis). Per §4, **availability is the liveness signal, not a reading's
value** ("Do not infer liveness from a room sensor's value ... uptime plus
`unavailable` is the whole answer") — so a board is `dead` when every
contract entity reads `unavailable`, or when none has changed state inside
the configured threshold (Strand A4: config, not a constant, because a
battery/solar or deliberately-intermittent board would false-alarm on a
fixed window).

Reads via `homeassistant`'s declared `homeassistant.entities` capability
(`facade.entity_state`), never `app.integrations.homeassistant.models`
directly — the same capability-boundary rule every cross-package call in
this codebase follows (`tests/test_capability_boundaries.py`). Bounded: at
most 4 single-row, indexed lookups per board (≤4 × len(registry) queries
total for the whole fleet, today ~32) — never a table-wide scan, satisfying
Strand A2's "don't add a third such unbounded read" alongside axis 1's `.all()`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.plugin.capabilities import get_capability
from app.plugin.config_store import plugin_config

logger = logging.getLogger(__name__)

# Emergency fallback only, used when `system.device_fleet_stale_minutes`
# resolves to something unusable — see `_stale_minutes_for()`.
DEFAULT_STALE_MINUTES = 30

# The four signals `contracts/device-contract.md` §1 requires every
# conformant board to expose, all under the `sensor.` domain.
_CONTRACT_SUFFIXES = ("uptime", "esphome_version", "wifi_signal", "ip_address")


class FleetRegistryError(RuntimeError):
    """The fleet registry is missing, empty, or unparseable.

    Per Strand A's "an empty registry must be a hard refusal" rule, this
    must never be swallowed into a clean report — see module docstring.
    """


@dataclass(frozen=True)
class BoardEntry:
    board: str  # e.g. "ESP32-C3 SuperMini (`finn-air-quality/`)"
    room: str
    node_name: str
    entity_prefix: str  # stripped of the trailing "_*", e.g. "finn_air_quality"


def _repo_root_fleet_md() -> Path:
    """`contracts/fleet.md` resolved relative to THIS file, never the cwd
    or a home-relative guess — the exact lesson `build_shortcuts.py` paid
    for when the certs move broke it (lios `CLAUDE.md`: "when an element
    lands, ask what reads it from a fixed path").

    This file lives at
    `core/server/backend/app/integrations/system/device_fleet.py`; six
    `parents[]` up is the repo root (`core/` is one, `server/` another).
    Only used as the fallback for contexts that check out the whole repo
    (local `make dev`, pytest) — the deployed container reads the
    configured/mounted path instead (see manifest.py's
    `device_fleet_registry_path`).
    """
    return Path(__file__).resolve().parents[6] / "contracts" / "fleet.md"


def _fleet_md_path() -> Path:
    configured = plugin_config("system").device_fleet_registry_path
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return candidate
    return _repo_root_fleet_md()


_TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")


def load_registry(path: Path | None = None) -> list[BoardEntry]:
    """Parse the canonical `## The fleet` table. Raises `FleetRegistryError`
    on anything short of at least one real board row — see module docstring
    for why this must never degrade to an empty list.
    """
    fleet_path = path if path is not None else _fleet_md_path()
    try:
        text = fleet_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FleetRegistryError(
            f"cannot read fleet registry at {fleet_path}: {exc}"
        ) from exc

    if not text.strip():
        raise FleetRegistryError(f"fleet registry at {fleet_path} is empty")

    # Isolate "## The fleet" section, stopping at the next "## " heading —
    # this is what excludes "## Planned — not yet flashed" below it (see
    # module docstring for why planned boards must not be alerted on).
    section_match = re.search(
        r"^## The fleet\s*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    if not section_match:
        raise FleetRegistryError(
            f"fleet registry at {fleet_path} has no '## The fleet' section"
        )
    section = section_match.group(1)

    rows: list[BoardEntry] = []
    header_seen = False
    for line in section.splitlines():
        m = _TABLE_ROW_RE.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if not header_seen:
            # First matched row is the header; the markdown separator row
            # (all dashes) is skipped by the dash check below.
            header_seen = True
            continue
        if all(re.fullmatch(r"-+", c) for c in cells if c):
            continue
        if len(cells) < 4:
            continue
        board, room, node_name, entity_prefix = cells[0], cells[1], cells[2], cells[3]
        prefix = entity_prefix.strip("`")
        if prefix.endswith("_*"):
            prefix = prefix[:-2]
        node_name = node_name.strip("`")
        if not prefix or not node_name:
            continue
        rows.append(
            BoardEntry(board=board, room=room, node_name=node_name, entity_prefix=prefix)
        )

    if not rows:
        raise FleetRegistryError(
            f"fleet registry at {fleet_path} parsed with zero boards — "
            "refusing to report a clean fleet from an empty source"
        )
    return rows


def _stale_minutes_for(node_name: str) -> int:
    """Per-board threshold: `device_fleet_stale_minutes_overrides[node_name]`
    if set and parseable, else `device_fleet_stale_minutes`, else the
    emergency constant — same three-tier fallback shape as
    `system/tools.py::_daemon_silent_minutes`.
    """
    config = plugin_config("system")
    overrides = config.device_fleet_stale_minutes_overrides or {}
    raw_override = overrides.get(node_name)
    if raw_override is not None:
        try:
            value = int(raw_override)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
        logger.warning(
            "ignoring bad device_fleet_stale_minutes_overrides[%r]=%r",
            node_name, raw_override,
        )

    default = config.device_fleet_stale_minutes
    if isinstance(default, int) and default > 0:
        return default
    logger.warning(
        "ignoring bad system.device_fleet_stale_minutes %r; using default %d",
        default, DEFAULT_STALE_MINUTES,
    )
    return DEFAULT_STALE_MINUTES


def _board_status(
    board: BoardEntry, now: datetime, facade: Any, session: Session
) -> dict[str, Any]:
    """One board's liveness, read off exactly the four contract entities.

    Never a table-wide scan — `facade.entity_state()` is a single indexed
    lookup per entity_id, so this is ≤4 bounded queries per board (Strand
    A2's "don't add a third unbounded read").
    """
    entities: list[dict[str, Any]] = []
    for suffix in _CONTRACT_SUFFIXES:
        entity_id = f"sensor.{board.entity_prefix}_{suffix}"
        state = facade.entity_state(session, entity_id)
        if state is not None:
            entities.append(state)

    if not entities:
        return {
            "node_name": board.node_name,
            "room": board.room,
            "entity_prefix": board.entity_prefix,
            "status": "unknown",
            "detail": "no contract entities found in HA — never synced, or not (yet) conformant",
        }

    all_unavailable = all(e.get("state") == "unavailable" for e in entities)
    if all_unavailable:
        return {
            "node_name": board.node_name,
            "room": board.room,
            "entity_prefix": board.entity_prefix,
            "status": "dead",
            "detail": "all contract entities unavailable",
        }

    threshold_minutes = _stale_minutes_for(board.node_name)
    last_changed_values = [
        e["last_changed"] for e in entities if e.get("last_changed") is not None
    ]
    if last_changed_values:
        freshest = max(last_changed_values)
        if freshest.tzinfo is None:
            freshest = freshest.replace(tzinfo=timezone.utc)
        age_minutes = (now - freshest).total_seconds() / 60
        if age_minutes > threshold_minutes:
            return {
                "node_name": board.node_name,
                "room": board.room,
                "entity_prefix": board.entity_prefix,
                "status": "dead",
                "detail": (
                    f"freshest state {age_minutes:.0f}m old "
                    f"(threshold {threshold_minutes}m)"
                ),
            }

    return {
        "node_name": board.node_name,
        "room": board.room,
        "entity_prefix": board.entity_prefix,
        "status": "ok",
        "detail": None,
    }


def evaluate_alert(session: Session, *, registry_path: Path | None = None) -> dict:
    """The `system_alerts` axis: per-board liveness plus alert-ready issue
    strings for boards that are `dead`.

    Raises `FleetRegistryError` if the registry can't be loaded — the
    caller (`system/tools.py`) must treat that as a loud alert, never as
    "no boards, nothing wrong" (see module docstring).
    """
    registry = load_registry(registry_path)
    facade = get_capability("homeassistant.entities")
    now = datetime.now(timezone.utc)

    boards = [_board_status(board, now, facade, session) for board in registry]
    issues = [
        f"board {b['node_name']} ({b['room']}) dead — {b['detail']}"
        for b in boards
        if b["status"] == "dead"
    ]
    return {"boards": boards, "issues": issues}
