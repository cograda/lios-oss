"""Tests for `system_daily_brief` (app.integrations.system.brief).

The first test in this file is the one that matters. The brief fans out across
a thread pool, and `current_user_id()` is a ContextVar — which
`ThreadPoolExecutor` does **not** propagate to its workers. If a worker failed
to re-pin the user, every per-user query inside it would be scoped to whatever
the worker's context happened to hold. That is the same class of bug as the
`whatsapp_stats` aggregate leak, except spread across 20 sources at once.

The rest cover the cache contract, which has one rule that is easy to get
wrong and expensive when you do: a **volatile** source (departure boards, home
status) must never be written to or read from the cache, because stale trains
are worse than no trains.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import date

import pytest

from app.auth.context import current_user_id, use_user
from app.integrations.system import brief
from app.services import preferences as prefs_service

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubSession:
    """Stands in for a SQLAlchemy session; identity is all we assert on."""

    def __init__(self, label: str) -> None:
        self.label = label


class _Recorder:
    """Captures what each source saw when it ran."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.sessions: list = []
        self.lock = threading.Lock()

    def record(self, **kw) -> None:
        with self.lock:
            self.calls.append(kw)

    def keys_called(self) -> list[str]:
        return [c["method"] for c in self.calls]


def _install_stubs(
    monkeypatch,
    recorder: _Recorder,
    *,
    has_data: bool = True,
    failing_methods: set[str] | None = None,
    prefs: dict | None = None,
):
    """Point the brief at fake capabilities, sessions and preferences."""
    failing_methods = failing_methods or set()

    class _Facade:
        def __init__(self, capability: str) -> None:
            self._capability = capability

        def has_data(self, session, user_id):  # noqa: ANN001
            return has_data

        def __getattr__(self, method):  # noqa: ANN001
            def _call(session, arguments):
                # The two things a worker must get right, captured at the
                # moment of the call rather than inferred afterwards.
                recorder.record(
                    capability=self._capability,
                    method=method,
                    user_id=current_user_id(),
                    session_id=id(session),
                    arguments=arguments,
                )
                if method in failing_methods:
                    raise RuntimeError(f"{method} is broken")
                return json.dumps({"ok": method})

            return _call

    monkeypatch.setattr(brief, "get_capability", lambda cap: _Facade(cap))

    class _FakeDb:
        @contextmanager
        def session(self):
            s = _StubSession("worker")
            # Held for the lifetime of the test on purpose. `id()` is a memory
            # address in CPython, and a session collected the instant its
            # with-block exits gets its address recycled by the next one —
            # which would make an id-uniqueness assertion pass or fail on
            # allocator behaviour rather than on session sharing.
            recorder.sessions.append(s)
            yield s

    monkeypatch.setattr("app.db.get_db", lambda: _FakeDb())

    resolved = {k: v.default for k, v in prefs_service.PREFERENCES.items()}
    resolved.update(prefs or {})
    monkeypatch.setattr(prefs_service, "get_all", lambda session, uid: resolved)

    brief.clear_cache()
    return resolved


FRIDAY = date(2026, 8, 7)


# ---------------------------------------------------------------------------
# The important one
# ---------------------------------------------------------------------------


def test_every_worker_sees_the_requested_user_not_the_ambient_one(monkeypatch):
    """A pool worker must re-pin `use_user`, not inherit or default.

    conftest's autouse fixture pins user 1 for the whole test. We ask for a
    brief for user **2**. Thread-pool workers start with a fresh context, so
    an implementation that forgot `use_user` would either raise (the
    sentinel-0 guard) or read the wrong user — both caught here.
    """
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    assert current_user_id() == 1, "precondition: ambient user is 1"

    brief.build(_StubSession("caller"), user_id=2, today=FRIDAY)

    assert rec.calls, "no sources ran"
    observed = {c["user_id"] for c in rec.calls}
    assert observed == {2}, f"workers saw {observed}, expected only user 2"


def test_each_worker_opens_its_own_session(monkeypatch):
    """SQLAlchemy sessions are not thread-safe — no sharing across the pool."""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    caller = _StubSession("caller")
    brief.build(caller, user_id=1, today=FRIDAY)

    session_ids = [c["session_id"] for c in rec.calls]
    assert id(caller) not in session_ids, "a worker reused the caller's session"
    assert len(set(session_ids)) == len(session_ids), "workers shared a session"


# ---------------------------------------------------------------------------
# Cache contract
# ---------------------------------------------------------------------------


def _volatile_keys(prefs: dict) -> set[str]:
    ctx = brief.Ctx(user_id=1, prefs=prefs, since=None, today=FRIDAY)
    return {s.key for s in brief.build_sources(ctx) if s.volatile}


def test_volatile_sources_are_refetched_every_time(monkeypatch):
    """Departure boards and home status must never come from cache."""
    rec = _Recorder()
    resolved = _install_stubs(monkeypatch, rec)
    volatile = _volatile_keys(resolved)
    assert "rail" in volatile and "home" in volatile, "fixture assumption"

    brief.build(_StubSession("c"), user_id=1, today=FRIDAY)
    first = len(rec.calls)
    brief.build(_StubSession("c"), user_id=1, today=FRIDAY)
    second_pass = rec.calls[first:]

    refetched = {c["capability"] for c in second_pass}
    assert refetched, "second call fetched nothing at all — cache too greedy"
    # Everything refetched on the second pass must be volatile; the cacheable
    # ones should have been served warm.
    assert len(second_pass) == len(volatile), (
        f"expected only the {len(volatile)} volatile sources to refetch, "
        f"got {len(second_pass)}"
    )


def test_cacheable_sources_are_served_warm_on_the_second_call(monkeypatch):
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    first = brief.build(_StubSession("c"), user_id=1, today=FRIDAY)
    second = brief.build(_StubSession("c"), user_id=1, today=FRIDAY)

    assert first["_meta"]["sources_from_cache"] == []
    assert "weather_current" in second["_meta"]["sources_from_cache"]
    assert "rail" not in second["_meta"]["sources_from_cache"]


def test_health_sources_are_never_cached(monkeypatch):
    """Health is push-fed by the phone on its own schedule, so a cached
    snapshot may predate the night's data and serve a plausible zero
    (0h sleep / no sessions) as fresh. Regression for 2026-08-11, when
    three consecutive daily notes escalated a fictional 'Watch not
    recording' alarm off exactly this."""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    brief.build(_StubSession("c"), user_id=1, today=FRIDAY)
    second = brief.build(_StubSession("c"), user_id=1, today=FRIDAY)

    health_keys = {"health_summary", "health_sleep", "health_trends", "health_workouts"}
    assert health_keys.isdisjoint(second["_meta"]["sources_from_cache"])
    assert health_keys <= set(second["_meta"]["sources_fetched"])


def test_refresh_bypasses_the_cache(monkeypatch):
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    brief.build(_StubSession("c"), user_id=1, today=FRIDAY)
    before = len(rec.calls)
    result = brief.build(_StubSession("c"), user_id=1, today=FRIDAY, refresh=True)

    assert result["_meta"]["sources_from_cache"] == []
    assert len(rec.calls) - before > len(_volatile_keys(
        {k: v.default for k, v in prefs_service.PREFERENCES.items()}
    ))


def test_cache_is_per_user(monkeypatch):
    """User 1 warming the cache must not serve user 2 anything."""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    brief.build(_StubSession("c"), user_id=1, today=FRIDAY)
    result = brief.build(_StubSession("c"), user_id=2, today=FRIDAY)

    assert result["_meta"]["sources_from_cache"] == [], (
        "user 2 read cache entries warmed by user 1"
    )


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


def test_one_failing_source_does_not_take_down_the_brief(monkeypatch):
    """A dead Gmail token must not cost you the weather."""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec, failing_methods={"unread"})

    result = brief.build(_StubSession("c"), user_id=1, today=FRIDAY)

    assert "error" in result["mail_unread"]
    assert result["weather_current"] == {"ok": "current"}


def test_failed_sources_are_not_cached(monkeypatch):
    """A transient failure must be retried, not cached as an error."""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec, failing_methods={"unread"})

    brief.build(_StubSession("c"), user_id=1, today=FRIDAY)
    second = brief.build(_StubSession("c"), user_id=1, today=FRIDAY)

    assert "mail_unread" not in second["_meta"]["sources_from_cache"]
    assert "mail_unread" in second["_meta"]["sources_fetched"]


def test_non_json_source_output_is_passed_through_as_a_message(monkeypatch):
    """An unconfigured integration returns prose naming the fix, not JSON."""
    assert brief._decode("no station configured") == {
        "message": "no station configured"
    }
    assert brief._decode('{"a": 1}') == {"a": 1}


# ---------------------------------------------------------------------------
# Selection: sections, gating, weekends
# ---------------------------------------------------------------------------


def test_sections_filter_limits_what_is_fetched(monkeypatch):
    """`/refresh transport` should cost two sources, not twenty."""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    result = brief.build(
        _StubSession("c"), user_id=1, today=FRIDAY, sections=["transport"]
    )

    fetched = set(result["_meta"]["sources_fetched"])
    assert "rail" in fetched
    assert "health_summary" not in fetched
    assert "mail_recent" not in fetched
    # Alerts have no section and must survive any filter — the caller needs
    # to know the data is degraded before trusting a partial refresh.
    assert "alerts" in fetched


def test_sources_are_skipped_when_the_user_has_no_data(monkeypatch):
    """Auto-omission: no scrobbles, no Listening lines. No config needed."""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec, has_data=False)

    result = brief.build(_StubSession("c"), user_id=1, today=FRIDAY)

    skipped = set(result["_meta"]["sources_skipped_no_data"])
    assert {"lastfm_recent", "coffee_current", "health_summary"} <= skipped
    # Ungated sources still run.
    assert "weather_current" in result["_meta"]["sources_fetched"]


def test_transport_is_absent_at_the_weekend(monkeypatch):
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    saturday = brief.build(_StubSession("c"), user_id=1, today=date(2026, 8, 8))

    assert "rail" not in saturday
    assert saturday["_meta"]["is_weekend"] is True


def test_appliance_sources_only_exist_once_configured(monkeypatch):
    """No neutral default entity id exists, so the lines stay off by default."""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)
    default = brief.build(_StubSession("c"), user_id=1, today=FRIDAY)
    assert not [k for k in default if k.startswith("appliance:")]

    brief.clear_cache()
    _install_stubs(
        monkeypatch, rec,
        prefs={"house.appliance_entities": ["sensor.washer", "sensor.dryer"]},
    )
    configured = brief.build(_StubSession("c"), user_id=1, today=FRIDAY)
    assert "appliance:sensor.washer" in configured
    assert "appliance:sensor.dryer" in configured


def test_preferences_shape_the_arguments(monkeypatch):
    rec = _Recorder()
    _install_stubs(
        monkeypatch, rec,
        prefs={"comms.mail_limit": 7, "rail.direction": "Southbound"},
    )

    brief.build(_StubSession("c"), user_id=1, today=FRIDAY)

    # Keyed by (capability, method): three capabilities expose `recent`
    # (mail, whatsapp, music), so keying on the method alone silently
    # compares whichever ran last.
    by_call = {(c["capability"], c["method"]): c["arguments"] for c in rec.calls}
    assert by_call[("mail.query", "recent")]["limit"] == 7
    assert by_call[("rail.query", "departures")]["direction"] == "Southbound"
    # The Last.fm limit is independent of the mail limit.
    assert by_call[("music.query", "recent")]["limit"] == 15


def test_empty_rail_direction_is_omitted_not_sent_blank(monkeypatch):
    rec = _Recorder()
    _install_stubs(monkeypatch, rec, prefs={"rail.direction": ""})

    brief.build(_StubSession("c"), user_id=1, today=FRIDAY)

    departures = next(c for c in rec.calls if c["method"] == "departures")
    assert "direction" not in departures["arguments"]


# ---------------------------------------------------------------------------
# Read-only contract
# ---------------------------------------------------------------------------


def test_the_brief_composes_no_writing_source():
    """`snag_capture` writes; it must stay out of a readOnlyHint tool.

    Guards the honesty of the annotation rather than any behaviour — a future
    edit that folds a capture/ingest/send capability in here would make
    `readOnlyHint: True` a lie to every MCP client.
    """
    prefs = {k: v.default for k, v in prefs_service.PREFERENCES.items()}
    prefs["house.appliance_entities"] = ["sensor.x"]
    ctx = brief.Ctx(user_id=1, prefs=prefs, since=None, today=FRIDAY)

    forbidden = {"capture", "ingest", "send", "add", "create", "complete", "push"}
    for source in brief.build_sources(ctx):
        assert source.method not in forbidden, f"{source.key} writes"
        assert not any(
            source.capability.endswith(f".{w}") for w in forbidden
        ), f"{source.key} resolves a writing capability"


def test_prewarm_skips_volatile_sources(monkeypatch):
    """Warming a departure board caches something unservable."""
    rec = _Recorder()
    resolved = _install_stubs(monkeypatch, rec)

    with use_user(1):
        brief.build(
            _StubSession("c"), user_id=1, today=FRIDAY,
            include_volatile=False, refresh=True,
        )

    fetched_caps = {c["capability"] for c in rec.calls}
    assert "rail.query" not in fetched_caps
    # Health is volatile too (push-fed — see brief.py docstring), so the
    # pre-warm must not snapshot it either.
    assert "health.query" not in fetched_caps
    assert "weather.query" in fetched_caps


def test_default_comms_limits_are_the_trimmed_ones(monkeypatch):
    """2026-09-02: at 50/50 the live brief was 130k characters, more than a
    model client keeps in context. The registry default is the one that
    matters (`preferences.PREFERENCES`); the inline fallback in brief.py
    must agree with it, or a user with no stored preference gets a different
    brief from the one the registry describes."""
    from app.services import preferences

    rec = _Recorder()
    _install_stubs(monkeypatch, rec, prefs={})
    brief.build(_StubSession("c"), user_id=1, today=FRIDAY)
    by_call = {(c["capability"], c["method"]): c["arguments"] for c in rec.calls}
    assert by_call[("mail.query", "recent")]["limit"] == 25 == preferences.PREFERENCES["comms.mail_limit"].default
    assert by_call[("whatsapp.query", "recent")]["limit"] == 30 == preferences.PREFERENCES["comms.whatsapp_limit"].default
