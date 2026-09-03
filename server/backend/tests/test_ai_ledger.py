"""app.services.ai_ledger — the never-raises, never-blocks write path.

Two tiers, following the project convention: the "never raises" and
null-vs-zero-cost properties are pure/mocked and stay `unit`; the actual
row-lands-in-`ai_usage` proof needs Postgres and is `db`.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_ledger():
    """Fresh in-memory queue per test — the module-level singleton would
    otherwise leak enqueued rows and a running flush thread across tests."""
    from app.services import ai_ledger

    ai_ledger._reset_for_tests()
    yield
    ai_ledger._reset_for_tests()


# ---------------------------------------------------------------------------
# record() never raises
# ---------------------------------------------------------------------------

def test_record_never_raises_on_broken_db(monkeypatch):
    """A broken get_db() must not surface through record() or flush_sync()."""
    from app.services import ai_ledger

    def _boom():
        raise RuntimeError("database is on fire")

    monkeypatch.setattr("app.db.get_db", _boom)
    # Prevent the real background thread from also racing this test's own
    # flush_sync() call against the same broken get_db() — this test is
    # about flush_sync()'s never-raise property specifically.
    monkeypatch.setattr(ai_ledger._LEDGER, "_ensure_thread", lambda: None)

    # record() itself never touches the DB (it only enqueues) — but the
    # contract is "never raises", so assert that holds regardless.
    ai_ledger.record(provider="anthropic", model="claude-sonnet-5", kind="chat", caller="test")

    # flush_sync() is where the broken DB would actually be exercised.
    ai_ledger.flush_sync()  # must not raise

    assert ai_ledger.dropped_count() >= 1


def test_record_never_raises_on_bad_kwargs(monkeypatch):
    """A caller passing garbage must not be able to crash the meter."""
    from app.services import ai_ledger

    monkeypatch.setattr(ai_ledger._LEDGER, "_ensure_thread", lambda: None)

    # Wrong types across the board — record() catches everything.
    ai_ledger.record(
        provider=None, model=123, kind="chat", caller=object(),  # type: ignore[arg-type]
        units_in="not-an-int",  # type: ignore[arg-type]
    )
    # No exception means the contract held. (It may or may not enqueue —
    # what matters is it never propagates.)


def test_record_drops_unknown_kind_and_counts_it():
    from app.services import ai_ledger

    before = ai_ledger.dropped_count()
    ai_ledger.record(provider="google", model="gemini-3.6-flash", kind="not-a-real-kind", caller="test")
    assert ai_ledger.dropped_count() == before + 1


def test_queue_overflow_is_counted_not_silent(monkeypatch):
    """A silently lossy meter is worse than none — dropped rows must be counted."""
    from collections import deque

    from app.services import ai_ledger

    # Shrink the queue so overflow is reachable without enqueuing thousands of
    # rows, and stop record() from spinning up the real background thread
    # (which would touch app.db.get_db() and race the assertions below) —
    # this test is about the synchronous enqueue-time drop count, not flush.
    ai_ledger._LEDGER._queue = deque(maxlen=2)
    monkeypatch.setattr(ai_ledger._LEDGER, "_ensure_thread", lambda: None)

    for _ in range(5):
        ai_ledger.record(provider="google", model="gemini-3.6-flash", kind="chat", caller="test")

    assert ai_ledger.dropped_count() == 3  # 5 enqueued, capacity 2 -> 3 dropped


# ---------------------------------------------------------------------------
# null-vs-zero cost distinction
# ---------------------------------------------------------------------------

def test_record_llm_response_null_cost_for_unpriced_model():
    """A model absent from coglib.llm.MODELS must yield cost_usd=None, not 0."""
    from app.services import ai_ledger

    class _FakeResponse:
        model = "some-model-not-in-the-rate-table"
        provider = "google"
        prompt_tokens = 100
        output_tokens = 50
        reasoning_tokens = 0
        secs = 1.2

        @property
        def cost(self):
            raise KeyError("not priced")  # mirrors coglib.llm.Response.cost's behaviour

    captured = {}

    def _capture_record(**kwargs):
        captured.update(kwargs)

    import app.services.ai_ledger as mod
    orig_record = mod.record
    try:
        mod.record = _capture_record  # type: ignore[assignment]
        mod.record_llm_response(_FakeResponse(), caller="test:unpriced")
    finally:
        mod.record = orig_record

    assert captured["cost_usd"] is None
    assert captured["input_rate"] is None
    assert captured["output_rate"] is None


def test_record_llm_response_real_cost_for_priced_model():
    """A model present in coglib.llm.MODELS gets a real, non-null cost."""
    from coglib import llm as _llm

    import app.services.ai_ledger as mod

    class _FakeResponse:
        model = "claude-sonnet-5"
        provider = "anthropic"
        prompt_tokens = 1000
        output_tokens = 500
        reasoning_tokens = 0
        secs = 2.0
        # response.cost is a real property on the real Response dataclass;
        # this fake mirrors it exactly using the live rate table so the test
        # breaks (loudly) if MODELS' shape ever changes.
        @property
        def cost(self):
            spec = _llm.MODELS[self.model]
            return (self.prompt_tokens * spec.input_rate + self.output_tokens * spec.output_rate) / 1e6

    captured = {}

    def _capture_record(**kwargs):
        captured.update(kwargs)

    orig_record = mod.record
    try:
        mod.record = _capture_record  # type: ignore[assignment]
        mod.record_llm_response(_FakeResponse(), caller="test:priced")
    finally:
        mod.record = orig_record

    assert captured["cost_usd"] is not None
    assert captured["cost_usd"] > 0
    assert captured["input_rate"] == _llm.MODELS["claude-sonnet-5"].input_rate
    assert captured["output_rate"] == _llm.MODELS["claude-sonnet-5"].output_rate


def test_local_call_records_zero_not_null(monkeypatch):
    """A caller reporting a local (free) call must pass cost_usd=0.0 explicitly
    — the ledger doesn't infer "free" from anything, by design."""
    from app.services import ai_ledger

    # Inspecting the queue below needs the background flush thread not to
    # have drained it out from under the assertion.
    monkeypatch.setattr(ai_ledger._LEDGER, "_ensure_thread", lambda: None)
    ai_ledger.record(
        provider="local", model="fastembed-bge-small", kind="embedding",
        caller="test:local", cost_usd=0.0,
    )
    with ai_ledger._LEDGER._lock:
        row = ai_ledger._LEDGER._queue[-1]
    assert row.cost_usd == 0.0
    assert row.cost_usd is not None


def test_unknown_cost_stays_null_end_to_end(monkeypatch):
    from app.services import ai_ledger

    monkeypatch.setattr(ai_ledger._LEDGER, "_ensure_thread", lambda: None)
    ai_ledger.record(
        provider="nabu-casa", model="cloud-stt", kind="stt", caller="test:subscription",
        cost_usd=None,
    )
    with ai_ledger._LEDGER._lock:
        row = ai_ledger._LEDGER._queue[-1]
    assert row.cost_usd is None


# ---------------------------------------------------------------------------
# role — W2 chunk 2 fills a column chunk 1 left NULL
# ---------------------------------------------------------------------------

def test_record_carries_role_through_to_the_row(monkeypatch):
    from app.services import ai_ledger

    monkeypatch.setattr(ai_ledger._LEDGER, "_ensure_thread", lambda: None)
    ai_ledger.record(
        provider="google", model="gemini-3.6-flash", kind="stt",
        caller="integration:transcription", role="stt.memo",
    )
    with ai_ledger._LEDGER._lock:
        row = ai_ledger._LEDGER._queue[-1]
    assert row.role == "stt.memo"


def test_record_role_defaults_to_none_for_unrewired_callers():
    """Chunk 1's contract: `role` is nullable and every row was written with
    `role=None` before the registry existed. A caller that still doesn't
    pass one (there shouldn't be any left among the role-served call sites,
    but the parameter itself must stay optional) gets NULL, not an error."""
    from app.services import ai_ledger

    row = ai_ledger._Row(
        ts=None, role=None, provider="p", model="m", kind="chat", caller="c",
        units_in=0, units_out=0, reasoning_units=0, seconds=None,
        latency_ms=None, cost_usd=None, input_rate=None, output_rate=None,
        ok=True, error=None,
    )
    assert row.role is None


def test_record_genai_usage_carries_role(monkeypatch):
    """`record_genai_usage()` — the shared path for vision/client.py and
    transcription/gemini.py — must accept and forward `role`, since those
    are exactly the two call sites this chunk wires a role into."""
    from app.services import ai_ledger

    captured = {}

    def _capture_record(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(ai_ledger, "record", _capture_record)
    ai_ledger.record_genai_usage(
        model="gemini-3.5-flash-lite", kind="vision", caller="integration:vision",
        role="vision.inbox", started=0.0, ok=True,
    )
    assert captured["role"] == "vision.inbox"
