"""Guard: numeric thresholds/business rules in `.j2` skill templates must
trace back to a Python constant or a runtime tool call (lios#223).

Three confirmed drifts prompted this: `tasks/dupes.py`'s `DEFAULT_THRESHOLD`
(0.80) disagreed with `tunetasks.md.j2`'s hand-typed "0.85"/"0.75" duplicate
bands; `apple_health/tools.py`'s `target_strength` (2) disagreed with
`kickoff.md.j2`'s hand-typed "2/week floor"; and `services/preferences.py`'s
`daily_note.focus_count` (default 5) disagreed with `plan-week.md.j2`'s
hand-typed "capped at 5" stub comment. None of the three would have been
caught by anything until someone noticed the advice was wrong in practice —
`test_personalisation_guard.py` is the same shape of guard for a different
kind of drift (names, not numbers), and this module is deliberately modelled
on it.

**The rule**, decided in lios#223 and not to be re-opened: a numeric
threshold/business rule in a template comes from either a tool call at
runtime, or render-time templating (`{{ ... }}`, substituted by
`app.prompts.commands`) from the actual Python constant. Never a hand-typed
number.

**The check.** For every `.j2` template, for every line containing one of
`TRIGGER_WORDS` and a bare number within `WORD_WINDOW` words of it: the line
must either contain a `{{ ... }}` expression, or match an `ALLOWLIST` entry
with a stated justification. Anything else fails.

`ALLOWLIST` is deliberately short. Two shapes recur and neither is the drift
this guard exists to catch:

  1. A tool-call page-size argument (`limit=500`, `"limit": 20`, "limit of
     10") — an arbitrary how-many-rows-to-fetch value the prompt author
     chose, not a business rule with a "correct" answer sourced from a
     constant.
  2. A number that names what a *tool itself* already enforces server-side
     (`conversations_since`'s default page size, `gmail_thread`'s/
     `whatsapp_thread`'s message cap) — informational context for the
     model reading the prompt, not a number the model is asked to apply
     itself. If the underlying default ever changes, this guard has no way
     to know and the line would silently drift again; each such entry says
     so rather than pretending otherwise.

Module docstrings and code comments are not scanned (mirroring
`test_personalisation_guard.py`) — this only looks at `.j2` templates.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent / "app" / "prompts" / "templates"
)

TRIGGER_WORDS = (
    "threshold", "floor", "cap", "capped", "target",
    "limit", "minimum", "maximum",
)
_TRIGGER_RE = re.compile(r"\b(?:" + "|".join(TRIGGER_WORDS) + r")\b", re.IGNORECASE)

# A bare integer or decimal, not part of a longer identifier/date-like token
# (so `#223`, `2026-09-10`, `v4` etc. are still matched per-digit-run, same
# as any other number — dates are excluded via ALLOWLIST/word-window, not by
# trying to out-clever a general number pattern here).
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

_JINJA_EXPR_RE = re.compile(r"\{\{.*?\}\}")

WORD_WINDOW = 6

# Small and justified — see the module docstring for the two shapes covered.
# Each entry is (compiled pattern over the raw line, justification). A line
# matching any entry is exempt even with no `{{ }}` expression.
ALLOWLIST: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r'\blimit["\']?\s*[:=]\s*-?\d+(?:\.\d+)?\b|\blimit of \d+\b',
            re.IGNORECASE,
        ),
        "a tool-call page-size argument (limit=N / \"limit\": N / \"limit "
        "of N\") — an arbitrary how-many-rows-to-fetch value chosen by the "
        "prompt author, not a business-rule threshold sourced from a "
        "Python constant",
    ),
    (
        re.compile(r"\bdefault \d+(?:\.\d+)?\b", re.IGNORECASE),
        "names a default already enforced server-side by the tool being "
        "described (informational context for the model), not a number "
        "the model is asked to apply itself",
    ),
    (
        re.compile(
            r"(?=.*\bcapped 20\b)(?=.*\b(?:whatsapp_thread|gmail_thread)\b)",
            re.IGNORECASE,
        ),
        "20 is whatsapp_thread's/gmail_thread's own server-side "
        "default_limit (app/integrations/{whatsapp,google_mail}/tools.py) "
        "— informational, not a number the model applies",
    ),
    (
        re.compile(r"\bList \d+'s own cap\b", re.IGNORECASE),
        "the digit is an ordinal reference to a numbered list this same "
        "command already built ('List 2'), not a threshold — the actual "
        "cap being described is `conversations_since`'s own `limit`, "
        "already covered by the pagination-limit allowlist entry above",
    ),
]


def _template_paths() -> list[Path]:
    return sorted(TEMPLATES_DIR.glob("*.md.j2"))


def _word_positions(line: str) -> list[tuple[int, int, str]]:
    """(start_word_index, end_word_index, token) for every whitespace-split
    token, so trigger/number proximity can be measured in words rather than
    characters — a number two paragraphs away in the same wrapped line is a
    different thing from one stapled to the trigger word."""
    return [(i, i, tok) for i, tok in enumerate(line.split())]


def _violations_on_line(line: str) -> list[str]:
    """Bare numbers within WORD_WINDOW words of a trigger word on this raw
    line, as the matched number strings — empty if the line is clean."""
    if _JINJA_EXPR_RE.search(line):
        return []
    for pattern, _reason in ALLOWLIST:
        if pattern.search(line):
            return []

    tokens = line.split()
    trigger_idx = [i for i, tok in enumerate(tokens) if _TRIGGER_RE.search(tok)]
    if not trigger_idx:
        return []

    number_idx = [
        (i, tok) for i, tok in enumerate(tokens) if _NUMBER_RE.search(tok)
    ]
    if not number_idx:
        return []

    hits = []
    for i, tok in number_idx:
        if any(abs(i - t) <= WORD_WINDOW for t in trigger_idx):
            hits.append(tok)
    return hits


def _scan(path: Path) -> dict[int, list[str]]:
    """{1-based line number: [offending number tokens]} for one template."""
    out: dict[int, list[str]] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        hits = _violations_on_line(line)
        if hits:
            out[lineno] = hits
    return out


class TestNoBareThresholds:
    """The mechanical guard itself, one template at a time."""

    @pytest.mark.parametrize("path", _template_paths(), ids=lambda p: p.name)
    def test_no_bare_numeric_thresholds(self, path: Path):
        hits = _scan(path)
        assert not hits, (
            f"{path.name} hand-types a number next to "
            f"{'/'.join(TRIGGER_WORDS)} instead of sourcing it from a tool "
            "call or a render-time `{{ }}` constant (lios#223): "
            + "; ".join(
                f"line {lineno}: {tokens}" for lineno, tokens in sorted(hits.items())
            )
            + ". Either template it in via app.prompts.commands (see "
            "dupes_threshold/dupes_related_threshold), switch the "
            "instruction to a tool call that returns the value at runtime, "
            "or add a justified ALLOWLIST entry in "
            "tests/test_template_constants.py if it's genuinely not a "
            "business-rule threshold."
        )

    def test_templates_dir_is_not_empty(self):
        # A guard over an empty glob passes vacuously and proves nothing —
        # same trap `test_personalisation_guard.py`'s own meta-test guards
        # against for its tool sweep.
        assert len(_template_paths()) >= 10


class TestMutationCheck:
    """Prove the guard actually fails on the drifts it was built for —
    otherwise a green suite here is as trustworthy as no suite at all (see
    user-memory `feedback_mutation_check_your_tests.md`)."""

    def test_flags_reinserted_dupes_call_argument(self):
        line = (
            "- `tasks_duplicates(threshold=0.75)` — wider than the default, "
            "so related-work pairs surface too."
        )
        assert _violations_on_line(line), (
            "the guard must flag a hand-typed threshold= call argument "
            "with no {{ }} expression"
        )

    def test_does_not_flag_prose_with_no_trigger_word(self):
        # Known, documented limitation (see the module docstring): the
        # original lios#223 duplicate-band prose ("Above 0.85 is usually the
        # same task...") never used the literal word "threshold", so a
        # keyword-adjacency scan cannot catch it by wording alone — only the
        # `tasks_duplicates(threshold=0.75)` call argument nearby did. This
        # test pins that limitation rather than silently relying on it.
        line = (
            "Above 0.85 is usually the same task said twice: propose "
            "`tasks_merge`, naming which survives and why."
        )
        assert not _violations_on_line(line)

    def test_flags_reinserted_exercise_floor(self):
        line = (
            "movement (from health_trends), exercise (from health_workouts, "
            "read against the 2/week floor), listening"
        )
        assert _violations_on_line(line), (
            "the guard must flag a hand-typed exercise-floor number with no "
            "{{ }} expression"
        )

    def test_flags_reinserted_focus_cap(self):
        line = "<!-- Manual picks, set at lock-in, capped at 5. -->"
        assert _violations_on_line(line), (
            "the guard must flag a hand-typed focus-cap number with no "
            "{{ }} expression"
        )

    def test_does_not_flag_templated_dupes_threshold(self):
        line = (
            "At or above `{{ dupes_threshold }}` is usually the same task "
            "said twice"
        )
        assert not _violations_on_line(line)

    def test_does_not_flag_allowlisted_pagination_limit(self):
        line = '`tasks_history(limit=300)` — every completion and field change this week'
        assert not _violations_on_line(line)
