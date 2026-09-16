"""F11a + P3 (tranche1-hardening): heartbeat token targeting + write throttle.

F11a: `GET /api/v1/heartbeat` used to write client_version/task_health onto
the calling user's *most-recently-seen active* `client_tokens` row, not the
row that actually authenticated the request — a race against the user's
other tokens (e.g. a phone's Health Auto Export push touching last_seen_at
between the auth check and the write). The fix threads the authenticated
token's id through via `User.client_token_id` (see
`app.auth.client_token.client_token_id_of`) and writes onto that row by id.

P3: `resolve_token_to_user` used to write+commit last_seen_at/expires_at on
every single authenticated request — the hot path for every MCP/API call.
It's now throttled to at most once per `LAST_SEEN_WRITE_THROTTLE` (60s).

Unit tier throughout — no real Postgres, a small hand-rolled fake session
standing in for `get_db()`.
"""

import contextlib
from datetime import datetime, timedelta, timezone

import pytest

from app.models.users import User


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakeDb:
    def __init__(self, session):
        self._session = session

    def session(self):
        @contextlib.contextmanager
        def _cm():
            yield self._session

        return _cm()


def _user_with_token(token_id):
    u = User(id=1, name="alex", display_name="Alex")
    if token_id is not None:
        u.client_token_id = token_id
    return u


# ---------------------------------------------------------------------------
# F11a — heartbeat writes to the authenticated token, not "most recent"
# ---------------------------------------------------------------------------


class _FakeTokenRow:
    def __init__(self, id):
        self.id = id
        self.client_version = None
        self.task_health = None


class _ByIdSession:
    """Fake session whose ClientToken query only ever resolves by id — no
    `order_by(last_seen_at.desc())`/"most recent" query is possible here."""

    def __init__(self, rows: dict[int, _FakeTokenRow]):
        self._rows = rows
        self.commits = 0
        self.queried_ids: list[int] = []

    def query(self, model):
        session = self

        class _Query:
            def filter_by(self, **kwargs):
                token_id = kwargs["id"]
                session.queried_ids.append(token_id)
                self._match = session._rows.get(token_id)
                return self

            def first(self):
                return self._match

        return _Query()

    def commit(self):
        self.commits += 1


def test_heartbeat_writes_to_authenticated_token_row(monkeypatch):
    from app.api import v1 as v1_mod

    authenticated_row = _FakeTokenRow(id=7)
    other_users_more_recent_row = _FakeTokenRow(id=99)
    session = _ByIdSession({7: authenticated_row, 99: other_users_more_recent_row})
    monkeypatch.setattr(v1_mod, "get_db", lambda: _FakeDb(session))

    user = _user_with_token(7)
    result = v1_mod.heartbeat(client_version="9.9.9", task_health='{"a": 1}', user=user)

    assert session.queried_ids == [7]
    assert authenticated_row.client_version == "9.9.9"
    assert authenticated_row.task_health == '{"a": 1}'
    assert other_users_more_recent_row.client_version is None
    assert session.commits == 1
    assert result["ok"] is True


def test_heartbeat_skips_write_when_no_client_token_row(monkeypatch):
    """OAuth-authenticated sessions carry no `client_tokens` row at all —
    `client_token_id_of` returns None, and the handler must skip the write
    rather than guess or raise."""
    from app.api import v1 as v1_mod

    def _boom():
        raise AssertionError("get_db must not be touched with no token id")

    monkeypatch.setattr(v1_mod, "get_db", _boom)

    user = _user_with_token(None)
    result = v1_mod.heartbeat(client_version="1.0.0", task_health="{}", user=user)

    assert result["ok"] is True


def test_heartbeat_noop_write_skips_db_entirely(monkeypatch):
    """No client_version and no task_health in the request → nothing to
    write, so the handler shouldn't touch the DB even with a real token id."""
    from app.api import v1 as v1_mod

    def _boom():
        raise AssertionError("get_db must not be touched when there's nothing to write")

    monkeypatch.setattr(v1_mod, "get_db", _boom)

    user = _user_with_token(7)
    result = v1_mod.heartbeat(client_version="", task_health="", user=user)

    assert result["ok"] is True


# ---------------------------------------------------------------------------
# P3 — throttle the sliding last_seen_at/expires_at write
# ---------------------------------------------------------------------------


class _FakeClientTokenRow:
    def __init__(self, *, user_id, last_seen_at, expires_at=None, scope="full"):
        self.id = 1
        self.user_id = user_id
        self.last_seen_at = last_seen_at
        self.expires_at = expires_at
        self.is_active = True
        # NOT NULL, server_default 'full' — a real row always has it.
        self.scope = scope


class _FakeUserRow:
    def __init__(self, id, name="alex", display_name="Alex", is_active=True, is_admin=False):
        self.id = id
        self.is_admin = is_admin
        self.name = name
        self.display_name = display_name
        # `resolve_token_to_user` refuses a deactivated user's bearer
        # (2026-09-06 scoping audit) — a real User row always has this column.
        self.is_active = is_active


class _ResolveSession:
    """Fake session backing `resolve_token_to_user`: one ClientToken row,
    one User row, tracking whether commit() was ever called."""

    def __init__(self, token_row, user_row):
        self._token_row = token_row
        self._user_row = user_row
        self.commits = 0

    def query(self, model):
        from app.models.clients import ClientToken
        from app.models.users import User as UserModel

        session = self

        class _Query:
            def filter_by(self, **kwargs):
                if model is ClientToken:
                    self._result = session._token_row
                elif model is UserModel:
                    self._result = session._user_row
                else:
                    self._result = None
                return self

            def first(self):
                return self._result

        return _Query()

    def commit(self):
        self.commits += 1


def _patch_resolve(monkeypatch, session):
    import app.auth.client_token as ct_mod

    monkeypatch.setattr(ct_mod, "get_db", lambda: _FakeDb(session))
    monkeypatch.setattr(ct_mod, "hash_token", lambda token: "irrelevant-hash")


def test_throttle_skips_write_within_window(monkeypatch):
    from app.auth.client_token import resolve_token_to_user

    now = datetime.now(timezone.utc)
    token_row = _FakeClientTokenRow(user_id=1, last_seen_at=now - timedelta(seconds=5))
    user_row = _FakeUserRow(id=1)
    session = _ResolveSession(token_row, user_row)
    _patch_resolve(monkeypatch, session)

    before = token_row.last_seen_at
    result = resolve_token_to_user("some-token")

    assert result is not None
    assert result.id == 1
    assert result.client_token_id == token_row.id
    # Within the 60s throttle window — last_seen_at must be untouched.
    assert token_row.last_seen_at == before


def test_throttle_writes_after_window_elapses(monkeypatch):
    from app.auth.client_token import resolve_token_to_user

    now = datetime.now(timezone.utc)
    token_row = _FakeClientTokenRow(user_id=1, last_seen_at=now - timedelta(seconds=120))
    user_row = _FakeUserRow(id=1)
    session = _ResolveSession(token_row, user_row)
    _patch_resolve(monkeypatch, session)

    result = resolve_token_to_user("some-token")

    assert result is not None
    assert result.client_token_id == token_row.id
    # Past the throttle window — must have been bumped forward.
    assert token_row.last_seen_at > now - timedelta(seconds=5)
    assert session.commits == 1


def test_throttle_writes_when_last_seen_at_is_null(monkeypatch):
    """A brand-new token (never seen) must get its first last_seen_at write
    unconditionally — there's nothing to throttle against yet."""
    from app.auth.client_token import resolve_token_to_user

    token_row = _FakeClientTokenRow(user_id=1, last_seen_at=None)
    user_row = _FakeUserRow(id=1)
    session = _ResolveSession(token_row, user_row)
    _patch_resolve(monkeypatch, session)

    resolve_token_to_user("some-token")

    assert token_row.last_seen_at is not None


# ---------------------------------------------------------------------------
# F11b — fingerprint stability (the notifications sweep's own _issue_kind,
# exercised against the exact issue strings system/tools.py's new daemon
# axis emits). Pure function, no DB — belongs in the unit tier even though
# the full alert-axis behaviour is covered by db-tier tests elsewhere.
# ---------------------------------------------------------------------------


def test_daemon_alert_issue_kind_ignores_rendered_ages():
    """Fingerprints must key on issue kind + token label, never on rendered
    text with ages in it — a stale-but-unresolved daemon must keep the same
    fingerprint sweep over sweep, or the notifications ledger would never
    dedupe it and it would re-notify every 15 minutes forever."""
    from app.integrations.notifications.sweep import _issue_kind

    assert _issue_kind("daemon silent (last seen 25m ago)") == _issue_kind(
        "daemon silent (last seen 3h 2m ago)"
    )
    assert _issue_kind("daemon silent (never seen)") == _issue_kind(
        "daemon silent (last seen 25m ago)"
    )


def test_task_unhealthy_issue_kind_stable_across_restart_counts():
    from app.integrations.notifications.sweep import _issue_kind

    assert _issue_kind("task vault_watcher unhealthy (restarts=4)") == _issue_kind(
        "task vault_watcher unhealthy (restarts=97)"
    )


def test_resolved_user_carries_is_admin(monkeypatch):
    """`/api/auth/login` answers from this object. On 2026-09-06 the DB said
    Alex was admin and the login response said he was not, because the
    snapshot taken before commit omitted the flag — and the dashboard hid
    every admin control from him."""
    from app.auth.client_token import resolve_token_to_user

    now = datetime.now(timezone.utc)
    for flag in (True, False):
        token_row = _FakeClientTokenRow(user_id=1, last_seen_at=now)
        session = _ResolveSession(token_row, _FakeUserRow(id=1, is_admin=flag))
        _patch_resolve(monkeypatch, session)
        result = resolve_token_to_user("some-token")
        assert result is not None and result.is_admin is flag


# ---------------------------------------------------------------------------
# Token scope (2026-09-07) — `readonly` rides on the resolved User and gates
# HTTP methods in `get_current_user`. The tool half lives in test_dispatch.py.
# ---------------------------------------------------------------------------


def test_resolved_user_carries_scope(monkeypatch):
    """Same trap as `is_admin` above: the snapshot is taken before commit, and
    a field left out of it silently reads as its default — which for scope
    would be `full`, i.e. a read-only device token quietly promoted."""
    from app.auth.client_token import client_token_scope_of, resolve_token_to_user

    now = datetime.now(timezone.utc)
    for scope in ("readonly", "full"):
        token_row = _FakeClientTokenRow(user_id=1, last_seen_at=now, scope=scope)
        session = _ResolveSession(token_row, _FakeUserRow(id=1))
        _patch_resolve(monkeypatch, session)
        result = resolve_token_to_user("some-token")
        assert result is not None
        assert client_token_scope_of(result) == scope


def test_scope_of_defaults_to_full_when_unset():
    """OAuth-resolved sessions never set it — a person's own connector is
    not a restricted device."""
    from app.auth.client_token import client_token_scope_of

    assert client_token_scope_of(User(id=1, name="alex", display_name="Alex")) == "full"


def _scoped_user(scope):
    u = User(id=3, name="agent", display_name="Agent")
    u.client_token_id = 42
    u.client_token_scope = scope
    return u


@pytest.mark.parametrize("method,path,allowed", [
    ("GET", "/api/v1/tools", True),
    ("GET", "/api/v1/heartbeat", True),          # writes only its own token row
    ("GET", "/api/v1/instructions", True),
    ("POST", "/api/v1/tools/calendar_today", True),
    ("POST", "/api/v1/tools/", False),           # no tool name — not a tool call
    ("POST", "/api/v1/tools", False),
    ("POST", "/api/v1/vault/push", False),
    ("POST", "/api/v1/reminders/push", False),
    ("POST", "/api/v1/reminders/verified", False),
    ("POST", "/api/inbox/ingest", False),
    ("POST", "/api/health/push", False),
    ("PUT", "/api/v1/anything", False),
    ("DELETE", "/api/v1/anything", False),
    ("PATCH", "/api/v1/tools/calendar_today", False),
])
def test_readonly_http_refusal_rule(method, path, allowed):
    from app.auth.client_token import readonly_http_refusal

    refusal = readonly_http_refusal(_scoped_user("readonly"), method, path)
    assert (refusal is None) is allowed, (method, path, refusal)
    if refusal is not None:
        assert "read-only token" in refusal and path in refusal
    # A full-scope token is never refused here, whatever the method.
    assert readonly_http_refusal(_scoped_user("full"), method, path) is None


def _request(method, path):
    from fastapi import Request

    return Request({
        "type": "http", "method": method, "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": [], "scheme": "http",
        "server": ("testserver", 80), "client": ("10.0.0.9", 1234), "root_path": "",
    })


def _patch_dependency(monkeypatch, scope):
    import app.auth.client_token as ct_mod

    monkeypatch.setattr(ct_mod, "is_over_limit", lambda ip: False)
    monkeypatch.setattr(ct_mod, "resolve_token_to_user", lambda token: _scoped_user(scope))


def test_get_current_user_403s_a_readonly_write(monkeypatch):
    from fastapi import HTTPException

    from app.auth.client_token import get_current_user

    _patch_dependency(monkeypatch, "readonly")
    with pytest.raises(HTTPException) as exc:
        get_current_user("Bearer x", _request("POST", "/api/v1/vault/push"))
    assert exc.value.status_code == 403
    assert "read-only token" in exc.value.detail


def test_get_current_user_lets_a_readonly_read_and_tool_call_through(monkeypatch):
    from app.auth.client_token import get_current_user

    _patch_dependency(monkeypatch, "readonly")
    assert get_current_user("Bearer x", _request("GET", "/api/v1/tools")).id == 3
    assert get_current_user("Bearer x", _request("GET", "/api/v1/heartbeat")).id == 3
    assert get_current_user("Bearer x", _request("POST", "/api/v1/tools/vault_search")).id == 3


def test_get_current_user_full_scope_unchanged(monkeypatch):
    from app.auth.client_token import get_current_user

    _patch_dependency(monkeypatch, "full")
    assert get_current_user("Bearer x", _request("POST", "/api/v1/vault/push")).id == 3
