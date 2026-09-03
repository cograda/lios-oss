"""F11b (tranche1-hardening): daemon silent + task-unhealthy alert axis.

`system_alerts` axis 5 reads `client_tokens.client_version`/`task_health`
(populated by the heartbeat handler in `app/api/v1.py`) and flags:
  - "daemon silent" when an active daemon token hasn't been seen in
    DAEMON_SILENT_MINUTES
  - "task <name> unhealthy" when task_health shows restarts over threshold,
    a non-null last_error, or finished=true

Only tokens with a non-empty `client_version` are considered daemons — a
token with no version (e.g. a phone's Health Auto Export bearer) never
trips this axis, even if it goes quiet.

Reuses the axis-1 `_alert()`/`alerts_by_integration` sink (keyed on the
token's label instead of an integration name) so the same
`integration:{name}:{kind}` fingerprint path the notifications sweep
already builds from `payload["alerts"]` covers this axis too, with zero
changes to the notifications package. db tier — real client_tokens row.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.db


def _seed_daemon_token(
    session, *, label, user_id=1, last_seen_at=None, client_version="2.4.0",
    task_health=None,
):
    from app.models.clients import ClientToken

    token = ClientToken.for_token(
        user_id=user_id, token=f"tok-{label}", label=label,
    )
    token.client_version = client_version
    token.last_seen_at = last_seen_at
    token.task_health = json.dumps(task_health) if task_health is not None else None
    session.add(token)
    session.commit()
    return token


def test_silent_daemon_triggers_alert(db_session):
    from app.integrations.system.tools import handle_alerts

    now = datetime.now(timezone.utc)
    _seed_daemon_token(
        db_session, label="alex-macbook",
        # Well past the default cutoff (`system.daemon_silent_minutes`,
        # 540m/9h as of 2026-08-27 — raised from a hardcoded 20m because a
        # MacBook asleep for the night was indistinguishable from a dead
        # daemon at 20 minutes; see system/tools.py::_daemon_silent_minutes).
        last_seen_at=now - timedelta(hours=10),
    )

    payload = json.loads(handle_alerts(db_session, {}))
    entry = next((a for a in payload["alerts"] if a["integration"] == "alex-macbook"), None)
    assert entry is not None
    assert any("daemon silent" in issue for issue in entry["issues"])
    assert payload["status"] == "degraded"


def test_daemon_silent_minutes_is_config_driven(db_session, monkeypatch):
    """2026-08-27: the cutoff used to be the hardcoded `DAEMON_SILENT_MINUTES`
    constant. It's now `system.daemon_silent_minutes` (config_schema), with
    that constant kept only as the fallback for a bad config value — see
    `_daemon_silent_minutes()`."""
    from types import SimpleNamespace

    from app.integrations.system import tools as system_tools

    now = datetime.now(timezone.utc)
    _seed_daemon_token(
        db_session, label="tight-cutoff-daemon",
        last_seen_at=now - timedelta(minutes=10),
    )

    # Default (540m) does not flag a daemon quiet for only 10 minutes.
    payload = json.loads(system_tools.handle_alerts(db_session, {}))
    entry = next((a for a in payload["alerts"] if a["integration"] == "tight-cutoff-daemon"), None)
    assert entry is None or not any("daemon silent" in i for i in entry["issues"])

    # A tighter configured cutoff catches the same 10-minute silence.
    monkeypatch.setattr(
        system_tools, "plugin_config",
        lambda name: SimpleNamespace(daemon_silent_minutes=5),
    )
    payload = json.loads(system_tools.handle_alerts(db_session, {}))
    entry = next(a for a in payload["alerts"] if a["integration"] == "tight-cutoff-daemon")
    assert any("daemon silent" in i for i in entry["issues"])


def test_bad_daemon_silent_minutes_falls_back_to_the_constant(db_session, monkeypatch):
    from types import SimpleNamespace

    from app.integrations.system import tools as system_tools

    now = datetime.now(timezone.utc)
    _seed_daemon_token(
        db_session, label="junk-config-daemon",
        last_seen_at=now - timedelta(minutes=45),  # exceeds the fallback 20m
    )
    monkeypatch.setattr(
        system_tools, "plugin_config",
        lambda name: SimpleNamespace(daemon_silent_minutes="not-a-number"),
    )
    payload = json.loads(system_tools.handle_alerts(db_session, {}))
    entry = next(a for a in payload["alerts"] if a["integration"] == "junk-config-daemon")
    assert any("daemon silent" in i for i in entry["issues"])


def test_recently_seen_daemon_is_not_flagged_silent(db_session):
    from app.integrations.system.tools import handle_alerts

    now = datetime.now(timezone.utc)
    _seed_daemon_token(
        db_session, label="fresh-daemon",
        last_seen_at=now - timedelta(minutes=2),
    )

    payload = json.loads(handle_alerts(db_session, {}))
    entry = next((a for a in payload["alerts"] if a["integration"] == "fresh-daemon"), None)
    assert entry is None or not any("daemon silent" in issue for issue in entry["issues"])


def test_non_daemon_token_never_flagged_silent(db_session):
    """A token with no client_version (e.g. a phone bearer) is never
    considered a daemon, however stale it is."""
    from app.integrations.system.tools import handle_alerts

    now = datetime.now(timezone.utc)
    _seed_daemon_token(
        db_session, label="sams-phone",
        last_seen_at=now - timedelta(days=30),
        client_version=None,
    )

    payload = json.loads(handle_alerts(db_session, {}))
    assert not any(a["integration"] == "sams-phone" for a in payload["alerts"])


def test_task_with_excess_restarts_flagged_unhealthy(db_session):
    from app.integrations.system.tools import handle_alerts

    now = datetime.now(timezone.utc)
    _seed_daemon_token(
        db_session, label="restarty-daemon",
        last_seen_at=now,
        task_health={"vault_watcher": {"restarts": 7, "alive_seconds_ago": 3}},
    )

    payload = json.loads(handle_alerts(db_session, {}))
    entry = next(a for a in payload["alerts"] if a["integration"] == "restarty-daemon")
    assert any("vault_watcher" in issue and "unhealthy" in issue for issue in entry["issues"])
    assert payload["status"] == "degraded"


def test_task_with_last_error_flagged_unhealthy(db_session):
    from app.integrations.system.tools import handle_alerts

    now = datetime.now(timezone.utc)
    _seed_daemon_token(
        db_session, label="erroring-daemon",
        last_seen_at=now,
        task_health={"reminders_sync": {"restarts": 0, "last_error": "EventKit denied"}},
    )

    payload = json.loads(handle_alerts(db_session, {}))
    entry = next(a for a in payload["alerts"] if a["integration"] == "erroring-daemon")
    assert any("reminders_sync" in issue and "unhealthy" in issue for issue in entry["issues"])


def test_finished_task_flagged_unhealthy(db_session):
    from app.integrations.system.tools import handle_alerts

    now = datetime.now(timezone.utc)
    _seed_daemon_token(
        db_session, label="finished-task-daemon",
        last_seen_at=now,
        task_health={"one_shot": {"restarts": 0, "finished": True}},
    )

    payload = json.loads(handle_alerts(db_session, {}))
    entry = next(a for a in payload["alerts"] if a["integration"] == "finished-task-daemon")
    assert any("one_shot" in issue and "unhealthy" in issue for issue in entry["issues"])


def test_healthy_daemon_produces_no_task_issues(db_session):
    from app.integrations.system.tools import handle_alerts

    now = datetime.now(timezone.utc)
    _seed_daemon_token(
        db_session, label="healthy-daemon",
        last_seen_at=now,
        task_health={"vault_watcher": {"restarts": 1, "alive_seconds_ago": 5, "last_error": None, "finished": False}},
    )

    payload = json.loads(handle_alerts(db_session, {}))
    entry = next((a for a in payload["alerts"] if a["integration"] == "healthy-daemon"), None)
    assert entry is None or entry["issues"] == []


def test_bound_caller_sees_only_own_daemons(db_session):
    """An MCP caller (user bound) must not see another user's daemon —
    the unbound path (notifications sweep, dashboard) covers the whole
    fleet instead. The leak-canary in test_user_scoping.py enforces the
    same property indirectly; this pins it directly."""
    from app.auth.context import use_user
    from app.integrations.system.tools import handle_alerts

    _seed_daemon_token(db_session, label="other-users-daemon", user_id=2, last_seen_at=None)

    with use_user(1):
        payload = json.loads(handle_alerts(db_session, {}))
    assert not any(a["integration"] == "other-users-daemon" for a in payload["alerts"])

    # conftest's autouse `_pin_test_user` binds user 1 for every test —
    # `use_user(0)` is the documented sentinel for the genuinely-unbound
    # state (how the notifications sweep and dashboard route call this).
    with use_user(0):
        payload = json.loads(handle_alerts(db_session, {}))
    assert any(a["integration"] == "other-users-daemon" for a in payload["alerts"])


# ─── axis 7: the host's disk ───────────────────────────────────────────────


def _fake_usage(pct):
    import collections
    T = collections.namedtuple("usage", "total used free")
    total = 61_000_000_000
    return T(total, int(total * pct / 100), int(total * (100 - pct) / 100))


def test_a_nearly_full_disk_is_a_household_alert(db_session, monkeypatch):
    """2026-09-02: 100 % → Postgres PANIC → two minutes of 'connection failed'
    that read as a network fault. The alert exists so the next one is heard
    at 85 %, with the consequence spelled out."""
    import shutil
    from app.integrations.system.tools import handle_alerts

    monkeypatch.setattr(shutil, "disk_usage", lambda p: _fake_usage(92))
    out = json.loads(handle_alerts(db_session, {}))
    host = next(a for a in out["alerts"] if a["integration"] == "host")
    assert "92% used" in host["issues"][0] and "Postgres" in host["issues"][0]
    assert out["status"] == "degraded"


def test_a_healthy_disk_raises_nothing(db_session, monkeypatch):
    import shutil
    from app.integrations.system.tools import handle_alerts

    monkeypatch.setattr(shutil, "disk_usage", lambda p: _fake_usage(58))
    out = json.loads(handle_alerts(db_session, {}))
    assert all(a["integration"] != "host" for a in out["alerts"])
