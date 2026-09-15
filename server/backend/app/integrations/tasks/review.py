"""Is this line actually a next action?

The one rule the ledger cannot enforce with a column: **a task states one
physical action you could start now, without deciding anything else first.**
A schema can reject a bad priority; it cannot reject a bad sentence.

So this is a heuristic, and it is wrong a lot. Two consequences are designed
in rather than apologised for:

  1. **Nothing here ever auto-fixes anything.** Every finding goes to a human
     who reads the task and rules on it. A rewrite is a judgement about what
     the work actually is, and no regex holds that.
  2. **Callers must report precision, not just the count.** A raw "47 tasks
     flagged" reads as "47 problems" and is not. `summarise()` exists to make
     the honest framing the easy one.

Adapted from a sanitised export of a parallel implementation (`taskdb`,
2026-08-31), with its verb lists kept and three changes:

  - **Flag names are the plain-English ones.** That export stored `container`
    and `compound` and relabelled them in the browser, having found the words
    "meant nothing at a glance". A name a UI has to translate is a name that
    will eventually be shown untranslated.
  - **`no_action` is new.** A bare noun phrase ("Kitchen quotes", "Car tax")
    is the most common non-action in a household backlog, and matching on
    absent verbs catches what a verb blocklist by construction cannot.
  - **Leading markup is skipped** before anchoring. Our titles routinely open
    with a `[[wikilink]]`, so an `^`-anchored verb test would silently never
    fire on exactly the project-linked tasks most likely to be containers.
"""

from __future__ import annotations

import re

# Verbs that name an area of concern rather than a move. You cannot start any
# of these without first deciding something else.
CONTAINER_VERBS = (
    "work through", "work out", "spec", "scope", "sort out", "look at",
    "look into", "figure out", "think about", "own", "drive", "improve",
    "explore", "progress", "advance", "continue", "keep on top of",
    "stay across", "manage", "handle", "deal with", "investigate",
    "consider", "review the open", "chase up on", "get across",
    "get to grips with", "tidy up", "go through",
)

# Real imperatives. Used two ways: several of them in one clause means several
# actions, and *none* of them anywhere means no action was named at all.
ACTION_VERBS = (
    "write|send|build|ship|schedule|ask|confirm|read|draft|set up|pull|cut|"
    "fix|share|check|raise|take|get|meet|decide|run|add|remove|move|close|"
    "open|file|chase|book|call|reply|publish|merge|test|submit|introduce|"
    "reset|follow up|give|catch up|expose|compile|sit down|change|reopen|"
    "prep|set|define|answer|produce|analyse|analyze|compare|tell|brief|turn|"
    "start|create|update|land|push|point|walk|talk|agree|approve|finalise|"
    "finalize|rebuild|redo|tidy|route|block|audit|quantify|identify|surface|"
    "track|hold|make|report|plan|design|document|order|buy|pay|cancel|renew|"
    "return|collect|drop|bring|fit|install|replace|clean|empty|fill|sort|"
    "print|scan|upload|download|email|text|ring|arrange|swap|claim|apply|"
    "register|sign|post|deliver|measure|paint|hang|mount|wire|seal|clear|"
    "put|stop|start|settle|find|source|phone|sell|list|record|solve|capture|"
    "pick|fit|swap|wash|feed|book|price|quote|invoice|refund|dispose|donate|"
    "photograph|label|store|stock|top up|switch|migrate|restore|back up|"
    "rewire|reroute|patch|flash|reflash|calibrate|mount|seal|grout|caulk|"
    "sand|prime|trim|cut|drill|screw|glue|assemble|dismantle|repair|service|"
    "renew|insure|tax|nct|claim|lodge|transfer|reconcile|export|import|"
    "diagnose|prototype|wire up|hook up|connect|disconnect|reset|rename|"
    "delete|archive|split|merge|tag|sort out the|draft|circulate|forward|"
    "attach|upload|print out|laminate|frame|hang up|bin|recycle|return|"
    "lock down|organise|organize|grant|revoke|re-?share|re-?export|re-?jig|"
    "back-?fill|hand over|drop off|sign off|write up|set aside"
)

_CONTAINER_RE = re.compile(
    r"^(?:" + "|".join(re.escape(v) for v in CONTAINER_VERBS) + r")\b",
    re.IGNORECASE,
)
_ACTION_RE = re.compile(r"\b(?:" + ACTION_VERBS + r")\b", re.IGNORECASE)
# Where a *second* clause can begin. Splitting on these and asking which
# segments START with an imperative is the whole trick — see `flags_for`.
_COORDINATOR_RE = re.compile(
    r",\s*then\b|\band then\b|\bthen\b|\band\b|;\s|\s\+\s|\s&\s",
    re.IGNORECASE,
)
_STARTS_ACTION_RE = re.compile(r"^\W*(?:" + ACTION_VERBS + r")\b", re.IGNORECASE)

# Deliberately-batched work. The live backlog uses "— one job" and "— one
# trip" to mean "yes, this is several things, and I am doing them together on
# purpose". That is a decision already taken, so re-raising it is noise.
_BATCHED_RE = re.compile(r"\bone (?:job|trip|go|sitting|session|pass)\b", re.IGNORECASE)

# Markup a title may open with before the verb: a wikilink, a bold lead-in, a
# bare tag, a "Project:" prefix. Stripped so the container test still anchors.
_LEAD_RE = re.compile(
    r"^(?:\s|[-*]\s|\*\*|__|~~|#\w[\w/-]*\s|\[\[[^\]]*\]\]\s*[-–—:]?\s*|"
    r"[A-Z][\w &'/]{0,30}:\s)+",
)

# Where the first clause ends. Counting verbs across a whole line with three
# sentences of context flags long-but-single-action tasks.
_CLAUSE_END_RE = re.compile(r"(?:\.\s|\s[-–—]\s|\s\(|\bso that\b|\bbecause\b)")

FLAGS: dict[str, str] = {
    "not_a_next_action":
        "Names an area rather than a move you could start now.",
    "several_actions":
        "Holds more than one action, so it can never be ticked.",
    "no_action_named":
        "No verb — a subject, not something you can do.",
    "very_long":
        "Long enough that the actual action is probably buried.",
}


def _body(title: str) -> str:
    """The title with leading markup removed, for anchored matching."""
    return _LEAD_RE.sub("", title).strip()


def flags_for(title: str) -> list[str]:
    """Which rules a title trips. Empty is the healthy case."""
    body = _body(title)
    if not body:
        return []
    out: list[str] = []

    if _CONTAINER_RE.match(body):
        out.append("not_a_next_action")

    # Count clauses that BEGIN with an imperative, not verb-shaped tokens
    # anywhere.
    #
    # ⚠️ The obvious version — distinct verbs in the clause — does not work,
    # and measuring on the live ledger is what showed it. English nouns and
    # imperatives overlap heavily, so "Send the check-in **email**" scored
    # three verbs (send / check / email) and "Ask … for an itemised
    # **invoice**, not a €500 package **price**" scored three more. Every one
    # of those is a single action. A genuine second action starts a clause;
    # a noun sits inside one.
    head = _CLAUSE_END_RE.split(body, maxsplit=1)[0]
    if not _BATCHED_RE.search(body):
        clauses = _COORDINATOR_RE.split(head)
        if sum(1 for c in clauses if _STARTS_ACTION_RE.match(c)) >= 2:
            out.append("several_actions")

    # Only claim "no action" on a SHORT title with no verb anywhere.
    #
    # ⚠️ This rule detects absence against an allowlist, which is the weakest
    # thing a heuristic can do: English has thousands of imperatives and the
    # list has a few hundred, so on a long title a miss means "our list is
    # short", not "no action was named". Measured on the live 246 before the
    # length bound, it flagged 52 tasks at roughly one-in-six precision —
    # "Put the outdoor kit away", "Phone Brooks and Strahan", "Sell
    # armchairs" — all real actions with verbs we simply had not listed. A
    # bare noun phrase ("Contemporaneous notes habit") is genuinely short,
    # so the bound is what makes the rule mean anything at all.
    if not out and len(body.split()) <= 6 and not _ACTION_RE.search(body):
        out.append("no_action_named")

    if len(title) > 400:
        out.append("very_long")
    return out


def summarise(results: list[tuple[str, list[str]]]) -> dict:
    """Counts plus the framing.

    `results` is (title, flags) per task. The `caveat` is returned rather than
    left to the caller to remember: the count alone overstates the problem
    every time, because a long task that is genuinely one action trips the
    heuristic and is fine.
    """
    flagged = [(t, f) for t, f in results if f]
    by_flag: dict[str, int] = {}
    for _, fs in flagged:
        for f in fs:
            by_flag[f] = by_flag.get(f, 0) + 1
    return {
        "total": len(results),
        "flagged": len(flagged),
        "by_flag": dict(sorted(by_flag.items(), key=lambda kv: -kv[1])),
        "caveat": (
            "These are candidates, not defects. Expect a substantial share to "
            "be false positives — a long task that is genuinely one action "
            "trips the same rules. Read each before proposing a rewrite, and "
            "quote the ratio you actually found."
        ),
    }
