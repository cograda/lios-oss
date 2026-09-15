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


_COMMS_CAPABILITIES = {"mail.query", "whatsapp.query"}


def _messages_across_days(anchor: date, *, n_days: int, per_day: int) -> list[dict]:
    """A fake comms pool spanning `n_days` ending on `anchor`, newest-first —
    matching the real `whatsapp`/`mail_recent` `to_dict` shape (a `date`
    field) and `ListTool`'s default ordering."""
    from datetime import datetime, timedelta, timezone

    messages = []
    for day_offset in range(n_days):
        day = anchor - timedelta(days=day_offset)
        for i in range(per_day):
            ts = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(
                hours=8 + (i % 12), minutes=i
            )
            messages.append({"date": ts.isoformat(), "body": f"msg {day.isoformat()}-{i}"})
    messages.sort(key=lambda m: m["date"], reverse=True)
    return messages


def _install_stubs(
    monkeypatch,
    recorder: _Recorder,
    *,
    has_data: bool = True,
    failing_methods: set[str] | None = None,
    prefs: dict | None = None,
    comms_messages=None,
):
    """Point the brief at fake capabilities, sessions and preferences.

    `comms_messages`, if given, is a zero-arg callable returning a list of
    fake message dicts — used to exercise the window-slicing behaviour on
    `mail.query`/`whatsapp.query` `recent` calls. Every other call keeps the
    plain `{"ok": method}` stub.
    """
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
                if (
                    comms_messages is not None
                    and method == "recent"
                    and self._capability in _COMMS_CAPABILITIES
                ):
                    return json.dumps(comms_messages())
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
    """`/checkin transport` should cost two sources, not twenty."""
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


def test_calendar_alias_selects_the_calendar_source(monkeypatch):
    """Callers pass the *rendered* vocabulary (`brief_render.SECTIONS`, and
    both prompt templates), not the *preference* vocabulary
    (`daily_note.sections`) - `sections=["calendar"]` (as `/kickoff`'s
    template sends) must select the `calendar` source, not match nothing."""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    result = brief.build(
        _StubSession("c"), user_id=1, today=FRIDAY, sections=["calendar"]
    )

    fetched = set(result["_meta"]["sources_fetched"])
    assert "calendar" in fetched


def test_preference_vocabulary_still_works(monkeypatch):
    """The preference name itself (`today`, not the rendered alias
    `calendar`) must keep selecting the same source."""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    result = brief.build(
        _StubSession("c"), user_id=1, today=FRIDAY, sections=["today"]
    )

    fetched = set(result["_meta"]["sources_fetched"])
    assert "calendar" in fetched


def test_unknown_section_name_is_reported_not_silently_dropped(monkeypatch):
    """A typo in `sections=[...]` should be loud in `_meta`, not produce a
    quiet 'not measured' section."""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    result = brief.build(
        _StubSession("c"), user_id=1, today=FRIDAY, sections=["kalendar"]
    )

    assert result["_meta"]["sections_unknown"] == ["kalendar"]


def test_sources_are_skipped_when_the_user_has_no_data(monkeypatch):
    """Auto-omission: no scrobbles, no Listening lines. No config needed."""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec, has_data=False)

    result = brief.build(_StubSession("c"), user_id=1, today=FRIDAY)

    skipped = set(result["_meta"]["sources_skipped_no_data"])
    assert {"lastfm_recent", "coffee_current", "health_summary"} <= skipped
    # Ungated sources still run.
    assert "weather_current" in result["_meta"]["sources_fetched"]
    # strava_activities (issue #195b) is deliberately NOT gated_on="strava.query"
    # — has_data()=False here would otherwise mean "connected but no activity
    # ever stored" gets treated the same as "never fetched", which is exactly
    # the silent-omission failure #195 was filed against.
    assert "strava_activities" not in skipped
    assert "strava_activities" in result["_meta"]["sources_fetched"]


def test_strava_activities_source_is_wired_for_the_pulse_merge(monkeypatch):
    """#195b: the brief must actually ask Strava, not just Apple Health, for
    the daily-note Pulse workouts line."""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    brief.build(_StubSession("c"), user_id=1, today=FRIDAY)

    calls = {c["method"]: c for c in rec.calls if c["capability"] == "strava.query"}
    assert "activities" in calls
    assert calls["activities"]["arguments"] == {"days": 7}


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
    # `mail.query.recent`'s `limit` is now a *pool* size — bigger than the
    # display cap so the cap can be applied breadth-first (see
    # `_slice_window`) — and it carries `after` for the lookback filter.
    # `_comms_pool_limit(7)` is the formula under test elsewhere; assert its
    # relationship to the preference rather than duplicating the constant.
    assert by_call[("mail.query", "recent")]["limit"] == brief._comms_pool_limit(7)
    expected_lookback = brief.Ctx(
        user_id=1,
        prefs={k: v.default for k, v in prefs_service.PREFERENCES.items()},
        since=None, today=FRIDAY,
    ).lookback_start
    assert by_call[("mail.query", "recent")]["after"] == expected_lookback.isoformat()
    assert by_call[("rail.query", "departures")]["direction"] == "Southbound"
    # The Last.fm limit is independent of the mail limit, and unwindowed.
    assert by_call[("music.query", "recent")]["limit"] == 15


# ---------------------------------------------------------------------------
# Comms windowing — `comms.whatsapp_limit`/`comms.mail_limit` as a safety cap
# on a time window, not the primary filter (Backlog: "silently loses days")
# ---------------------------------------------------------------------------


def test_slice_window_keeps_everything_when_pool_fits_the_cap():
    pool = _messages_across_days(FRIDAY, n_days=2, per_day=3)  # 6 messages
    result = brief._slice_window(pool, cap=10, window_requested="2026-08-06")

    assert result["messages"] == pool
    assert result["truncated"] is False
    assert result["dropped_count"] == 0
    assert result["window_requested"] == "2026-08-06"
    assert result["window_covered"]["newest"] == pool[0]["date"]
    assert result["window_covered"]["oldest"] == pool[-1]["date"]


def test_slice_window_breadth_preserves_every_day_when_the_cap_bites():
    """The regression case: one chatty day must not crowd the others out of
    the result the way a plain 'newest N' cap did (Backlog item — a Monday's
    47-minute WhatsApp window from one long conversation eating the budget)."""
    # A busy "Monday" (80 messages) plus three quiet days (2 each) — a plain
    # newest-N cap of 20 would return only Monday's messages.
    monday = FRIDAY  # any anchor; the day boundary logic is what's tested
    from datetime import timedelta

    pool = []
    pool += _messages_across_days(monday, n_days=1, per_day=80)  # "Monday"
    for offset in (1, 2, 3):
        day = monday - timedelta(days=offset)
        pool += _messages_across_days(day, n_days=1, per_day=2)

    result = brief._slice_window(pool, cap=20, window_requested="irrelevant")

    assert result["truncated"] is True
    assert result["dropped_count"] == len(pool) - 20
    assert len(result["messages"]) == 20

    days_present = {m["date"][:10] for m in result["messages"]}
    expected_days = {(monday - timedelta(days=d)).isoformat() for d in range(4)}
    assert days_present == expected_days, (
        "every day in the pool must survive the cap — a chatty day must not "
        "crowd the quiet ones out entirely"
    )


def test_breadth_select_is_a_lower_bound_not_evenly_split():
    """A day with fewer messages than its round-robin share contributes all
    of them and no more; it never pads with duplicates to reach parity."""
    pool = (
        _messages_across_days(FRIDAY, n_days=1, per_day=1)
        + _messages_across_days(FRIDAY, n_days=1, per_day=1)  # same day, dup date bucket
    )
    selected = brief._breadth_select(pool, cap=5)
    assert len(selected) == len(pool)  # cap exceeds pool size — nothing invented


def test_monday_lookback_covers_friday_to_sunday(monkeypatch):
    """End-to-end: a Monday brief must still surface Fri/Sat/Sun messages
    even when Monday itself is far chattier — the exact scenario the backlog
    item measured (47 minutes of coverage from one long conversation)."""
    from datetime import timedelta

    monday = date(2026, 8, 10)
    assert monday.weekday() == 0, "fixture assumption: this date is a Monday"

    wa_default = prefs_service.PREFERENCES["comms.whatsapp_limit"].default

    def _chatty_monday_pool():
        # Monday alone must exceed the display cap on its own (2026-09-07:
        # raised to 120), or nothing is truncated and the test's premise —
        # that breadth-first selection is what saves the weekend — never
        # fires at all.
        pool = _messages_across_days(monday, n_days=1, per_day=wa_default + 40)  # Monday
        for offset in (1, 2, 3):  # Sun, Sat, Fri
            pool += _messages_across_days(monday - timedelta(days=offset), n_days=1, per_day=2)
        return pool

    rec = _Recorder()
    _install_stubs(monkeypatch, rec, comms_messages=_chatty_monday_pool)

    result = brief.build(_StubSession("c"), user_id=1, today=monday)

    wa = result["whatsapp"]
    assert wa["truncated"] is True
    days_present = {m["date"][:10] for m in wa["messages"]}
    friday = (monday - timedelta(days=3)).isoformat()
    saturday = (monday - timedelta(days=2)).isoformat()
    sunday = (monday - timedelta(days=1)).isoformat()
    assert {friday, saturday, sunday} <= days_present, (
        "weekend messages must survive even when Monday is far chattier"
    )


def test_truncation_is_reported_honestly_in_the_payload(monkeypatch):
    mail_default = prefs_service.PREFERENCES["comms.mail_limit"].default
    rec = _Recorder()
    _install_stubs(
        monkeypatch, rec,
        # Per-day volume big enough to exceed the registry default cap
        # (2026-09-07: raised to 40) across the 4-day pool.
        comms_messages=lambda: _messages_across_days(FRIDAY, n_days=4, per_day=mail_default),
    )

    result = brief.build(_StubSession("c"), user_id=1, today=FRIDAY)

    mail = result["mail_recent"]
    assert mail["truncated"] is True
    assert mail["dropped_count"] > 0
    assert mail["window_requested"] is not None
    assert mail["window_covered"]["oldest"] is not None
    assert mail["window_covered"]["newest"] is not None
    assert len(mail["messages"]) == mail_default  # the registry default cap


def test_untruncated_window_reports_truncated_false(monkeypatch):
    rec = _Recorder()
    _install_stubs(
        monkeypatch, rec,
        comms_messages=lambda: _messages_across_days(FRIDAY, n_days=1, per_day=2),
    )

    result = brief.build(_StubSession("c"), user_id=1, today=FRIDAY)

    assert result["whatsapp"]["truncated"] is False
    assert result["whatsapp"]["dropped_count"] == 0


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
    brief from the one the registry describes.

    The cap now applies to the *output* size (see `_slice_window`), not the
    raw query — a bigger pool is fetched internally for breadth, then
    trimmed back down — so this asserts the final message count stays at
    the registry default rather than the pool-fetch argument."""
    from app.services import preferences

    rec = _Recorder()
    _install_stubs(
        monkeypatch, rec, prefs={},
        comms_messages=lambda: _messages_across_days(FRIDAY, n_days=6, per_day=20),
    )
    result = brief.build(_StubSession("c"), user_id=1, today=FRIDAY)

    mail_default = preferences.PREFERENCES["comms.mail_limit"].default
    wa_default = preferences.PREFERENCES["comms.whatsapp_limit"].default
    assert mail_default == 40 and wa_default == 120

    assert len(result["mail_recent"]["messages"]) == mail_default
    assert len(result["whatsapp"]["messages"]) == wa_default


# ---------------------------------------------------------------------------
# `since` narrows the comms window (2026-09-07)
# ---------------------------------------------------------------------------


def test_since_later_than_lookback_narrows_comms_window(monkeypatch):
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    since = "2026-08-07T09:00:00+00:00"  # same day as FRIDAY, well after midnight
    result = brief.build(_StubSession("c"), user_id=1, today=FRIDAY, since=since)

    assert result["_meta"]["window_source"] == "since"
    assert result["_meta"]["comms_window_start"] == since

    by_call = {(c["capability"], c["method"]): c["arguments"] for c in rec.calls}
    assert by_call[("mail.query", "recent")]["after"] == since
    assert by_call[("whatsapp.query", "recent")]["after"] == since


def test_since_earlier_than_lookback_does_not_widen_it(monkeypatch):
    """A stale `since` (older than the default lookback) must never widen
    the comms window past what the weekday default already computed."""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    stale_since = "2020-01-01T00:00:00+00:00"
    result = brief.build(_StubSession("c"), user_id=1, today=FRIDAY, since=stale_since)

    expected_lookback = brief.Ctx(
        user_id=1,
        prefs={k: v.default for k, v in prefs_service.PREFERENCES.items()},
        since=None, today=FRIDAY,
    ).lookback_start.isoformat()

    assert result["_meta"]["window_source"] == "lookback"
    assert result["_meta"]["comms_window_start"] == expected_lookback

    by_call = {(c["capability"], c["method"]): c["arguments"] for c in rec.calls}
    assert by_call[("mail.query", "recent")]["after"] == expected_lookback


def test_no_since_uses_lookback(monkeypatch):
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    result = brief.build(_StubSession("c"), user_id=1, today=FRIDAY)
    assert result["_meta"]["window_source"] == "lookback"
    assert result["_meta"]["comms_window_start"] == result["_meta"]["lookback_start"]


# ---------------------------------------------------------------------------
# `render=True` (2026-09-07)
# ---------------------------------------------------------------------------


def test_render_true_adds_rendered_block_without_changing_the_rest(monkeypatch):
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    plain = brief.build(_StubSession("c"), user_id=1, today=FRIDAY)
    rendered_result = brief.build(_StubSession("c"), user_id=1, today=FRIDAY, refresh=True, render=True)

    assert "rendered" not in plain
    assert "rendered" in rendered_result
    from app.integrations.system import brief_render as br
    assert set(rendered_result["rendered"]) == set(br.SECTIONS)
    # render=True must not remove or rename anything from the base payload.
    for key in plain:
        if key == "_meta":
            continue
        assert key in rendered_result


def test_render_false_by_default(monkeypatch):
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)

    result = brief.build(_StubSession("c"), user_id=1, today=FRIDAY)
    assert "rendered" not in result


def test_render_caches_only_the_non_volatile_sections(monkeypatch):
    """Coffee/Snags/Listening have no volatile backing source, so their
    rendered fragment is cacheable; the rest (alerts/transport/pulse/
    consumables/calendar/freshness) must always be recomputed."""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)
    from app.integrations.system import brief_render as br

    render_calls: list[tuple] = []
    real_render_all = br.render_all

    def _spy(payload, sections=br.SECTIONS):
        render_calls.append(tuple(sections))
        return real_render_all(payload, sections)

    monkeypatch.setattr(br, "render_all", _spy)

    brief.build(_StubSession("c"), user_id=1, today=FRIDAY, render=True)
    brief.build(_StubSession("c"), user_id=1, today=FRIDAY, render=True)

    assert set(render_calls[0]) == set(br.SECTIONS)
    second = set(render_calls[1])
    assert br.NEVER_CACHE_RENDERED <= second, "always-fresh sections must still be recomputed"
    assert not (second - br.NEVER_CACHE_RENDERED), (
        f"cacheable sections were recomputed on a warm cache: {second - br.NEVER_CACHE_RENDERED}"
    )


def test_render_refresh_recomputes_every_section(monkeypatch):
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)
    from app.integrations.system import brief_render as br

    render_calls: list[tuple] = []
    real_render_all = br.render_all

    def _spy(payload, sections=br.SECTIONS):
        render_calls.append(tuple(sections))
        return real_render_all(payload, sections)

    monkeypatch.setattr(br, "render_all", _spy)

    brief.build(_StubSession("c"), user_id=1, today=FRIDAY, render=True)
    brief.build(_StubSession("c"), user_id=1, today=FRIDAY, render=True, refresh=True)

    assert set(render_calls[1]) == set(br.SECTIONS)


def test_prewarm_style_build_caches_renderable_sections(monkeypatch):
    """The pre-warm path (`include_volatile=False, refresh=True`) with
    `render=True` — as `prewarm_blocking` now calls `build` — must leave the
    cacheable rendered fragments warm for a later `render=True` call."""
    rec = _Recorder()
    _install_stubs(monkeypatch, rec)
    from app.integrations.system import brief_render as br

    with use_user(1):
        brief.build(
            _StubSession("c"), user_id=1, today=FRIDAY,
            include_volatile=False, refresh=True, render=True,
        )

    expected_keys = [f"rendered:{s}" for s in br.SECTIONS if s not in br.NEVER_CACHE_RENDERED]
    cached = brief._cache_read(1, expected_keys, 999999)
    assert set(cached) == set(expected_keys)
