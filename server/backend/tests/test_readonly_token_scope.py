"""Read-only bearer scope, end to end on the real app (2026-09-07). db tier.

`client_tokens.scope = 'readonly'` is the credential for a device that may
look but never touch — the Hall Panel's BFF (`apps/hub`) is the first. Its
docs had claimed a capability whitelist "enforced inside comar's registry
dispatch"; nothing enforced it, and the 2026-09-06 scoping audit recorded
that as "recorded, not fixed". These tests drive the real routes with real
rows: minting with a scope, what the token list shows, and what a read-only
bearer can and cannot do over HTTP, over the tool endpoint, and at the
dashboard's front door. The unit-tier halves are in
test_client_token_targeting.py (resolver + HTTP rule) and test_dispatch.py
(tool rule).
"""

import base64

import pytest
from fastapi.testclient import TestClient

from app.models.clients import ClientToken

pytestmark = pytest.mark.db


def _mint(real_db, user_id: int, scope: str, label: str = "scope-test") -> tuple[int, str]:
    with real_db.session() as session:
        row, plaintext = ClientToken.mint(user_id=user_id, label=label, scope=scope)
        session.add(row)
        session.commit()
        return row.id, plaintext


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(real_db):
    from app.main import app

    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def admin(real_db):
    """A signed-in admin dashboard session (alex, user 1)."""
    from app.main import app

    _, bearer = _mint(real_db, 1, "full", label="admin-session")
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/api/auth/login", json={"token": bearer}).status_code == 200
    return c


@pytest.fixture
def probe_tools(monkeypatch):
    """Two probe tools on the live registry: one read-only, one not."""
    from app.plugin import registry

    monkeypatch.setattr(registry, "tool_handlers", dict(registry.tool_handlers))
    monkeypatch.setattr(registry, "tool_metadata", dict(registry.tool_metadata))
    ran: list[str] = []

    def _handler(name):
        def h(session, arguments):
            ran.append(name)
            return {"tool": name}
        return h

    registry.tool_handlers["probe_read"] = (_handler("probe_read"), "probe")
    registry.tool_metadata["probe_read"] = {"annotations": {"readOnlyHint": True}}
    registry.tool_handlers["probe_write"] = (_handler("probe_write"), "probe")
    registry.tool_metadata["probe_write"] = {"annotations": {"readOnlyHint": False}}
    return ran


# --- minting ----------------------------------------------------------------

def test_admin_mints_a_readonly_token_and_the_list_shows_its_scope(real_db, admin):
    resp = admin.post(
        "/api/auth/clients", json={"user": "sam", "label": "hall-panel", "scope": "readonly"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["scope"] == "readonly"

    with real_db.session() as session:
        assert session.get(ClientToken, body["id"]).scope == "readonly"

    listed = {c["id"]: c for c in admin.get("/api/auth/clients").json()["clients"]}
    assert listed[body["id"]]["scope"] == "readonly"
    # Every token minted before scope existed — and the default now — is `full`.
    assert {c["scope"] for c in listed.values() if c["id"] != body["id"]} == {"full"}


def test_mint_defaults_to_full_scope(admin):
    resp = admin.post("/api/auth/clients", json={"user": "sam", "label": "phone"})
    assert resp.status_code == 201 and resp.json()["scope"] == "full"


@pytest.mark.parametrize("bad", ["read", "READ_ONLY", "admin", "write", "*"])
def test_mint_rejects_an_unknown_scope(admin, bad):
    resp = admin.post(
        "/api/auth/clients", json={"user": "sam", "label": "x", "scope": bad},
    )
    assert resp.status_code == 400
    assert "scope" in resp.json()["error"]


# --- what a read-only bearer can do -----------------------------------------

def test_readonly_bearer_may_read(real_db, client):
    _, ro = _mint(real_db, 2, "readonly")
    assert client.get("/api/v1/tools", headers=_bearer(ro)).status_code == 200
    # Heartbeat writes client_version onto its own token row only — allowed.
    resp = client.get("/api/v1/heartbeat?client_version=0.0.1", headers=_bearer(ro))
    assert resp.status_code == 200 and resp.json()["ok"] is True


def test_readonly_bearer_is_403_on_every_write_route(real_db, client):
    _, ro = _mint(real_db, 2, "readonly")
    h = _bearer(ro)

    resp = client.post("/api/v1/vault/push", headers=h, json={"path": "x.md", "content": "hi"})
    assert resp.status_code == 403 and "read-only token" in resp.json()["detail"]

    resp = client.post("/api/v1/reminders/verified", headers=h)
    assert resp.status_code == 403

    # Inbox ingest has its own bearer check, not `get_current_user` — the
    # refusal must reach it too.
    payload = {"data": base64.b64encode(b"hello").decode(), "filename": "note.txt", "type": "text"}
    resp = client.post("/api/inbox/ingest", headers=h, json=payload)
    assert resp.status_code == 403 and "read-only token" in resp.json()["detail"]


def test_full_bearer_is_never_403_on_those_routes(real_db, client):
    """The existing daemon's authority is unchanged — these may fail for
    other reasons in a bare test environment (no vault mount), but never
    because of scope."""
    _, full = _mint(real_db, 2, "full")
    h = _bearer(full)
    assert client.post(
        "/api/v1/vault/push", headers=h, json={"path": "x.md", "content": "hi"},
    ).status_code != 403
    assert client.post("/api/v1/reminders/verified", headers=h).status_code == 200


def test_readonly_bearer_tool_calls_are_gated_by_readonlyhint(real_db, client, probe_tools):
    _, ro = _mint(real_db, 2, "readonly")
    h = _bearer(ro)

    ok = client.post("/api/v1/tools/probe_read", headers=h, json={})
    assert ok.status_code == 200 and ok.json() == {"ok": True, "result": {"tool": "probe_read"}}

    refused = client.post("/api/v1/tools/probe_write", headers=h, json={})
    assert refused.status_code == 500
    assert refused.json()["ok"] is False
    assert "read-only token" in refused.json()["error"]
    assert probe_tools == ["probe_read"], "the write handler must not have run"


def test_full_bearer_calls_the_write_tool(real_db, client, probe_tools):
    _, full = _mint(real_db, 2, "full")
    resp = client.post("/api/v1/tools/probe_write", headers=_bearer(full), json={})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert probe_tools == ["probe_write"]


def test_readonly_bearer_cannot_open_a_dashboard_session(real_db, client):
    _, ro = _mint(real_db, 2, "readonly")
    resp = client.post("/api/auth/login", json={"token": ro})
    assert resp.status_code == 403
    assert "read-only" in resp.json()["error"]
    # No cookie, no session: a gated route is still a 401.
    assert client.get("/api/auth/clients").status_code == 401
