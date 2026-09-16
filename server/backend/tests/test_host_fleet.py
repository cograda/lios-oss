"""Host-liveness alerting (`system_alerts` axis 12) end to end.

Companion to `test_device_fleet.py` — same two hard rules under test, one
layer down (hosts, not boards):

- an empty/missing/unparseable registry is a HARD REFUSAL, never a silent
  "no hosts, nothing wrong"
- no probe data (`probe: none`, an unconfigured Pulse, or an entity HA has
  never seen) reports `unknown`, never `ok`

db tier for the `ha_entity` probe path (real `ha_entities` rows via the
`homeassistant.entities` capability, exactly as production does — no
mocking of `get_capability`). The `pulse` probe path doesn't touch the DB
at all, so those tests inject a fake `PulseClient` rather than talking to a
real Pulse instance (never call the real Pulse — no production access).
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.db


def _real_hosts_md_path() -> Path:
    """`contracts/hosts.md` resolved from THIS test file, not `host_fleet.py`'s
    own fallback — a separate, independent resolution so a test against the
    real file shape doesn't depend on the code path it's testing.
    """
    return Path(__file__).resolve().parents[4] / "contracts" / "hosts.md"


HOSTS_MD_MIXED = """\
# Host inventory — one machine, one row

## The hosts

| Name | IP | Kind | Runs on | Probe | Notes |
|---|---|---|---|---|---|
| `test-node` | `10.0.0.1` | `pve-node` | (physical) | `pulse` | primary |
| `test-guest` | `10.0.0.2` | `lxc` | `test-node` | `pulse` | secondary |
| `test-pi` | `10.0.0.3` | `pi` | (physical) | `ha_entity:sensor.test_pi_temperature` | proxy signal |
| `test-unwatched` | `10.0.0.4` | `pi` | (physical) | `none` | nothing checks this |
"""

HOSTS_MD_EMPTY_TABLE = """\
# Host inventory

## The hosts

| Name | IP | Kind | Runs on | Probe | Notes |
|---|---|---|---|---|---|

"""


def _write_hosts_md(tmp_path, content):
    path = tmp_path / "hosts.md"
    path.write_text(content, encoding="utf-8")
    return path


class _FakePulseClient:
    """Test double for `host_fleet.PulseClient` — one canned response, no
    network call, so `evaluate_alert(..., pulse_client=...)` never touches
    a real Pulse instance.
    """

    def __init__(self, statuses=None, *, raises=None):
        self._statuses = statuses or {}
        self._raises = raises

    def fetch_statuses(self):
        if self._raises is not None:
            raise self._raises
        return dict(self._statuses)


def _seed_entity(session, entity_id, *, state, last_updated=None):
    from app.integrations.homeassistant.models import HAEntity

    session.add(
        HAEntity(
            entity_id=entity_id,
            domain=entity_id.split(".", 1)[0],
            state=state,
            last_updated=last_updated,
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# 1. Empty/missing registry refuses
# ---------------------------------------------------------------------------


def test_missing_registry_file_raises(tmp_path):
    from app.integrations.system.host_fleet import HostRegistryError, load_registry

    with pytest.raises(HostRegistryError):
        load_registry(tmp_path / "does-not-exist.md")


def test_empty_table_registry_raises(tmp_path):
    from app.integrations.system.host_fleet import HostRegistryError, load_registry

    path = _write_hosts_md(tmp_path, HOSTS_MD_EMPTY_TABLE)
    with pytest.raises(HostRegistryError):
        load_registry(path)


def test_registry_with_no_hosts_section_raises(tmp_path):
    from app.integrations.system.host_fleet import HostRegistryError, load_registry

    path = _write_hosts_md(tmp_path, "# Host inventory\n\nNothing here.\n")
    with pytest.raises(HostRegistryError):
        load_registry(path)


def test_evaluate_alert_propagates_empty_registry_as_refusal(db_session, tmp_path):
    from app.integrations.system.host_fleet import HostRegistryError, evaluate_alert

    path = _write_hosts_md(tmp_path, HOSTS_MD_EMPTY_TABLE)
    with pytest.raises(HostRegistryError):
        evaluate_alert(db_session, registry_path=path)


def test_real_hosts_md_parses_with_no_backticks_in_any_field():
    """Issue #173: parsing the ACTUAL `contracts/hosts.md` used to leave a
    trailing backtick on names like `` homeassistant` (VM 102) `` and both
    backticks in `runs_on` (e.g. `` `lovelace` `` stayed `` `lovelace` ``
    rather than becoming `lovelace`). Assert against the real file, not a
    hand-written fixture — the fixture's cells never actually reproduced the
    shape that broke in production (a code span covering only part of a
    cell, followed by a trailing parenthetical, or more than one span in
    one cell).

    `contracts/hosts.md` lives one level above `core/` — the real household
    fleet inventory (VM ids, real hostnames), never shipped — so it's simply
    absent on a standalone checkout (e.g. the public lios-oss release).
    Skip rather than fail there; the fixture-based tests above already cover
    the parser's general correctness, this one only pins it against the
    live data on the checkout that actually has it.
    """
    from app.integrations.system.host_fleet import load_registry

    path = _real_hosts_md_path()
    if not path.is_file():
        pytest.skip(f"no real hosts.md at {path} — not inside the lios monorepo")

    rows = load_registry(path)
    assert rows  # the file is not itself empty

    for row in rows:
        for field_name in ("name", "ip", "kind", "runs_on", "probe"):
            value = getattr(row, field_name)
            assert "`" not in value, f"{field_name}={value!r} on {row.name} still has a backtick"

    # The two shapes issue #173 named explicitly, pinned by name.
    by_name = {r.name: r for r in rows}
    assert "homeassistant (VM 102)" in by_name
    ha_row = by_name["homeassistant (VM 102)"]
    assert "`" not in ha_row.runs_on
    assert ha_row.runs_on  # the parenthetical/content survived, not just the backticks


def test_system_alerts_surfaces_empty_registry_as_a_loud_alert(db_session, tmp_path, monkeypatch):
    """End-to-end through `system_alerts`: a refusal must show up as an
    alert entry and a non-None `registry_error` — never as `hosts: []`
    with nothing else to say.
    """
    from app.integrations.system import host_fleet
    from app.integrations.system.tools import handle_alerts_household

    path = _write_hosts_md(tmp_path, HOSTS_MD_EMPTY_TABLE)
    monkeypatch.setattr(host_fleet, "_hosts_md_path", lambda: path)

    payload = json.loads(handle_alerts_household(db_session, {}))

    assert payload["host_fleet"]["registry_error"] is not None
    assert payload["host_fleet"]["hosts"] == []
    entry = next((a for a in payload["alerts"] if a["integration"] == "host_fleet"), None)
    assert entry is not None
    assert "registry" in entry["issues"][0]


# ---------------------------------------------------------------------------
# 2. `pulse` probe: unconfigured -> unknown, never ok
# ---------------------------------------------------------------------------


def test_unconfigured_pulse_reports_unknown_never_ok(db_session, tmp_path, monkeypatch):
    from app.integrations.system import host_fleet
    from app.integrations.system.host_fleet import evaluate_alert

    path = _write_hosts_md(tmp_path, HOSTS_MD_MIXED)
    # No pulse_client injected AND config resolves to unconfigured.
    monkeypatch.setattr(host_fleet, "_pulse_client", lambda: None)

    result = evaluate_alert(db_session, registry_path=path)

    pulse_hosts = [h for h in result["hosts"] if h["probe"] == "pulse"]
    assert len(pulse_hosts) == 2
    for host in pulse_hosts:
        assert host["status"] == "unknown"
        assert host["detail"] == "probe_unconfigured"
    # unknown must never alert
    assert result["issues"] == []


def test_pulse_probe_failure_reports_unknown_never_dead(db_session, tmp_path):
    """A Pulse outage is a live-probe failure, not evidence the host is
    down — must not be conflated with a genuine `offline` verdict.
    """
    from app.integrations.system.host_fleet import evaluate_alert

    path = _write_hosts_md(tmp_path, HOSTS_MD_MIXED)
    client = _FakePulseClient(raises=RuntimeError("connection refused"))

    result = evaluate_alert(db_session, registry_path=path, pulse_client=client)

    pulse_hosts = [h for h in result["hosts"] if h["probe"] == "pulse"]
    for host in pulse_hosts:
        assert host["status"] == "unknown"
    assert result["issues"] == []


# ---------------------------------------------------------------------------
# 3. `pulse` probe: online -> ok, offline -> dead
# ---------------------------------------------------------------------------


def test_pulse_reports_node_online_as_ok(db_session, tmp_path):
    from app.integrations.system.host_fleet import evaluate_alert

    path = _write_hosts_md(tmp_path, HOSTS_MD_MIXED)
    client = _FakePulseClient({"test-node": "online", "test-guest": "online"})

    result = evaluate_alert(db_session, registry_path=path, pulse_client=client)

    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["test-node"]["status"] == "ok"
    assert by_name["test-guest"]["status"] == "ok"
    assert result["issues"] == []


def test_pulse_reports_node_offline_as_dead(db_session, tmp_path):
    """The core case this whole axis exists for: a Proxmox node going
    dark, reported by Pulse (which lives on the cluster it's watching).
    """
    from app.integrations.system.host_fleet import evaluate_alert

    path = _write_hosts_md(tmp_path, HOSTS_MD_MIXED)
    client = _FakePulseClient({"test-node": "offline", "test-guest": "online"})

    result = evaluate_alert(db_session, registry_path=path, pulse_client=client)

    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["test-node"]["status"] == "dead"
    assert by_name["test-guest"]["status"] == "ok"
    assert result["issues"]
    assert "test-node" in result["issues"][0]


def test_pulse_host_not_reported_at_all_is_unknown(db_session, tmp_path):
    """Pulse's response simply not naming a host (renumbered, not yet
    enrolled, etc) must not be misread as "online".
    """
    from app.integrations.system.host_fleet import evaluate_alert

    path = _write_hosts_md(tmp_path, HOSTS_MD_MIXED)
    client = _FakePulseClient({"test-node": "online"})  # test-guest absent

    result = evaluate_alert(db_session, registry_path=path, pulse_client=client)

    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["test-guest"]["status"] == "unknown"


def test_system_alerts_pushes_a_dead_host(db_session, tmp_path, monkeypatch):
    from app.integrations.system import host_fleet
    from app.integrations.system.tools import handle_alerts_household

    path = _write_hosts_md(tmp_path, HOSTS_MD_MIXED)
    monkeypatch.setattr(host_fleet, "_hosts_md_path", lambda: path)
    monkeypatch.setattr(
        host_fleet,
        "_pulse_client",
        lambda: _FakePulseClient({"test-node": "offline", "test-guest": "online"}),
    )

    payload = json.loads(handle_alerts_household(db_session, {}))

    assert payload["status"] == "degraded"
    entry = next((a for a in payload["alerts"] if a["integration"] == "host_fleet"), None)
    assert entry is not None
    assert "dead" in entry["issues"][0]
    by_name = {h["name"]: h for h in payload["host_fleet"]["hosts"]}
    assert by_name["test-node"]["status"] == "dead"


# ---------------------------------------------------------------------------
# 4. `ha_entity` probe path
# ---------------------------------------------------------------------------


def test_ha_entity_probe_unavailable_is_dead(db_session, tmp_path):
    from app.integrations.system.host_fleet import evaluate_alert

    path = _write_hosts_md(tmp_path, HOSTS_MD_MIXED)
    _seed_entity(db_session, "sensor.test_pi_temperature", state="unavailable")
    client = _FakePulseClient({"test-node": "online", "test-guest": "online"})

    result = evaluate_alert(db_session, registry_path=path, pulse_client=client)

    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["test-pi"]["status"] == "dead"
    assert result["issues"]


def test_ha_entity_probe_present_is_ok(db_session, tmp_path):
    """No `last_updated` recorded (a row predating that column, or a write
    path that hasn't backfilled it) means staleness can't be judged — that
    falls through to `ok`, not `unknown`: the entity itself is known and
    reporting a real value, which is a different claim from rule 2's "no
    probe data at all".
    """
    from app.integrations.system.host_fleet import evaluate_alert

    path = _write_hosts_md(tmp_path, HOSTS_MD_MIXED)
    _seed_entity(db_session, "sensor.test_pi_temperature", state="21.4")
    client = _FakePulseClient({"test-node": "online", "test-guest": "online"})

    result = evaluate_alert(db_session, registry_path=path, pulse_client=client)

    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["test-pi"]["status"] == "ok"


def test_ha_entity_probe_missing_row_is_unknown(db_session, tmp_path):
    """Deliberately no HAEntity row seeded — never synced yet, never
    `ok` (rule 2).
    """
    from app.integrations.system.host_fleet import evaluate_alert

    path = _write_hosts_md(tmp_path, HOSTS_MD_MIXED)
    client = _FakePulseClient({"test-node": "online", "test-guest": "online"})

    result = evaluate_alert(db_session, registry_path=path, pulse_client=client)

    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["test-pi"]["status"] == "unknown"


# ---------------------------------------------------------------------------
# 5. `none` probe always reports unknown
# ---------------------------------------------------------------------------


def test_no_probe_host_always_unknown(db_session, tmp_path):
    from app.integrations.system.host_fleet import evaluate_alert

    path = _write_hosts_md(tmp_path, HOSTS_MD_MIXED)
    client = _FakePulseClient({"test-node": "online", "test-guest": "online"})

    result = evaluate_alert(db_session, registry_path=path, pulse_client=client)

    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["test-unwatched"]["status"] == "unknown"
    assert by_name["test-unwatched"]["detail"] == "no probe configured"


def test_system_alerts_stays_clean_for_a_live_fleet(db_session, tmp_path, monkeypatch):
    from app.integrations.system import host_fleet
    from app.integrations.system.tools import handle_alerts_household

    path = _write_hosts_md(tmp_path, HOSTS_MD_MIXED)
    monkeypatch.setattr(host_fleet, "_hosts_md_path", lambda: path)
    monkeypatch.setattr(
        host_fleet,
        "_pulse_client",
        lambda: _FakePulseClient({"test-node": "online", "test-guest": "online"}),
    )
    _seed_entity(db_session, "sensor.test_pi_temperature", state="21.4")

    payload = json.loads(handle_alerts_household(db_session, {}))

    entry = next((a for a in payload["alerts"] if a["integration"] == "host_fleet"), None)
    assert entry is None
    assert payload["host_fleet"]["registry_error"] is None
    by_name = {h["name"]: h for h in payload["host_fleet"]["hosts"]}
    assert by_name["test-node"]["status"] == "ok"
    # test-unwatched is unknown but must not itself alert.
    assert by_name["test-unwatched"]["status"] == "unknown"


# ---------------------------------------------------------------------------
# 6. `ha_entity` probe: stale `last_updated` (issue #167)
# ---------------------------------------------------------------------------


def test_ha_entity_probe_fresh_last_updated_is_ok(db_session, tmp_path):
    from app.integrations.system.host_fleet import evaluate_alert

    path = _write_hosts_md(tmp_path, HOSTS_MD_MIXED)
    fresh = datetime.now(timezone.utc) - timedelta(minutes=5)
    _seed_entity(db_session, "sensor.test_pi_temperature", state="21.4", last_updated=fresh)
    client = _FakePulseClient({"test-node": "online", "test-guest": "online"})

    result = evaluate_alert(db_session, registry_path=path, pulse_client=client)

    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["test-pi"]["status"] == "ok"
    assert result["issues"] == []


def test_ha_entity_probe_stale_last_updated_is_dead(db_session, tmp_path):
    """The core case issue #167 exists for: `sensor.shed_cam_temperature`
    kept reporting a plausible number (24.5) for nine days after its
    publisher died — `state == "unavailable"` never fired because the
    value itself never became unavailable, only stopped changing. Staleness
    must be judged on HA's own `last_updated`, not on whether the value is
    a number.
    """
    from app.integrations.system.host_fleet import evaluate_alert

    path = _write_hosts_md(tmp_path, HOSTS_MD_MIXED)
    stale = datetime.now(timezone.utc) - timedelta(days=9)
    _seed_entity(db_session, "sensor.test_pi_temperature", state="24.5", last_updated=stale)
    client = _FakePulseClient({"test-node": "online", "test-guest": "online"})

    result = evaluate_alert(db_session, registry_path=path, pulse_client=client)

    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["test-pi"]["status"] == "dead"
    assert "stale" in by_name["test-pi"]["detail"]
    assert result["issues"]
    assert "test-pi" in result["issues"][0]


def test_ha_entity_probe_stale_threshold_is_configurable(db_session, tmp_path, monkeypatch):
    """`system.host_fleet_stale_minutes` is the knob, not a hardcoded window
    — a shorter configured threshold should flag a gap the default (60m)
    would tolerate.
    """
    from app.integrations.system import host_fleet
    from app.integrations.system.host_fleet import evaluate_alert

    path = _write_hosts_md(tmp_path, HOSTS_MD_MIXED)
    age = datetime.now(timezone.utc) - timedelta(minutes=20)
    _seed_entity(db_session, "sensor.test_pi_temperature", state="21.4", last_updated=age)
    client = _FakePulseClient({"test-node": "online", "test-guest": "online"})

    # Default (60m) tolerates a 20m-old reading.
    result = evaluate_alert(db_session, registry_path=path, pulse_client=client)
    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["test-pi"]["status"] == "ok"

    # A 10m threshold must not.
    monkeypatch.setattr(host_fleet, "_ha_entity_stale_minutes", lambda: 10)
    result = evaluate_alert(db_session, registry_path=path, pulse_client=client)
    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["test-pi"]["status"] == "dead"


def test_ha_entity_probe_unknown_state_is_dead(db_session, tmp_path):
    """`unknown` is HA's own "no value at all" state — must alert exactly
    like `unavailable`, per the module docstring's rule.
    """
    from app.integrations.system.host_fleet import evaluate_alert

    path = _write_hosts_md(tmp_path, HOSTS_MD_MIXED)
    _seed_entity(db_session, "sensor.test_pi_temperature", state="unknown")
    client = _FakePulseClient({"test-node": "online", "test-guest": "online"})

    result = evaluate_alert(db_session, registry_path=path, pulse_client=client)

    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["test-pi"]["status"] == "dead"


# ── Pulse payload shape — the one thing the fakes above cannot catch ────────
#
# Every test above injects a fake client ABOVE the parser, which is exactly how
# the first deploy shipped reading an empty list: Pulse v6 wraps the resource
# list under `data`, the docs-derived code looked for `resources`, and every
# pulse-probed host said "not reported by pulse". This fixture mirrors the
# live 2026-09-08 response shape (synthetic ids), incl. the storage rows that
# reuse one name with two statuses.
_LIVE_SHAPED_PAYLOAD = {
    "data": [
        {"id": "agent-1", "type": "agent", "technology": "proxmox", "name": "test-node", "status": "online"},
        {"id": "agent-2", "type": "agent", "technology": "proxmox", "name": "test-node-2", "status": "offline"},
        {"id": "vm-1", "type": "vm", "technology": "qemu", "name": "test-guest", "status": "online"},
        {"id": "ct-1", "type": "system-container", "technology": "lxc", "name": "test-ct", "status": "offline"},
        {"id": "st-1", "type": "storage", "name": "test-node", "status": "offline"},
        {"id": "st-2", "type": "storage", "name": "backup-store", "status": "online"},
        {"id": "st-3", "type": "storage", "name": "backup-store", "status": "offline"},
        {"id": "pd-1", "type": "physical_disk", "name": "Samsung SSD 850", "status": "unknown"},
    ],
    "meta": {"page": 1, "limit": 100, "total": 8, "totalPages": 1},
    "aggregations": {},
}


def test_pulse_parse_reads_the_data_key_and_keeps_only_host_types():
    from app.integrations.system.host_fleet import PulseClient

    statuses = PulseClient.parse_statuses(_LIVE_SHAPED_PAYLOAD)
    assert statuses == {
        "test-node": "online",
        "test-node-2": "offline",
        "test-guest": "online",
        "test-ct": "offline",
    }
    # a storage row sharing a node's name must not overwrite the node's status
    assert statuses["test-node"] == "online"
    assert "backup-store" not in statuses


def test_pulse_parse_still_accepts_bare_list_and_resources_key():
    from app.integrations.system.host_fleet import PulseClient

    rows = [{"type": "vm", "name": "x", "status": "online"}]
    assert PulseClient.parse_statuses(rows) == {"x": "online"}
    assert PulseClient.parse_statuses({"resources": rows}) == {"x": "online"}
    assert PulseClient.parse_statuses({"unexpected": 1}) == {}


def test_pulse_match_uses_first_token_when_registry_name_has_parenthetical():
    from app.integrations.system.host_fleet import _status_for_pulse_host

    statuses = {"homeassistant": "running", "nas": "stopped", "turing": "online", "lovelace": "offline", "agent": "warning"}
    assert _status_for_pulse_host("homeassistant (VM 102)", statuses, None) == {"status": "ok", "detail": None}
    assert _status_for_pulse_host("nas (LXC 105)", statuses, None) == {"status": "dead", "detail": "pulse reports stopped"}
    assert _status_for_pulse_host("turing", statuses, None) == {"status": "ok", "detail": None}
    assert _status_for_pulse_host("lovelace", statuses, None) == {"status": "dead", "detail": "pulse reports offline"}
    # anything outside the runtime vocabulary is neither pass nor fail
    assert _status_for_pulse_host("agent (LXC 107)", statuses, None)["status"] == "unknown"
    assert _status_for_pulse_host("radio (LXC 106)", statuses, None)["detail"] == "not reported by pulse"


def test_pulse_parse_reads_api_state_resources_with_runtime_vocabulary():
    """/api/state (the endpoint now used): `resources` key, runtime statuses."""
    from app.integrations.system.host_fleet import PulseClient

    payload = {
        "activeAlerts": [{"type": "powered-off", "resourceName": "test-ct"}],
        "resources": [
            {"type": "agent", "name": "test-node", "status": "online"},
            {"type": "vm", "name": "test-guest", "status": "running"},
            {"type": "system-container", "name": "test-ct", "status": "stopped"},
            {"type": "storage", "name": "test-node", "status": "offline"},
        ],
    }
    assert PulseClient.parse_statuses(payload) == {"test-node": "online", "test-guest": "running", "test-ct": "stopped"}
