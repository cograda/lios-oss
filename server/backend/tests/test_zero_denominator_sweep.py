""""Honest numbers" sweep (Backlog § A — `finance_summary` reports
`categorization_coverage: 100.0` over zero transactions).

The confirmed bug was one line in `finance/tools.py`:
`round((total_count - uncat_count) / total_count * 100, 1) if total_count
else 100.0` — the empty branch returns the *best possible* value, so an
empty period (a missing import) reads as a perfectly tidy one. The backlog
item asked for a sweep, not a one-liner: any percentage/coverage/ratio/rate
field with the same `if <denominator> else <flattering constant>` shape
carries the same bug.

Full sweep result (grep for `else (100|100.0|1.0|0.0|0)` plus every
`coverage`/`pct`/`percent`/`ratio`/`rate` output field under `app/`, and the
dashboard/loops BFFs):

  finance_summary.categorization_coverage   FLATTERING (100.0) — already
      fixed to `None` (this module, `finance/tools.py`), covered by
      `TestFinanceCategorizationCoverageNull` in test_coverage_null_on_empty.py
  lastfm_enrich.coverage                    FLATTERING ("0%") — already
      fixed to `None` (`lastfm/tools.py`), covered by
      `TestLastfmCoverageNull` in test_coverage_null_on_empty.py
  gmail_stats.embedding_coverage            FLATTERING (0) — already fixed
      to `None` (`google_mail/tools.py`), covered by
      `TestGmailEmbeddingCoverageNull` in test_coverage_null_on_empty.py
  finance_compare.change_pct                already `None` on `b_total <= 0`
      (`finance/services.py`) — not a bug, no fix needed
  system_info.disk.percent_used             FLATTERING (0) — fixed here
      (`routes/system.py`) — practically unreachable (statvfs reporting
      `f_blocks == 0`) but the same shape, fixed defensively
  tool_stats.error_rate                     FLATTERING (0.0) — fixed here
      (`routes/system.py`) — dead branch (a GROUP BY row's `calls` is
      always >= 1) but fixed defensively, plus the result sort hardened
      against a `None` rate
  system_ai_usage totals/unpriced_calls     NOT A PERCENTAGE — `cost_usd`
      is already `None`-on-unknown by construction (never computed as a
      ratio); `unpriced_calls`/`calls` are honest counts, not fabricated
      good values
  apple_health per-record `round(x, n) if x else 0`
                                             HONEST DEFAULT — a missing
      single field (not a count/denominator ratio); 0 distance/energy/hr
      does not read as "fully measured"
  algo/estimators.py mean-of-empty-list `else 0.0`
                                             NOT A PERCENTAGE — a fitted
      model mean, not a coverage/ratio metric; an empty training set is
      already rejected before `fit()` is reached in the harness
  finance/services.py subscription `confidence` `else 0`
                                             HONEST DEFAULT — 0 is the
      *worst* possible confidence, not the best; callers already drop
      anything below 0.3
  finance/services.py `amount = -debit if ... else credit if ... else 0.0`
                                             NOT A PERCENTAGE — a literal
      amount fallback, not a ratio
  apps/dashboard/backend, apps/loops/backend
                                             CHECKED, no `if <denominator>
      else <constant>` percentage/coverage/ratio/rate shape found

This file adds the cross-tool regression the backlog item asked for: one
parametrised test hitting every *fixed* tool/function with an empty input
and asserting the metric comes back `None`, plus an explicit
`finance_summary`-on-an-empty-period regression (mutation-checked: putting
the literal `100.0` back into `finance/tools.py` and re-running this file
was confirmed to fail `test_finance_summary_empty_period_is_null`, then
reverted).
"""

import json

import pytest

pytestmark = pytest.mark.db


def _finance_empty(db_session):
    from app.integrations.finance.tools import handle_summary

    payload = json.loads(handle_summary(db_session, {"period": "this_month"}))
    assert payload["transaction_count"] == 0
    return payload["categorization_coverage"]


def _lastfm_empty(db_session, monkeypatch):
    from app.integrations.lastfm import sync as lastfm_sync
    from app.integrations.lastfm import tools as lastfm_tools
    from app.auth.context import use_user

    monkeypatch.setattr(lastfm_sync, "enrich_artist_tags", lambda session, limit: 0)
    with use_user(1):
        payload = json.loads(lastfm_tools.handle_enrich(db_session, {}))
    assert payload["total_artists"] == 0
    return payload["coverage"]


def _gmail_empty(db_session, monkeypatch):
    from app.integrations.google_mail.tools import _gmail_stats_compute
    from app.auth.context import use_user

    with use_user(1):
        stats = _gmail_stats_compute(db_session, {})
    assert stats["total_messages"] == 0
    return stats["embedding_coverage"]


def _disk_zero_total(db_session, monkeypatch):
    """`system_info`'s disk block on a filesystem statvfs reports as empty."""
    import os
    from app.routes.system import system_info

    class _FakeStat:
        f_blocks = 0
        f_frsize = 4096
        f_bavail = 0

    monkeypatch.setattr(os, "statvfs", lambda path: _FakeStat())
    import asyncio
    payload = asyncio.run(system_info())
    assert payload["disk"]["total_gb"] == 0
    return payload["disk"]["percent_used"]


@pytest.mark.parametrize(
    "empty_case",
    ["finance", "lastfm", "gmail", "disk"],
)
def test_zero_denominator_metric_is_null(empty_case, db_session, monkeypatch):
    """Every fixed coverage/ratio field returns `None` on an empty input.

    Never `100.0`, never `0`, never `"0%"` — a flattering constant is
    indistinguishable from a genuinely measured, perfectly clean result.
    """
    if empty_case == "finance":
        metric = _finance_empty(db_session)
    elif empty_case == "lastfm":
        metric = _lastfm_empty(db_session, monkeypatch)
    elif empty_case == "gmail":
        metric = _gmail_empty(db_session, monkeypatch)
    elif empty_case == "disk":
        metric = _disk_zero_total(db_session, monkeypatch)
    else:
        raise AssertionError(empty_case)

    assert metric is None


def test_finance_summary_empty_period_is_null(db_session):
    """Regression, named for the exact backlog symptom.

    A period with zero transactions must never report
    `categorization_coverage: 100.0` — that reads as "a perfectly tidy
    month" for a month with no data at all, and the less data there is the
    healthier the number looks. Mutation-checked: reinstating
    `if total_count else 100.0` in `app/integrations/finance/tools.py` was
    confirmed to fail this assertion, then reverted.
    """
    from app.integrations.finance.tools import handle_summary

    payload = json.loads(handle_summary(db_session, {"period": "this_month"}))
    assert payload["transaction_count"] == 0
    assert payload["total_income"] == 0.0
    assert payload["total_expense"] == 0.0
    assert payload["categorization_coverage"] is None
