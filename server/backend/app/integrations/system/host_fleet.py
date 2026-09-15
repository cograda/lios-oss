"""Host-liveness alerting (`system_alerts` axis 12).

Companion to `device_fleet.py` (Strand A, axis 11) — same shape, one layer
down. `contracts/fleet.md` answers "is this ESP32 board alive"; this answers
"is the machine it depends on alive" — Proxmox nodes, their VMs/LXCs, and
the standalone Pis. Written after `turing` (a Proxmox cluster node) went
down on 2026-09-08 with nobody told: `pulse` (LXC 104) and `uptime-kuma`
both already know how to answer that question and both run *on the cluster
itself*, so losing the node that hosts them takes the alarm out with the
patient (recorded twice in `deploy/CLAUDE.md`, unactioned both times —
"an off-box check on node status is still the gap worth closing").

Same two hard rules as `device_fleet.py`, restated here because they are the
whole point and must not silently diverge between the two axes:

1. **An empty/missing/unparseable registry is a hard refusal, never a
   silent pass.** `load_registry()` raises `HostRegistryError` — a fleet
   that "loads zero hosts" and reports clean is the exact `lunchcloud-cli`
   blocklist failure ("a sanitiser silently running with an empty pattern
   list ... strictly worse than having no sanitiser at all").
2. **No probe data means `unknown`, never `ok`.** A host whose `probe`
   column is `none`, or whose configured probe cannot be reached/is not
   configured, reports `status: "unknown"` — never a clean pass.

The discriminator is `contracts/hosts.md`'s "## The hosts" table, derived
from `deploy/CLAUDE.md`'s cluster/LXC/VM tables and the Pi projects under
`things/` — not hand-listed, same reasoning as `fleet.md`.

Two probe kinds, per `hosts.md`'s `probe` column:

- **`pulse`** — reads Pulse's own HTTP API (`GET /api/state` (its `resources`
  list carries runtime status; see `PulseClient`) — see `PulseClient`'s docstring for the exact
  shape and the confidence caveat) for this host's `status`. Config keys
  `system.pulse_url` / `system.pulse_token`; unset means every
  `pulse`-probed host in this evaluation reports `unknown` with reason
  `probe_unconfigured` — **never `ok`**, per rule 2. Bounded to **one**
  Pulse HTTP request per `evaluate_alert()` call, however many hosts are
  `pulse`-probed (`_pulse_statuses()` fetches once and is looked up by
  name per host) — the same "don't add an unbounded read" discipline
  `device_fleet.py`'s module docstring names for its own four-entities-
  per-board bound.
- **`ha_entity:<entity_id>`** — reads one entity via the existing
  `homeassistant.entities` capability (`facade.entity_state`), exactly the
  capability-boundary rule `device_fleet.py` already follows. This is
  always a proxy signal, not a purpose-built host tracker (see the
  `hosts.md` row it's attached to for what specifically it is a proxy
  for) — `unavailable`/`unknown` reading means dead, and a missing row
  means unknown. ⚠️ **Also checks staleness of `last_updated`** (issue
  #167: `sensor.shed_cam_temperature` sat frozen for nine days and this
  probe reported `ok` throughout, because a number is not `unavailable`
  — a frozen sensor is indistinguishable from a stable one by value
  alone). `system.host_fleet_stale_minutes` (default 60) bounds how old
  HA's own `last_updated` may be before the reading counts as `dead` with
  detail `"stale: last update <age> old"`; a missing `last_updated` (a row
  from before that column existed) can't be judged for staleness and
  falls through to `ok` rather than manufacturing a false alarm.
- **`none`** — always `unknown`. Recorded in `hosts.md` deliberately
  (rather than omitting the row) so "nothing watches this host" is a fact
  this axis states, not a silence someone has to notice on their own.

⚠️ **The binding limitation, stated in `hosts.md` too and worth repeating
here because it is the reason this axis cannot be the whole answer:** this
code runs inside `lios-core`, which runs on `lovelace-docker`. It can only
ever detect some *other* host dying — if `lovelace` (and so
`lovelace-docker`, and lios-core with it) goes down, this axis goes dark
with it and the household hears nothing from *this* mechanism. A
daemon-side watcher running off the cluster, built in parallel, is the
piece that covers the whole-cluster-down case; this axis answers "did some
other host in the fleet die while lios itself kept running."
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.plugin.capabilities import get_capability
from app.plugin.config_store import plugin_config

logger = logging.getLogger(__name__)

PULSE_REQUEST_TIMEOUT = 10.0
# Resource `type` values in Pulse v6's /api/resources that denote a host we
# track: PVE nodes (`agent`, technology=proxmox), QEMU guests (`vm`) and LXC
# guests (`system-container`). Measured live 2026-09-08; `storage` and
# `physical_disk` rows are deliberately excluded (names repeat across nodes).
PULSE_HOST_TYPES = frozenset({"agent", "vm", "system-container", "node"})

# Emergency fallback only, used when `system.host_fleet_stale_minutes`
# resolves to something unusable — same shape as
# `device_fleet.py::DEFAULT_STALE_MINUTES`.
DEFAULT_HA_ENTITY_STALE_MINUTES = 60


class HostRegistryError(RuntimeError):
    """The host registry (`contracts/hosts.md`) is missing, empty, or
    unparseable. Must never be swallowed into a clean report — see module
    docstring's rule 1.
    """


@dataclass(frozen=True)
class HostEntry:
    name: str
    ip: str
    kind: str  # pve-node / vm / lxc / pi / network
    runs_on: str
    probe: str  # "pulse" | "none" | "ha_entity:<entity_id>"


def _repo_root_hosts_md() -> Path:
    """`contracts/hosts.md` resolved relative to THIS file, never the cwd —
    same fixed-path lesson `device_fleet.py::_repo_root_fleet_md` already
    paid for (lios `CLAUDE.md`: "when an element lands, ask what reads it
    from a fixed path").

    This file lives at
    `core/server/backend/app/integrations/system/host_fleet.py`; six
    `parents[]` up is the repo root. Only used as the fallback for contexts
    that check out the whole repo (local `make dev`, pytest) — the
    deployed container reads the configured/mounted path instead (see
    manifest.py's `host_fleet_registry_path`, and `docker-compose.yml`'s
    existing `HOME_CONTRACTS_HOST_PATH` mount, which covers this file too
    since it mounts the whole `contracts/` directory that `fleet.md` already
    uses).
    """
    return Path(__file__).resolve().parents[6] / "contracts" / "hosts.md"


def _hosts_md_path() -> Path:
    configured = plugin_config("system").host_fleet_registry_path
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return candidate
    return _repo_root_hosts_md()


_TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")


def _clean_cell(cell: str) -> str:
    """Strip every inline-code backtick out of a markdown table cell,
    keeping everything else — including a trailing parenthetical.

    Issue #173: a cell like `` `homeassistant` (VM 102) `` has its code span
    only around the first word, so `cell.strip("`")` (which only trims
    leading/trailing backtick *characters* off the whole string) left the
    inner backtick untouched, producing "homeassistant` (VM 102)". The
    `runs_on` column can carry more than one such span in one cell (e.g.
    ``(physical — was `proxmoxbigbox`)``), so `.strip()` was never going to
    be enough even for the cells it did manage to fix. Backticks never
    carry meaning once the table row is parsed into fields — removing all
    of them, wherever they fall, is the whole fix.
    """
    return cell.replace("`", "").strip()


def load_registry(path: Path | None = None) -> list[HostEntry]:
    """Parse the canonical `## The hosts` table. Raises `HostRegistryError`
    on anything short of at least one real host row — see module docstring
    for why this must never degrade to an empty list.
    """
    hosts_path = path if path is not None else _hosts_md_path()
    try:
        text = hosts_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HostRegistryError(
            f"cannot read host registry at {hosts_path}: {exc}"
        ) from exc

    if not text.strip():
        raise HostRegistryError(f"host registry at {hosts_path} is empty")

    section_match = re.search(
        r"^## The hosts\s*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    if not section_match:
        raise HostRegistryError(
            f"host registry at {hosts_path} has no '## The hosts' section"
        )
    section = section_match.group(1)

    rows: list[HostEntry] = []
    header_seen = False
    for line in section.splitlines():
        m = _TABLE_ROW_RE.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if not header_seen:
            header_seen = True
            continue
        if all(re.fullmatch(r"-+", c) for c in cells if c):
            continue
        if len(cells) < 5:
            continue
        name, ip, kind, runs_on, probe = (
            _clean_cell(cells[0]),
            _clean_cell(cells[1]),
            _clean_cell(cells[2]),
            _clean_cell(cells[3]),
            _clean_cell(cells[4]),
        )
        if not name or not probe:
            continue
        rows.append(HostEntry(name=name, ip=ip, kind=kind, runs_on=runs_on, probe=probe))

    if not rows:
        raise HostRegistryError(
            f"host registry at {hosts_path} parsed with zero hosts — "
            "refusing to report a clean fleet from an empty source"
        )
    return rows


class PulseClient:
    """Thin wrapper over Pulse v6's `GET /api/state` (was /api/resources) — the one
    documented, filterable, per-resource-status endpoint (`rcourtman/Pulse`,
    `docs/API.md`). Confidence note, per the task's own instruction to say
    so plainly: **Pulse's own docs do not publish a worked JSON example for
    this endpoint** — only the documented field list (`id`, `name`, `type`,
    `status` in `online|offline|warning|unknown`, plus a `health.verdict`
    envelope) and the query parameters (`type`, `status`, `q`, `page`,
    `limit`, ...). This is implemented against that documented shape;
    it has never been run against a real Pulse instance (no production
    access here — see the PR body). If the real response nests resources
    under a different key, or spells the status field differently,
    `_pulse_statuses()`'s `except Exception` below converts that into
    `unknown` for every pulse-probed host rather than crashing the whole
    alerts axis — see its docstring.

    Auth is `X-API-Token: <token>` (documented). One request per
    `evaluate_alert()` call, paged at the documented max (`limit=100`) —
    the household fleet is under a dozen resources, so one page always
    covers it; a second page would only ever be silently dropped data, so
    a truncated first page is treated as suspicious, not swallowed (see
    `fetch_statuses`).
    """

    def __init__(self, base_url: str, token: str):
        self._base_url = base_url.rstrip("/")
        self._token = token

    def fetch_statuses(self) -> dict[str, str]:
        """One bounded HTTP call → `{resource_name: status}` for every
        resource Pulse currently reports (`status` is Pulse's own
        `online|offline|warning|unknown` vocabulary, translated by the
        caller — see `_status_for_pulse_host`).
        """
        url = f"{self._base_url}/api/state"
        with httpx.Client(timeout=PULSE_REQUEST_TIMEOUT) as client:
            response = client.get(
                url,
                headers={"X-API-Token": self._token},
                params={"limit": 100},
            )
            response.raise_for_status()
            payload = response.json()
        return self.parse_statuses(payload)

    @staticmethod
    def parse_statuses(payload: Any) -> dict[str, str]:
        """`{resource_name: status}` from a /api/state (or /api/resources) payload.

        Measured against the live instance 2026-09-08 (v6): the list is under
        `data` (with `meta`/`aggregations` beside it), not `resources` — the
        docs-derived guess read an empty list and every pulse-probed host said
        "not reported by pulse". `resources` and a bare list are kept as
        fallbacks for other builds. Only `PULSE_HOST_TYPES` rows are kept:
        `storage`/`physical_disk` reuse names across nodes (`backup-store`
        appears once online and once offline) and would clobber each other in
        a name-keyed map.
        """
        if isinstance(payload, list):
            resources = payload
        elif isinstance(payload, dict):
            resources = payload.get("data") or payload.get("resources") or []
        else:
            resources = []
        statuses: dict[str, str] = {}
        for resource in resources:
            if not isinstance(resource, dict) or resource.get("type") not in PULSE_HOST_TYPES:
                continue
            name = resource.get("name")
            status = resource.get("status")
            if name and status:
                statuses[name] = status
        return statuses


def _pulse_client() -> PulseClient | None:
    """`None` when Pulse isn't configured — every `pulse`-probed host must
    then report `unknown`/`probe_unconfigured`, never `ok` (rule 2).
    """
    config = plugin_config("system")
    url = getattr(config, "pulse_url", None)
    token = getattr(config, "pulse_token", None)
    if not url or not token:
        return None
    return PulseClient(url, token)


def _pulse_statuses(client: PulseClient | None) -> tuple[dict[str, str], str | None]:
    """Fetch once, bounded — returns `(statuses, error_reason)`. A non-None
    `error_reason` means every `pulse`-probed host reports `unknown` with
    that reason; it is never raised, because a Pulse outage is a live-probe
    failure, not a registry refusal (rule 1 is about the registry, not the
    probe backend).
    """
    if client is None:
        return {}, "probe_unconfigured"
    try:
        return client.fetch_statuses(), None
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("pulse probe unreachable: %s", exc)
        return {}, "pulse_unreachable"
    except Exception:  # noqa: BLE001 — see PulseClient docstring's confidence note
        logger.exception("pulse probe returned an unexpected shape")
        return {}, "pulse_unreachable"


def _status_for_pulse_host(name: str, statuses: dict[str, str], error_reason: str | None) -> dict[str, Any]:
    if error_reason is not None:
        return {"status": "unknown", "detail": error_reason}
    # hosts.md names carry a parenthetical for humans ("homeassistant (VM 102)",
    # "lovelace-docker (VM 103, was dockerbox)"); Pulse reports the bare guest
    # name. Measured on the first live read 2026-09-08: both nodes matched
    # (bare cells) and every guest said "not reported". Exact match first,
    # then the first whitespace-delimited token.
    pulse_status = statuses.get(name)
    if pulse_status is None:
        pulse_status = statuses.get(_pulse_lookup_key(name))
    if pulse_status is None:
        return {"status": "unknown", "detail": "not reported by pulse"}
    # Vocabulary of /api/state's `resources[].status` (measured 2026-09-08):
    # nodes say online|offline, guests say running|stopped. NOT the
    # `/api/resources` endpoint: its `status` is an alert rollup that read
    # `warning` for every resource — including a stopped LXC — the moment two
    # alerts existed anywhere. Liveness must come from the runtime field.
    if pulse_status in ("online", "running"):
        return {"status": "ok", "detail": None}
    if pulse_status in ("offline", "stopped"):
        return {"status": "dead", "detail": f"pulse reports {pulse_status}"}
    # any other value Pulse's vocabulary might add later: neither a clean
    # pass nor a confirmed failure.
    return {"status": "unknown", "detail": f"pulse reports {pulse_status!r}"}


def _pulse_lookup_key(name: str) -> str:
    """`"homeassistant (VM 102)"` → `"homeassistant"`; a bare name is unchanged."""
    return name.split(" (", 1)[0].split()[0] if name.strip() else name


def _ha_entity_stale_minutes() -> int:
    """`system.host_fleet_stale_minutes`, falling back to an emergency
    constant on a missing/unparseable config value — same three-tier
    fallback shape as `device_fleet.py::_stale_minutes_for`.
    """
    config = plugin_config("system")
    value = getattr(config, "host_fleet_stale_minutes", None)
    if isinstance(value, int) and value > 0:
        return value
    logger.warning(
        "ignoring bad system.host_fleet_stale_minutes %r; using default %d",
        value, DEFAULT_HA_ENTITY_STALE_MINUTES,
    )
    return DEFAULT_HA_ENTITY_STALE_MINUTES


def _status_for_ha_entity_host(entity_id: str, facade: Any, session: Session) -> dict[str, Any]:
    """Issue #167: a frozen sensor is indistinguishable from a stable one by
    value alone — `sensor.shed_cam_temperature` sat at 24.5 for nine days
    and this probe reported `ok` throughout, because a number is not
    `unavailable`. `last_updated` is HA's own "have I heard anything from
    this entity" timestamp (distinct from `last_changed`, which only moves
    when the *value* changes) — a publisher that has died leaves both
    frozen at the same instant, so checking staleness on `last_updated`
    catches exactly that case without false-alarming on a value that
    happens to hold steady while updates keep arriving.

    `last_updated` missing (a row written before that column existed, or by
    a path that hasn't backfilled it) means staleness can't be judged —
    that is not the same claim as "no probe data" (rule 2), because the
    entity itself is known and reporting a real state; it falls through to
    `ok` rather than `unknown`.
    """
    state = facade.entity_state(session, entity_id)
    if state is None:
        return {"status": "unknown", "detail": f"{entity_id} not found in HA"}
    raw_state = state.get("state")
    if raw_state in ("unavailable", "unknown"):
        return {"status": "dead", "detail": f"{entity_id} {raw_state}"}

    last_updated = state.get("last_updated")
    if last_updated is not None:
        if last_updated.tzinfo is None:
            last_updated = last_updated.replace(tzinfo=timezone.utc)
        age_minutes = (datetime.now(timezone.utc) - last_updated).total_seconds() / 60
        threshold_minutes = _ha_entity_stale_minutes()
        if age_minutes > threshold_minutes:
            return {
                "status": "dead",
                "detail": (
                    f"{entity_id} stale: last update {age_minutes:.0f}m old "
                    f"(threshold {threshold_minutes}m)"
                ),
            }

    return {"status": "ok", "detail": None}


def _host_status(
    host: HostEntry,
    *,
    pulse_statuses: dict[str, str],
    pulse_error_reason: str | None,
    facade: Any,
    session: Session,
) -> dict[str, Any]:
    if host.probe == "pulse":
        result = _status_for_pulse_host(host.name, pulse_statuses, pulse_error_reason)
    elif host.probe.startswith("ha_entity:"):
        entity_id = host.probe.split(":", 1)[1]
        result = _status_for_ha_entity_host(entity_id, facade, session)
    else:
        result = {"status": "unknown", "detail": "no probe configured"}
    return {
        "name": host.name,
        "kind": host.kind,
        "runs_on": host.runs_on,
        "probe": host.probe,
        **result,
    }


def evaluate_alert(
    session: Session,
    *,
    registry_path: Path | None = None,
    pulse_client: PulseClient | None = None,
) -> dict:
    """The `system_alerts` axis: per-host liveness plus alert-ready issue
    strings for hosts that are `dead`.

    Raises `HostRegistryError` if the registry can't be loaded — the caller
    (`system/tools.py`) must treat that as a loud alert, never as "no
    hosts, nothing wrong" (module docstring rule 1).

    `pulse_client` is an injection seam for tests (never a production
    control) — production always resolves via `_pulse_client()` from
    config, same as `device_fleet.py` never takes a facade override.
    """
    registry = load_registry(registry_path)

    pulse_hosts = [h for h in registry if h.probe == "pulse"]
    pulse_statuses: dict[str, str] = {}
    pulse_error_reason: str | None = None
    if pulse_hosts:
        client = pulse_client if pulse_client is not None else _pulse_client()
        pulse_statuses, pulse_error_reason = _pulse_statuses(client)

    ha_entity_hosts = [h for h in registry if h.probe.startswith("ha_entity:")]
    facade = get_capability("homeassistant.entities") if ha_entity_hosts else None

    hosts = [
        _host_status(
            host,
            pulse_statuses=pulse_statuses,
            pulse_error_reason=pulse_error_reason,
            facade=facade,
            session=session,
        )
        for host in registry
    ]
    issues = [
        f"host {h['name']} ({h['kind']}) dead — {h['detail']}"
        for h in hosts
        if h["status"] == "dead"
    ]
    return {"hosts": hosts, "issues": issues}
