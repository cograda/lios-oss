"""Deterministic text fragments for the daily note that must never depend on
the composing model's judgement — the "R1" fixes (Backlog: "The daily-note
briefing has no verification step").

Two fragments live here, and both share the same shape of bug: a section
that is silently *absent* reads identically whether nothing happened or
nobody looked, and the reader cannot tell which. Pinning the exact wording in
code (rather than leaving it to a Haiku subagent to phrase fresh every
morning) makes the behaviour testable and stops it drifting note to note.

Neither fragment queries the database directly — callers (the `/kickoff`
and `/checkin` command flows) pass in whatever they already fetched, so this
module stays a pure formatter and needs no session, no facade, and no new
capability wiring.
"""

from __future__ import annotations

# Rendered verbatim into the note's `### Focus` section when nothing has been
# locked in yet. Exact wording is pinned here (not left to the Haiku
# subagent) so "empty" always reads as a deliberate, visible state — see
# Backlog: "empty Focus is invisible" (found 2026-08-19: the 16th, 17th and
# 18th all had zero Focus items and the note rendered identically to a day
# where Focus had simply been achieved).
EMPTY_FOCUS_LINE = (
    "_No Focus items set yet today. Run `/tunetasks` for candidates from the "
    "ledger, or say \"lock in <items>\" now._"
)


def focus_section(focus_titles: list[str]) -> str:
    """Render the `### Focus` section body.

    `focus_titles` is whatever the caller already resolved as today's locked
    -in picks (from the ledger's focus queue, or from the note being built
    for the first time before lock-in has happened). An empty list renders
    the explicit line above, never an absent section — that is the whole
    point of this function existing.
    """
    if not focus_titles:
        return EMPTY_FOCUS_LINE
    return "\n".join(f"- [ ] {title}" for title in focus_titles)


def vault_guard_section(guard_stdout: str, exit_code: int) -> str:
    """Render the `## Vault guard` section from `vault/.tools/vault_guard.py`'s
    own output.

    Returns "" (nothing to render — the caller should omit the heading
    entirely) when the guard reported clean. On a finding, the guard's own
    stdout is already the message a human needs (file paths, byte-size
    verdicts); this wraps it under a clearly labelled heading rather than
    re-parsing and reformatting it, because re-deriving the verdict here
    would be a second copy of logic the script already owns, and a rescue
    copy (a `.sync-conflict-*` file) is exactly the kind of thing that must
    not be misreported by an intermediate parser (Backlog: "find the writer
    that truncates vault files to 0 bytes — and make conflicts visible the
    next morning").
    """
    if exit_code == 0:
        return ""
    body = guard_stdout.strip()
    return (
        "## Vault guard\n\n"
        "⚠️ The vault integrity check found something that needs a human look "
        "(a sync conflict or a zero-byte note). Verbatim from "
        "`vault_guard.py`:\n\n"
        "```\n"
        f"{body}\n"
        "```\n"
    )
