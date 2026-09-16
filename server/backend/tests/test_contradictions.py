"""Contradiction detector, narrow (Wave 2 R6).

Unit tier: the extractor (claims found, non-claims ignored) and the code-side
measurers (tool/integration/table counts against the live registry — no DB
needed, these all read in-process state or the filesystem).

DB tier: the vault_health path against real `embeddings` rows, seeded with
R4's provenance metadata (`source_date`/`is_history`). Two rows, same wrong
number, one `is_history` and one live:

  - the `is_history` chunk must NOT produce a contradiction finding — it's
    the historical record, not a live claim
  - the live chunk WITH THE SAME WRONG NUMBER must produce one

Both mutation checks from the brief are included directly, not just
described: reinstating the bug (removing the `is_history` skip) makes the
first assertion fail, and widening the tolerance to 100% makes the second
assertion fail — proving each test actually exercises the mechanism it
claims to, not something else that happens to produce the same result.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.integrations.system.contradictions import (
    TOLERANCE,
    Claim,
    _check_vault_health,
    _measure_integrations,
    _measure_tables,
    _measure_tools,
    _verdict,
    extract_numeric_claims,
    find_contradictions,
)


class TestExtractNumericClaims:
    def test_tools_and_integrations_in_one_sentence(self):
        claims = extract_numeric_claims("126 tools across 26 integrations")
        by_noun = {c.noun: c for c in claims}
        assert by_noun["tools"].value == 126.0
        assert by_noun["integrations"].value == 26.0

    def test_integration_packages_phrasing(self):
        claims = extract_numeric_claims("27 integration packages")
        assert len(claims) == 1
        assert claims[0].noun == "integrations"
        assert claims[0].value == 27.0

    def test_comma_thousands(self):
        claims = extract_numeric_claims("2,130 tests")
        assert claims[0].noun == "tests"
        assert claims[0].value == 2130.0

    def test_resting_hr_bare(self):
        claims = extract_numeric_claims("resting HR is ~52")
        assert len(claims) == 1
        assert claims[0].noun == "resting_hr"
        assert claims[0].value == 52.0
        assert claims[0].window_days is None

    def test_sleep_hours_minutes_form(self):
        claims = extract_numeric_claims("7h30 average sleep")
        assert len(claims) == 1
        assert claims[0].noun == "sleep_hours"
        assert claims[0].value == 7.5

    def test_sleep_hours_plain_form(self):
        claims = extract_numeric_claims("7.5 hours of average sleep")
        assert claims[0].noun == "sleep_hours"
        assert claims[0].value == 7.5

    def test_hrv_windowed_avg_not_the_min_max_range(self):
        claims = extract_numeric_claims("HRV | 19–43 ms, 7-day avg ~27 ms")
        assert len(claims) == 1
        assert claims[0].noun == "hrv_ms"
        assert claims[0].value == 27.0  # the averaged figure, not 19 or 43
        assert claims[0].window_days == 7

    def test_non_claims_ignored(self):
        """Ordinary prose with numbers that aren't allowlist nouns produces
        nothing — this is a narrow allowlist detector, not a generic
        number-in-prose scraper."""
        text = (
            "The house has 4 bedrooms and the snag register has 42 open "
            "items logged since 15 August, at a cost of 3,200 euro."
        )
        assert extract_numeric_claims(text) == []

    def test_no_double_count_overlapping_patterns(self):
        """The windowed HRV pattern and a hypothetical looser one must not
        both fire on the same span."""
        claims = extract_numeric_claims("HRV 7-day avg ~27 ms")
        assert len(claims) == 1


class TestVerdict:
    def test_within_tolerance_is_match(self):
        assert _verdict(claimed=100.0, measured=104.0, tolerance=0.05) == "match"

    def test_outside_tolerance_is_contradiction(self):
        assert _verdict(claimed=100.0, measured=119.0, tolerance=0.05) == "contradiction"

    def test_unmeasured_when_no_measurement(self):
        assert _verdict(claimed=100.0, measured=None, tolerance=0.05) == "unmeasured"


class TestCodeMeasurers:
    """Measured against the live registry/schema — the same call the
    CLAUDE.md files being checked already document as the correct way to
    check this ("measure, never quote")."""

    def test_tool_count_matches_live_registry(self):
        import app.integrations as I

        I.register_all()
        expected = sum(len(i.mcp_tools()) for i in I.get_all().values())
        assert _measure_tools() == float(expected)

    def test_integration_count_matches_disk(self):
        from pathlib import Path

        import app.integrations.system.contradictions as c

        root = Path(c.__file__).resolve().parents[1]
        expected = sum(
            1 for p in root.iterdir()
            if p.is_dir() and not p.name.startswith(("_", "."))
            and (p / "manifest.py").is_file()
        )
        assert _measure_integrations() == float(expected)

    def test_table_count_matches_metadata(self):
        import app.models  # noqa: F401
        from coglib import Base

        assert _measure_tables() == float(len(Base.metadata.tables))

    def test_tests_noun_is_unmeasured_not_run(self):
        """`pytest --collect-only` is never invoked by this detector — a test
        count claim comes back `unmeasured`, with a reason, not executed."""
        from app.integrations.system.contradictions import _CODE_MEASURERS

        value, reason = _CODE_MEASURERS["tests"](session=None)
        assert value is None
        assert reason  # a stated reason, not a silent None


class TestClaudeMdIntegration:
    """End-to-end against the REAL repo-tracked CLAUDE.md files, run in a
    dev checkout (they're not shipped in the image — see module docstring).
    Reports what's actually there; this test only asserts the machinery
    runs and produces sane verdicts, not any specific current count (that
    would be exactly the "quote a number" mistake this module exists to
    avoid)."""

    def test_runs_against_real_files_without_error(self, mock_session):
        findings = find_contradictions(mock_session, sources=["claude_md"])
        # At least the tool/integration/table counts should be present and
        # checkable (this repo's CLAUDE.md files do state all three).
        nouns_seen = {f.noun for f in findings}
        assert {"tools", "integrations", "tables"} & nouns_seen
        for f in findings:
            assert f.verdict in ("match", "contradiction", "unmeasured")


# ---------------------------------------------------------------------------
# DB tier: vault_health, is_history vs live, same wrong number
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestVaultHealthHistorySkip:
    @staticmethod
    def _seed_metric(session, user_id, metric_type, value, days_ago=0):
        from datetime import date

        from app.integrations.apple_health.models import HealthDailyMetric

        session.add(HealthDailyMetric(
            user_id=user_id,
            date=date.today() - timedelta(days=days_ago),
            metric_type=metric_type,
            value=value,
            synced_at=datetime.now(timezone.utc),
        ))

    @staticmethod
    def _seed_chunk(session, source_id, chunk_text, *, is_history, user_id=1):
        from app.integrations.embedding.models import Embedding

        now = datetime.now(timezone.utc)
        row = Embedding(
            source="vault",
            source_id=source_id,
            user_id=user_id,
            chunk_text=chunk_text,
            content_hash=f"hash-{source_id}",
            metadata_json=json.dumps({
                "source_date": (now - timedelta(days=40 if is_history else 0)).isoformat(),
                "is_history": is_history,
            }),
            created_at=now,
        )
        session.add(row)
        return row

    def test_is_history_wrong_number_not_a_contradiction_live_one_is(self, db_session, monkeypatch):
        # Real resting HR average: seed 30 days at 52 bpm exactly.
        for n in range(30):
            self._seed_metric(db_session, 1, "resting_hr_bpm", 52.0, days_ago=n)
        db_session.flush()

        # Both chunks claim 80 bpm — roughly 54% off a measured 52, well
        # outside the 15% resting_hr tolerance, and (deliberately) also
        # outside a widened 100% tolerance's boundary would need checking
        # separately below for the second mutation check.
        wrong_claim_text = "Resting HR is ~80 on a bad week."
        self._seed_chunk(
            db_session, "Health/Old Snapshot.md", wrong_claim_text, is_history=True,
        )
        self._seed_chunk(
            db_session, "Health/Health Profile.md", wrong_claim_text, is_history=False,
        )
        db_session.commit()

        findings = _check_vault_health(db_session, path_filter=None)
        by_path = {f.source_path: f for f in findings}

        assert "Health/Old Snapshot.md" not in by_path, (
            "an is_history chunk must never produce a finding at all"
        )
        assert by_path["Health/Health Profile.md"].verdict == "contradiction"
        assert by_path["Health/Health Profile.md"].measured == pytest.approx(52.0)

    def test_mutation_drop_is_history_skip_makes_the_first_assertion_fail(self, db_session, monkeypatch):
        """Reinstate the bug: patch `_chunk_provenance` to always report
        `is_history=False`, so the skip never fires, and confirm the
        history chunk WOULD be (wrongly) reported."""
        for n in range(30):
            self._seed_metric(db_session, 1, "resting_hr_bpm", 52.0, days_ago=n)
        db_session.flush()

        wrong_claim_text = "Resting HR is ~80 on a bad week."
        self._seed_chunk(
            db_session, "Health/Old Snapshot.md", wrong_claim_text, is_history=True,
        )
        db_session.commit()

        from app.services import embedding as emb

        real_chunk_provenance = emb._chunk_provenance

        def _never_history(metadata_json, created_at, staleness_threshold_days):
            prov = real_chunk_provenance(metadata_json, created_at, staleness_threshold_days)
            prov["is_history"] = False
            return prov

        monkeypatch.setattr(emb, "_chunk_provenance", _never_history)

        findings = _check_vault_health(db_session, path_filter=None)
        by_path = {f.source_path: f for f in findings}
        # With the skip removed, the history chunk's wrong number now DOES
        # surface as a contradiction — the opposite of the real test above,
        # proving that test was actually exercising the is_history skip.
        assert by_path["Health/Old Snapshot.md"].verdict == "contradiction"

    def test_mutation_widen_tolerance_makes_contradiction_disappear(self, db_session, monkeypatch):
        """Widen `resting_hr` tolerance to 100% and confirm the live chunk's
        wrong number NO LONGER reads as a contradiction — proving the
        original assertion was actually exercising the tolerance check."""
        import app.integrations.system.contradictions as c

        for n in range(30):
            self._seed_metric(db_session, 1, "resting_hr_bpm", 52.0, days_ago=n)
        db_session.flush()

        wrong_claim_text = "Resting HR is ~80 on a bad week."
        self._seed_chunk(
            db_session, "Health/Health Profile.md", wrong_claim_text, is_history=False,
        )
        db_session.commit()

        monkeypatch.setitem(c.TOLERANCE, "resting_hr", 1.0)  # 100%

        findings = _check_vault_health(db_session, path_filter=None)
        by_path = {f.source_path: f for f in findings}
        assert by_path["Health/Health Profile.md"].verdict == "match"

    def test_stale_but_live_chunk_is_still_checked_and_labelled(self, db_session):
        """A `stale` (old, but not `is_history`) chunk is NOT skipped — it's
        checked like any other live claim, and the finding carries the
        `stale` label so a reader can weigh it."""
        for n in range(30):
            self._seed_metric(db_session, 1, "resting_hr_bpm", 52.0, days_ago=n)
        db_session.flush()

        from app.integrations.embedding.models import Embedding

        old = datetime.now(timezone.utc) - timedelta(days=400)
        row = Embedding(
            source="vault",
            source_id="Health/Health Profile.md",
            user_id=1,
            chunk_text="Resting HR is ~52 bpm.",
            content_hash="hash-stale",
            metadata_json=json.dumps({"source_date": old.isoformat(), "is_history": False}),
            created_at=old,
        )
        db_session.add(row)
        db_session.commit()

        findings = _check_vault_health(db_session, path_filter=None)
        assert len(findings) == 1
        assert findings[0].stale is True
        assert findings[0].verdict == "match"  # still checked, and correct here

    def test_narrows_to_health_notes_by_default(self, db_session):
        """Without an explicit path filter, only source_ids mentioning
        'health' are scanned — an unscoped sweep would flag unrelated vault
        numbers with the same confidence."""
        from app.integrations.embedding.models import Embedding

        now = datetime.now(timezone.utc)
        db_session.add(Embedding(
            source="vault",
            source_id="Household/Renovation/Snags.md",
            user_id=1,
            chunk_text="Resting HR is ~999 bpm.",  # obviously wrong, wrong note
            content_hash="hash-unrelated",
            metadata_json=json.dumps({"source_date": now.isoformat(), "is_history": False}),
            created_at=now,
        ))
        db_session.commit()

        findings = _check_vault_health(db_session, path_filter=None)
        assert findings == []
