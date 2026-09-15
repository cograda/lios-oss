"""AiUsage model round-trip, and the ai_ledger service actually landing rows.

db tier — needs the real ai_usage table from the migration.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.db


def test_ai_usage_round_trip(db_session):
    from app.models.ai_usage import AiUsage

    db_session.add(AiUsage(
        role=None,
        provider="anthropic",
        model="claude-sonnet-5",
        kind="chat",
        caller="test:round_trip",
        units_in=100,
        units_out=50,
        reasoning_units=0,
        cost_usd=0.00105,
        input_rate=3.00,
        output_rate=15.00,
        ok=True,
    ))
    db_session.commit()

    row = db_session.query(AiUsage).filter(AiUsage.caller == "test:round_trip").one()
    assert row.provider == "anthropic"
    assert row.model == "claude-sonnet-5"
    assert row.kind == "chat"
    assert row.units_in == 100
    assert row.units_out == 50
    assert row.cost_usd == pytest.approx(0.00105)
    assert row.input_rate == 3.00
    assert row.output_rate == 15.00
    assert row.ok is True
    assert row.error is None
    assert row.role is None
    assert row.ts is not None


def test_null_cost_is_distinct_from_zero_cost(db_session):
    """The whole design turns on this: NULL means unknown, 0.0 means free."""
    from app.models.ai_usage import AiUsage

    db_session.add(AiUsage(
        provider="local", model="fastembed-bge-small", kind="embedding",
        caller="test:local_zero", cost_usd=0.0,
    ))
    db_session.add(AiUsage(
        provider="nabu-casa", model="cloud-tts", kind="tts",
        caller="test:subscription_unknown", cost_usd=None,
    ))
    db_session.commit()

    local_row = db_session.query(AiUsage).filter(AiUsage.caller == "test:local_zero").one()
    sub_row = db_session.query(AiUsage).filter(
        AiUsage.caller == "test:subscription_unknown"
    ).one()

    assert local_row.cost_usd == 0.0
    assert local_row.cost_usd is not None
    assert sub_row.cost_usd is None


def test_indexes_exist(db_session):
    """ix_ai_usage_ts and ix_ai_usage_caller_ts must exist, per the migration."""
    from sqlalchemy import inspect

    inspector = inspect(db_session.bind)
    index_names = {ix["name"] for ix in inspector.get_indexes("ai_usage")}
    assert "ix_ai_usage_ts" in index_names
    assert "ix_ai_usage_caller_ts" in index_names


def test_ai_ledger_record_lands_a_row(db_session, monkeypatch):
    """The service's buffered write path actually reaches the table.

    `db_session` depends on the `real_db` fixture, which already points
    `app.db.get_db()` at this test's Postgres — the ledger's `_write()`
    calls that same `get_db()`, so no extra wiring is needed here.
    """
    from app.models.ai_usage import AiUsage
    from app.services import ai_ledger

    ai_ledger._reset_for_tests()
    try:
        # Land the row via this test's own flush_sync(), not a race against
        # the background thread — see the equivalent note in test_algo_llm.py.
        monkeypatch.setattr(ai_ledger._LEDGER, "_ensure_thread", lambda: None)
        ai_ledger.record(
            provider="anthropic", model="claude-haiku-4-5-20251001", kind="chat",
            caller="test:service_write", units_in=10, units_out=5, cost_usd=0.001,
        )
        ai_ledger.flush_sync()

        row = db_session.query(AiUsage).filter(
            AiUsage.caller == "test:service_write"
        ).one_or_none()
        assert row is not None
        assert row.provider == "anthropic"
        assert row.units_in == 10
    finally:
        ai_ledger._reset_for_tests()


def test_system_ai_usage_tool_answers_what_a_call_cost(db_session):
    """Added 2026-09-02, the day the question 'what did that transcription
    cost?' could only be answered with psql. Totals by role/model, plus the
    recent rows in full, and NULL cost surfaced as unpriced — never as zero."""
    import json
    from app.integrations.system.tools import handle_ai_usage
    from app.models.ai_usage import AiUsage

    db_session.add_all([
        AiUsage(role="stt.memo", provider="google", model="gemini-3.7-flash", kind="stt",
                caller="integration:transcription", units_in=83217, units_out=14147,
                reasoning_units=17847, latency_ms=91103, cost_usd=0.18239, input_rate=0.75, output_rate=3.75),
        AiUsage(role="stt.memo", provider="google", model="gemini-3.7-flash", kind="stt",
                caller="integration:transcription", units_in=7625, units_out=849, cost_usd=0.0097),
        AiUsage(role="embed.corpus", provider="google", model="gemini-embedding-2", kind="embedding",
                caller="integration:embedding", units_in=5000, cost_usd=None),
        AiUsage(role="vision.inbox", provider="google", model="gemini-3.5-flash-lite", kind="vision",
                caller="integration:vision", units_in=900, units_out=80, cost_usd=0.001, ok=False, error="400"),
    ])
    db_session.commit()

    out = json.loads(handle_ai_usage(db_session, {"days": 7, "limit": 5}))
    assert out["total"]["calls"] == 4
    assert out["total"]["cost_usd"] == pytest.approx(0.1931, abs=1e-4)
    assert out["total"]["unpriced_calls"] == 1
    by_role = {r["key"]: r for r in out["by_role"]}
    assert by_role["stt.memo"]["calls"] == 2
    assert by_role["stt.memo"]["cost_usd"] == pytest.approx(0.1921, abs=1e-4)
    assert by_role["embed.corpus"]["cost_usd"] is None and by_role["embed.corpus"]["unpriced_calls"] == 1
    assert by_role["vision.inbox"]["failed_calls"] == 1
    # Most expensive first.
    assert out["by_role"][0]["key"] == "stt.memo"
    # The recent rows carry the whole story of one call.
    top = next(r for r in out["recent"] if r["tokens"]["in"] == 83217)
    assert top["tokens"] == {"in": 83217, "out": 14147, "reasoning": 17847}
    assert top["cost_usd"] == pytest.approx(0.1824, abs=1e-4) and top["rates_per_1m"] == {"in": 0.75, "out": 3.75}

    only_stt = json.loads(handle_ai_usage(db_session, {"role": "stt.memo"}))
    assert only_stt["total"]["calls"] == 2 and only_stt["filters"] == {"role": "stt.memo"}
