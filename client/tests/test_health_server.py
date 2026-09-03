"""Unit tests for the minimal health app (health_server.py).

Phase 4 (2026-07-14): the daemon is no longer an MCP server. This is all
that's left listening on the old mcp_port — a single /health route for
`lios-sync status` (and anyone else) to check the daemon is alive.
"""

from starlette.testclient import TestClient

from lios_sync import __version__
from lios_sync.health_server import create_health_app


class StubVaultHandler:
    def __init__(self, retry_queue_depth=0):
        self.retry_queue_depth = retry_queue_depth


class StubSupervisor:
    def __init__(self, health=None):
        self._health = health or {}

    def health(self):
        return self._health


def test_health_reports_version_and_defaults():
    app = create_health_app()
    client = TestClient(app)

    resp = client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["version"] == __version__
    assert data["vault_watcher_active"] is False
    assert data["retry_queue_depth"] == 0
    assert data["tasks"] == {}


def test_health_reports_vault_watcher_state():
    app = create_health_app(vault_handler=StubVaultHandler(retry_queue_depth=3))
    client = TestClient(app)

    data = client.get("/health").json()

    assert data["vault_watcher_active"] is True
    assert data["retry_queue_depth"] == 3


def test_health_reports_task_liveness_from_supervisor():
    tasks = {"heartbeat": {"alive_seconds_ago": 1.2, "restarts": 0, "last_error": None, "finished": False}}
    app = create_health_app(supervisor=StubSupervisor(health=tasks))
    client = TestClient(app)

    data = client.get("/health").json()

    assert data["tasks"] == tasks


def test_health_only_exposes_the_health_route():
    app = create_health_app()
    client = TestClient(app)

    resp = client.get("/mcp")

    assert resp.status_code == 404
