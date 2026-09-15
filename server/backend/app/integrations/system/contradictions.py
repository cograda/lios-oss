"""Contradiction detector, narrow (Wave 2 R6).

Numeric/status claims in prose, diffed against measured values. Two seed
cases drove the scope:

1. **The Health Profile HRV rule.** `vault/Health/Health Profile.md` used to
   claim same-day `hr_min`/`avg`/`max` collapsing to one value was the
   same-day-placeholder tell. Measurement showed the collapse happens on
   EVERY day, settled or not — Apple Health stores one Min/Avg/Max entry per
   day regardless. The note has since been corrected in place (2026-08-25),
   but nothing stops the next hand-typed number in that note (or any other)
   from drifting the same way, silently, the way "119 tools" sat wrong for
   weeks before someone measured (see `core/CLAUDE.md`'s own "measure, never
   quote" history).
2. **CLAUDE.md counts.** "126 tools across 26 integrations", "27 integration
   packages", "2,130 tests" — all measurable from the running registry, none
   of them re-checked when the code they describe changes.

This module is deliberately narrow, per the backlog item: numeric claims of
the shape "<number> <noun>" against a small allowlist of measurable nouns,
diffed against a live measurement, with a documented relative tolerance per
noun. A stated *pattern* rule ("min==avg==max means same-day") is a harder
problem — it needs a rule DSL, not a number diff — and is out of scope here;
the corrected note already states the true rule in prose, which is exactly
what this narrower detector CAN check going forward if it drifts again.

## Two sources, two different shapes of "measured"

- `claude_md` — the repo-tracked `CLAUDE.md` files under `core/` (currently
  `core/CLAUDE.md` and `core/server/CLAUDE.md`; found by walking up from this
  file's own path with `Path(__file__)`, never a hardcoded absolute path —
  see the root `lios/CLAUDE.md`'s "ask what reads it from a fixed path").
  ⚠️ **These files are not in the deployed image.** The Dockerfile only
  `COPY`s `backend/`, `frontend/dist/` and `client-dist/` — neither
  `core/CLAUDE.md` nor `core/server/CLAUDE.md` ships. In production this
  source finds zero files and says so; it is a dev-time check, run from a
  full checkout. That is a real constraint of "measure, never quote" living
  in the same repo as the docs it measures, not a bug in this detector.
  Measured against the live process: integration count (directories on
  disk), tool count (the registry), table count (`Base.metadata`). Test
  counts are **not** measured — running `pytest --collect-only` from inside
  a test run is neither cheap nor safe, so a test-count claim is reported
  `unmeasured` rather than silently skipped or (worse) hung on.
- `vault_health` — the CALLING user's own vault chunks (via the unified
  `embeddings` table, `source="vault"`, scoped to `Embedding.user_id`, never
  a raw file read) whose `source_id` mentions "health". Reusing the chunk
  table rather than reading files off disk is what makes R4's provenance
  fields (`source_date`, `is_history`, `stale` — see
  `app.services.embedding._chunk_provenance`) available for free: a claim
  living in an `.stversions` snapshot or an old dated report is **not** a
  contradiction just because the number in it doesn't match today's
  measurement — it's history, and is dropped before ever becoming a
  `Finding`. A `stale` (old but not `is_history`) chunk's claim IS still
  checked and reported — `stale` is a label on the finding, never a reason
  to skip it (an old chunk can still describe the live file, and its number
  can still be wrong right now).

## Cross-package access

`system` may not import another integration's internals directly (V4 chunk
4.2 — see `tools.py`'s module docstring and `tests/test_capability_boundaries
.py`). Health aggregates go through the already-declared `health.query`
capability (`app.integrations.apple_health.facade.AppleHealthFacade`, already
in this package's `manifest.py::depends_on`). Vault chunks and the
integration registry are read at the kernel level — `app.services.embedding`
(the same module `tools.py::_index_state` already reaches into) and
`app.integrations` (the registry itself, not `app.integrations.<pkg>`
internals) — neither is "another integration's internals" in the sense the
boundary test polices; both are the same kind of kernel-level read this
package already does elsewhere.

Nothing here calls out to an LLM. Extraction is regex; comparison is
arithmetic. That is the point — a contradiction detector whose own
measurements are model output would be exactly the kind of thing it exists
to catch.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.auth.context import current_user_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


@dataclass
class Claim:
    """One extracted "<number> <noun>" claim from a piece of prose."""

    noun: str
    value: float
    unit: str | None
    # Only set when the claim itself names an averaging window (e.g. the
    # "7-day avg" in "HRV ... 7-day avg ~27 ms") — lets the measurer match
    # the window the claim actually describes instead of guessing one.
    window_days: int | None
    raw: str      # the matched text, e.g. "27 ms" or "126 tools"
    context: str  # a short surrounding excerpt, for display


_NUM = r"[0-9][0-9,]*(?:\.[0-9]+)?"


def _num(m: "re.Match", group: int) -> float:
    return float(m.group(group).replace(",", ""))


def _simple(group: int = 1) -> Callable[["re.Match"], tuple[float, int | None]]:
    return lambda m: (_num(m, group), None)


def _windowed(window_group: int, value_group: int) -> Callable[["re.Match"], tuple[float, int | None]]:
    return lambda m: (_num(m, value_group), int(_num(m, window_group)))


def _hours_minutes(hour_group: int, min_group: int) -> Callable[["re.Match"], tuple[float, int | None]]:
    def _parse(m: "re.Match") -> tuple[float, int | None]:
        hours = _num(m, hour_group)
        minutes = _num(m, min_group) if m.group(min_group) else 0.0
        return round(hours + minutes / 60.0, 2), None
    return _parse


@dataclass(frozen=True)
class _Pattern:
    noun: str
    regex: re.Pattern
    parse: Callable[["re.Match"], tuple[float, int | None]]
    unit: str | None


# Order matters: earlier patterns claim their matched span first, so a more
# specific pattern for a noun (e.g. the windowed HRV form) is listed before
# any looser fallback that could otherwise also match inside the same text.
_PATTERNS: list[_Pattern] = [
    # --- CLAUDE.md-shaped counts -------------------------------------------
    _Pattern("tools", re.compile(rf"({_NUM})\s+tools\b", re.I), _simple(), None),
    # Matches both "26 integrations" and "27 integration packages" — the
    # brief's allowlist names the noun "integrations"; CLAUDE.md's own prose
    # uses both phrasings for the same count.
    _Pattern(
        "integrations",
        re.compile(rf"({_NUM})\s+integration(?:s\b|\s+packages?\b)", re.I),
        _simple(),
        None,
    ),
    _Pattern("tables", re.compile(rf"({_NUM})\s+tables\b", re.I), _simple(), None),
    _Pattern("tests", re.compile(rf"({_NUM})\s+tests\b", re.I), _simple(), None),
    _Pattern("chunks", re.compile(rf"({_NUM})\s+chunks\b", re.I), _simple(), None),
    _Pattern("entities", re.compile(rf"({_NUM})\s+entities\b", re.I), _simple(), None),
    # --- Health Profile-shaped claims ---------------------------------------
    # The explicit "<N>-day avg ~<X> ms" form near the word HRV — this is
    # the shape Health Profile.md actually uses ("HRV | 19-43 ms, 7-day avg
    # ~27 ms") and it's the only HRV shape this detector checks: the bare
    # min-max range either side of it is a range, not a point estimate, and
    # diffing a range against a single measured average is a different (and
    # noisier) problem than this narrow pass takes on.
    _Pattern(
        "hrv_ms",
        re.compile(rf"HRV\b[^\n]{{0,80}}?({_NUM})-day\s+avg\D{{0,10}}({_NUM})\s*ms\b", re.I),
        _windowed(1, 2),
        "ms",
    ),
    # Resting HR, with an optional explicit window ("30-day avg ~52 bpm") or
    # bare ("resting HR is ~52").
    _Pattern(
        "resting_hr",
        re.compile(
            rf"resting\s+(?:heart\s+rate|hr)[^\n]{{0,20}}?({_NUM})-day\s+avg\D{{0,10}}({_NUM})\s*bpm\b",
            re.I,
        ),
        _windowed(1, 2),
        "bpm",
    ),
    _Pattern(
        "resting_hr",
        re.compile(rf"resting\s+(?:heart\s+rate|hr)[^0-9]{{0,15}}({_NUM})\s*(?:bpm)?\b", re.I),
        _simple(),
        "bpm",
    ),
    # Sleep: "7h30 average sleep" and "7.5 hours of average sleep".
    _Pattern(
        "sleep_hours",
        re.compile(rf"({_NUM})h([0-9]{{1,2}})?\s*(?:average\s+)?sleep\b", re.I),
        _hours_minutes(1, 2),
        "hours",
    ),
    _Pattern(
        "sleep_hours",
        re.compile(rf"({_NUM})\s*(?:hours?|hrs?)\s+(?:of\s+)?(?:average\s+)?sleep\b", re.I),
        _simple(),
        "hours",
    ),
]

# Relative tolerance per noun — how far a claim may sit from the live
# measurement before it's a contradiction rather than drift/rounding.
#
# Counts default to 5%: a documented snapshot count ("measured 2026-08-29")
# is expected to drift by a unit or two as the codebase moves under it
# without becoming wrong, but "119" against a live 134 (11% off) is exactly
# the kind of gap this module exists to catch — see core/server/CLAUDE.md's
# own multi-paragraph history of that drift.
#
# Health aggregates get a wider tolerance because the underlying quantity is
# genuinely noisy day to day, not because the claim is allowed to be more
# wrong: HRV in particular swings well outside 15% between good and bad
# recovery days (Health Profile.md's own worked example: 37.6 average vs
# 18 on a bad night, a 2x swing that is not a data error).
TOLERANCE: dict[str, float] = {
    "tools": 0.05,
    "integrations": 0.05,
    "tables": 0.05,
    "tests": 0.05,
    "chunks": 0.05,
    "entities": 0.05,
    "resting_hr": 0.15,
    "hrv_ms": 0.25,
    "sleep_hours": 0.20,
}

_CODE_NOUNS = {"tools", "integrations", "tables", "tests", "chunks", "entities"}
_HEALTH_NOUNS = {"resting_hr", "hrv_ms", "sleep_hours"}


def extract_numeric_claims(text: str) -> list[Claim]:
    """Extract "<number> <noun>" claims from `text` against the noun allowlist.

    Deterministic regex only — no LLM involved anywhere in this module (see
    the module docstring). Overlapping matches are resolved in `_PATTERNS`
    order: the first pattern to claim a span wins, so list a more specific
    pattern before a looser fallback for the same noun.
    """
    if not text:
        return []
    claims: list[Claim] = []
    claimed_spans: list[tuple[int, int]] = []
    for pat in _PATTERNS:
        for m in pat.regex.finditer(text):
            span = m.span()
            if any(a < span[1] and span[0] < b for a, b in claimed_spans):
                continue
            value, window_days = pat.parse(m)
            start = max(0, span[0] - 40)
            end = min(len(text), span[1] + 40)
            claims.append(Claim(
                noun=pat.noun,
                value=value,
                unit=pat.unit,
                window_days=window_days,
                raw=m.group(0).strip(),
                context=" ".join(text[start:end].split()),
            ))
            claimed_spans.append(span)
    return claims


# ---------------------------------------------------------------------------
# Measurers — code side (claude_md source)
# ---------------------------------------------------------------------------


def _measure_tools() -> float:
    """Live MCP tool count, the same call the CLAUDE.md files themselves
    document as the correct way to check this ("measure, never quote")."""
    import app.integrations as I

    I.register_all()
    return float(sum(len(i.mcp_tools()) for i in I.get_all().values()))


def _integrations_dir() -> Path:
    # This file: app/integrations/system/contradictions.py
    # parents[0] = system, parents[1] = integrations.
    return Path(__file__).resolve().parents[1]


def _measure_integrations() -> float:
    """Count of integration packages on disk — a directory under
    `app/integrations/` carrying its own `manifest.py`. Excludes `_template`
    and any dunder/hidden directory, neither of which is a shipped
    integration."""
    root = _integrations_dir()
    count = 0
    for p in root.iterdir():
        if not p.is_dir() or p.name.startswith("_") or p.name.startswith("."):
            continue
        if (p / "manifest.py").is_file():
            count += 1
    return float(count)


def _measure_tables() -> float:
    """Live table count from `Base.metadata` — every integration's models are
    imported as a side effect of `app.models` (manifest-driven discovery, see
    core/server/CLAUDE.md's "Model Registration"), so this reflects the whole
    schema, not just kernel-owned tables."""
    import app.models  # noqa: F401 — import for side effect: registers every model on Base.metadata
    from coglib import Base

    return float(len(Base.metadata.tables))


def _measure_chunks(session: Session) -> float:
    """Live row count of the unified `embeddings` table — the same table
    `tools.py::_index_state` already reads for the R4 index-state axis."""
    from sqlalchemy import func

    from app.services.embedding import Embedding

    return float(session.query(func.count(Embedding.id)).scalar() or 0)


# A measurer returns `(value, skip_reason)`. `skip_reason` set means "don't
# even try to compare this claim" — the claim is reported `unmeasured`
# rather than silently dropped, so a caller can see that lios knows about
# the claim but doesn't (yet) have a cheap, safe way to check it.
_CODE_MEASURERS: dict[str, Callable[[Session], tuple[float | None, str | None]]] = {
    "tools": lambda session: (_measure_tools(), None),
    "integrations": lambda session: (_measure_integrations(), None),
    "tables": lambda session: (_measure_tables(), None),
    "chunks": lambda session: (_measure_chunks(session), None),
    "tests": lambda session: (
        None,
        "test collection is not run automatically here — `pytest --collect-only` "
        "is not 'cheap' to run inside a live request, so test-count claims are "
        "reported unmeasured rather than executed or silently skipped",
    ),
    "entities": lambda session: (
        None,
        "no capability-safe live entity count is exposed today — the "
        "homeassistant facade has no count method, only entity/state reads; "
        "adding one is a small separate change, not part of this narrow pass",
    ),
}


# ---------------------------------------------------------------------------
# Measurers — health side (vault_health source)
# ---------------------------------------------------------------------------


_DEFAULT_WINDOW_DAYS = {"resting_hr": 30, "hrv_ms": 7, "sleep_hours": 7}


def _measure_health_noun(session: Session, noun: str, window_days: int | None) -> tuple[float | None, str | None]:
    """Measure a health aggregate through the `health.query` capability —
    never `apple_health`'s internals directly (see module docstring)."""
    from app.plugin.capabilities import get_capability

    days = window_days or _DEFAULT_WINDOW_DAYS[noun]
    health = get_capability("health.query")

    if noun in ("resting_hr", "hrv_ms"):
        metric = "resting_hr_bpm" if noun == "resting_hr" else "hrv_ms"
        try:
            data = json.loads(health.trends(session, {"days": days, "metric": metric}))
        except Exception:  # noqa: BLE001
            logger.exception("contradiction detector: health.trends failed for %s", metric)
            return None, "health.trends call failed"
        avg = data.get("period_averages", {}).get(metric)
        return (float(avg), None) if avg is not None else (None, f"no {metric} data in the last {days} days")

    if noun == "sleep_hours":
        uid = current_user_id()
        vals: list[float] = []
        today = date.today()
        for n in range(1, days + 1):
            hours = health.slept_hours(session, user_id=uid, night=today - timedelta(days=n))
            if hours is not None:
                vals.append(hours)
        if not vals:
            return None, f"no sleep sessions in the last {days} days"
        return round(sum(vals) / len(vals), 2), None

    return None, f"no measurer for {noun!r}"


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    claim_text: str
    source_path: str
    source_date: str | None
    is_history: bool
    claimed: float
    measured: float | None
    tolerance: float
    verdict: str  # "match" | "contradiction" | "unmeasured"
    noun: str
    stale: bool = False
    skip_reason: str | None = None


def _verdict(claimed: float, measured: float | None, tolerance: float) -> str:
    if measured is None:
        return "unmeasured"
    denom = max(abs(measured), 1e-9)
    diff = abs(claimed - measured) / denom
    return "match" if diff <= tolerance else "contradiction"


def _claude_md_paths() -> list[Path]:
    """Repo-tracked `CLAUDE.md` files under `core/` — resolved from this
    file's own path, never a hardcoded absolute path (see the root
    `lios/CLAUDE.md`'s "ask what reads it from a fixed path"). Filters to
    files that actually exist: these are NOT copied into the deployed image
    (see module docstring), so in production this legitimately returns an
    empty list rather than raising."""
    here = Path(__file__).resolve()
    try:
        server_dir = here.parents[4]  # .../core/server
        core_dir = here.parents[5]    # .../core
    except IndexError:
        return []
    candidates = [core_dir / "CLAUDE.md", server_dir / "CLAUDE.md"]
    return [p for p in candidates if p.is_file()]


def _check_claude_md(session: Session, path_filter: str | None) -> list[Finding]:
    findings: list[Finding] = []
    for p in _claude_md_paths():
        if path_filter and path_filter not in str(p):
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            logger.warning("contradiction detector: could not read %s", p)
            continue
        for claim in extract_numeric_claims(text):
            if claim.noun not in _CODE_NOUNS:
                continue
            measured, skip_reason = _CODE_MEASURERS[claim.noun](session)
            tolerance = TOLERANCE[claim.noun]
            verdict = "unmeasured" if skip_reason else _verdict(claim.value, measured, tolerance)
            findings.append(Finding(
                claim_text=claim.raw,
                source_path=str(p),
                source_date=None,
                is_history=False,
                claimed=claim.value,
                measured=measured,
                tolerance=tolerance,
                verdict=verdict,
                noun=claim.noun,
                skip_reason=skip_reason,
            ))
    return findings


def _check_vault_health(session: Session, path_filter: str | None) -> list[Finding]:
    """Numeric health claims from the caller's own vault chunks.

    Reads the unified `embeddings` table directly (`source="vault"`, scoped
    to this caller's `user_id`) rather than the vault filesystem, because R4's
    provenance (`source_date`/`is_history`/`stale`) lives on the chunk, not
    on the file — see module docstring.

    A chunk flagged `is_history` (an `.stversions` snapshot, a dated,
    never-edited `Reports/` entry, etc.) is skipped entirely: its number
    disagreeing with today's measurement is not a contradiction, it's the
    historical record doing its job. `stale` (old but not `is_history`) is
    the opposite case — it's still checked, just labelled, because an old
    *live* chunk can still describe a current file whose number really is
    wrong right now.
    """
    from app.services.embedding import Embedding, _chunk_provenance, _recency_settings

    uid = current_user_id()
    _half_life_days, staleness_threshold_days = _recency_settings()

    q = session.query(Embedding).filter(Embedding.source == "vault", Embedding.user_id == uid)
    if path_filter:
        q = q.filter(Embedding.source_id.ilike(f"%{path_filter}%"))
    else:
        # Narrow to start (per the backlog item): health-relevant notes only.
        # An unscoped sweep of the whole vault would flag numbers in the
        # renovation snag register with the same confidence as a Health
        # Profile claim, which is exactly the "detector without staleness
        # metadata just generates noise" trap the brief calls out.
        q = q.filter(Embedding.source_id.ilike("%health%"))

    findings: list[Finding] = []
    for row in q.all():
        claims = extract_numeric_claims(row.chunk_text or "")
        if not claims:
            continue
        prov = _chunk_provenance(row.metadata_json, row.created_at, staleness_threshold_days)
        if prov["is_history"]:
            continue
        for claim in claims:
            if claim.noun not in _HEALTH_NOUNS:
                continue
            measured, skip_reason = _measure_health_noun(session, claim.noun, claim.window_days)
            tolerance = TOLERANCE[claim.noun]
            verdict = "unmeasured" if skip_reason else _verdict(claim.value, measured, tolerance)
            findings.append(Finding(
                claim_text=claim.raw,
                source_path=row.source_id,
                source_date=prov["source_date"],
                is_history=False,
                claimed=claim.value,
                measured=measured,
                tolerance=tolerance,
                verdict=verdict,
                noun=claim.noun,
                stale=prov["stale"],
                skip_reason=skip_reason,
            ))
    return findings


def find_contradictions(
    session: Session, sources: list[str] | None = None, path: str | None = None
) -> list[Finding]:
    """Diff numeric/status claims from `sources` against live measurements.

    `sources`: any of "claude_md", "vault_health". Defaults to both.
    `path`: optional substring filter on the source path/id (e.g. a single
    CLAUDE.md file, or a single vault note).
    """
    sources = sources or ["claude_md", "vault_health"]
    findings: list[Finding] = []
    if "claude_md" in sources:
        findings.extend(_check_claude_md(session, path))
    if "vault_health" in sources:
        findings.extend(_check_vault_health(session, path))
    return findings


# ---------------------------------------------------------------------------
# MCP tool handler
# ---------------------------------------------------------------------------


def handle_contradictions(session: Session, arguments: dict[str, Any]) -> str:
    """Read-only: numeric/status claims in prose, diffed against measured
    values. See module docstring for sources, nouns and tolerances."""
    sources = arguments.get("sources") or ["claude_md", "vault_health"]
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]
    path = arguments.get("path") or None

    findings = find_contradictions(session, sources=sources, path=path)
    contradictions = [f for f in findings if f.verdict == "contradiction"]
    unmeasured = [f for f in findings if f.verdict == "unmeasured"]

    if not findings:
        summary = "No numeric/status claims matched the noun allowlist in the given sources."
    else:
        summary = (
            f"{len(contradictions)} contradiction(s), {len(unmeasured)} unmeasured, "
            f"out of {len(findings)} claim(s) checked across {sources}."
        )
        if contradictions:
            summary += " Contradictions: " + "; ".join(
                f"{Path(f.source_path).name}: \"{f.claim_text}\" (measured {f.measured})"
                for f in contradictions[:5]
            )

    return json.dumps({
        "summary": summary,
        "findings": [asdict(f) for f in findings],
    }, indent=2)
