"""app.algo.llm.AlgoLLM — the per-run cost ledger, now also writing ai_usage.

The AlgoRun aggregation (self.ledger, tested indirectly by test_algo_harness.py
via the harness end to end) is untouched by this chunk. What's new: every
`ask()` call also lands one row on the cross-cutting `ai_usage` table via
`app.services.ai_ledger`, caller="algo:<name>" — see app/algo/llm.py.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.db


def _fake_response(model="claude-haiku-4-5-20251001", provider="anthropic"):
    from coglib.llm import Response

    return Response(
        text="42",
        model=model,
        provider=provider,
        prompt_tokens=120,
        reasoning_tokens=0,
        output_tokens=8,
        secs=0.75,
        raw={},
    )


def test_ask_writes_an_ai_usage_row(db_session, monkeypatch):
    from app.algo.llm import AlgoLLM
    from app.models.ai_usage import AiUsage
    from app.services import ai_ledger

    ai_ledger._reset_for_tests()
    try:
        # Force the write through this test's own flush_sync() call below,
        # not the background thread — the thread is real in production, but
        # racing it against a same-test assertion is exactly the kind of
        # flake a fire-and-forget design invites if tests don't pin it down.
        monkeypatch.setattr(ai_ledger._LEDGER, "_ensure_thread", lambda: None)
        monkeypatch.setattr("app.algo.llm._resolve_key", lambda provider: "fake-key")

        import coglib.llm as coglib_llm

        response = _fake_response()
        monkeypatch.setattr(coglib_llm, "call", lambda *a, **kw: response)

        algo_llm = AlgoLLM(algo="solar_forecast", model="claude-haiku-4-5-20251001")
        text = algo_llm.ask("what is the forecast?")
        assert text == "42"

        # The per-run aggregation still works exactly as before this chunk.
        assert algo_llm.ledger.calls == 1
        assert algo_llm.ledger.tokens == 128

        ai_ledger.flush_sync()

        row = db_session.query(AiUsage).filter(
            AiUsage.caller == "algo:solar_forecast"
        ).one_or_none()
        assert row is not None
        assert row.provider == "anthropic"
        assert row.model == "claude-haiku-4-5-20251001"
        assert row.kind == "chat"
        assert row.units_in == 120
        assert row.units_out == 8
        assert row.cost_usd is not None
        assert row.cost_usd > 0
        assert row.role is None  # reserved for the registry chunk
    finally:
        ai_ledger._reset_for_tests()


def test_ask_never_raises_into_the_algo_when_ledger_db_is_broken(db_session, monkeypatch):
    """`record_llm_response()` is called inline in `ask()`, right after a
    successful (billable) LLM call — it only enqueues (see
    app.services.ai_ledger's module docstring), so a broken database behind
    it must not turn that already-succeeded call into a failed algo run."""
    from app.algo.llm import AlgoLLM
    from app.services import ai_ledger

    ai_ledger._reset_for_tests()
    try:
        monkeypatch.setattr("app.algo.llm._resolve_key", lambda provider: "fake-key")
        monkeypatch.setattr(ai_ledger._LEDGER, "_ensure_thread", lambda: None)

        import coglib.llm as coglib_llm

        monkeypatch.setattr(coglib_llm, "call", lambda *a, **kw: _fake_response())

        def _boom():
            raise RuntimeError("database is on fire")

        monkeypatch.setattr("app.db.get_db", _boom)

        algo_llm = AlgoLLM(algo="test_algo", model="claude-haiku-4-5-20251001")
        # Must not raise, even though the ledger's eventual DB write would.
        text = algo_llm.ask("prompt")
        assert text == "42"
        assert algo_llm.ledger.calls == 1  # the pre-existing per-run ledger is untouched

        ai_ledger.flush_sync()  # exercises the broken get_db() — must not raise
        assert ai_ledger.dropped_count() >= 1
    finally:
        ai_ledger._reset_for_tests()
