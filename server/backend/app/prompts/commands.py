"""Per-user slash-command registry (sam-rollout Phase B2).

Single generator for the CURATED cross-user command set — the small group
of `.claude/commands/*.md` files every user gets, rendered with per-user
substitution (display name, vault-relative paths, pronoun). This is the
one source of truth for those six; the delivered files are a REGENERATED
artifact of these templates, never hand-edited.

Where the rendered output lands differs per user:

  * Sam — `~/Comar/.claude/commands/`, written by the installer from
    `GET /api/v1/commands` (`app/routes/install.py`). She never has the repo.
  * Alex — `vault/.claude/commands/`, written by
    `scripts/render_commands.py`. Since the dev/day-to-day project split
    (2026-07-28) the vault is his day-to-day Claude Code project, which is
    the direct analogue of Sam's `~/Comar/`. Both targets are outside
    version control; the committed record is these templates plus the
    golden snapshots in `tests/snapshots/commands/`.

Alex-specific commands (finance, import-finance, listening, linkedin,
seed-backlog, meeting, ...) are out of scope for this registry — they are
hand-authored markdown under the repo's `commands/` directory, copied
through verbatim by `scripts/render_commands.py` alongside the six rendered
here. Folding them into this registry (so they self-update from the server
like Sam's set) is still open.

The old `app/prompts/*.yaml` files (gRPC-era MCP "prompt" definitions) and
the loader/endpoint that served them (`registry.get_all_prompts()`,
`GET /api/v1/prompts`) have all been deleted (2026-08-08) — nothing consumed
them post-Phase-4 (the daemon's prompt-mirroring code path was a confirmed
no-op, see `client/src/comar/daemon.py`). This module replaces that
subsystem for the six curated commands only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import re

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Order matters only for iteration convenience (dict below is the real index).
CURATED_COMMANDS: tuple[str, ...] = (
    "daily-note",
    "add-task",
    "find",
    "triage",
    "lock-in",
    "week-ahead",
)

_TOKEN_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# Block markers. Each must occupy a line of its own — see `_apply_conditionals`.
_IF_RE = re.compile(r"\{%\s*if\s+([^%]+?)\s*%\}")
_ENDIF_RE = re.compile(r"\{%\s*endif\s*%\}")
_STRAY_MARKER_RE = re.compile(r"\{%[^%]*%\}")

_GENERATED_HEADER = (
    "<!-- GENERATED from server/backend/app/prompts/templates/{slug}.md.j2 "
    "via app.prompts.commands — do not hand-edit. Edit the template and run "
    "scripts/render_commands.py (or refetch GET /api/v1/commands). -->\n\n"
)


@dataclass(frozen=True)
class UserCommandContext:
    """Per-user substitution values for the curated command templates."""

    display_name: str
    daily_notes_dir: str  # vault-relative, trailing slash, e.g. "Daily Notes/Alex/"
    triage_state_path: str  # vault-relative-from-repo-root, e.g. "vault/Alex/.triage-state.md"
    lock_in_user_suffix: str  # appended after the daily-note path in /lock-in step 1
    object_pronoun: str  # "him" / "her" / "them"


# Known per-user literals. Alex's values were chosen to faithfully reproduce
# the then-hand-written command content byte-for-byte
# (including the `vault/Alex/.triage-state.md` path, which predates the
# single-vault-per-user split and is left as-is here — fixing it is out of
# scope for B2). Sam's mirror the sam-rollout decision that her vault
# has no per-user subfolder.
_USER_CONTEXTS: dict[str, UserCommandContext] = {
    "alex": UserCommandContext(
        display_name="Alex",
        daily_notes_dir="Daily Notes/Alex/",
        triage_state_path="vault/Alex/.triage-state.md",
        lock_in_user_suffix=" (or Sam's if specified in arguments)",
        object_pronoun="him",
    ),
    "sam": UserCommandContext(
        display_name="Sam",
        daily_notes_dir="Daily Notes/",
        triage_state_path="vault/.triage-state.md",
        lock_in_user_suffix="",
        object_pronoun="her",
    ),
}

# Fallback for any future user not yet in _USER_CONTEXTS: behave like sam
# (single vault root, no subfolder) rather than crashing.
_DEFAULT_CONTEXT_TEMPLATE = _USER_CONTEXTS["sam"]


def context_for_user(user_name: str, display_name: str | None = None) -> UserCommandContext:
    """Resolve substitution values for a user by their canonical `name`."""
    ctx = _USER_CONTEXTS.get(user_name)
    if ctx is not None:
        return ctx
    dn = display_name or user_name.capitalize()
    return UserCommandContext(
        display_name=dn,
        daily_notes_dir=_DEFAULT_CONTEXT_TEMPLATE.daily_notes_dir,
        triage_state_path=_DEFAULT_CONTEXT_TEMPLATE.triage_state_path,
        lock_in_user_suffix=_DEFAULT_CONTEXT_TEMPLATE.lock_in_user_suffix,
        object_pronoun="them",
    )


def _eval_condition(expr: str, prefs: dict[str, Any] | None, slug: str) -> bool:
    """Evaluate one `{% if %}` condition against a user's resolved preferences.

    Two forms, and deliberately no more — this is a prompt template, not a
    programming language:

      * `section:<name>`  — the section is in `daily_note.sections`
      * `pref:<key>`      — the preference resolves to a truthy value

    Either may be negated with a leading `not `.

    `prefs is None` means "render everything", which is what keeps the golden
    snapshots and any caller that doesn't have a session stable. A block is
    only ever *removed* by an explicit preference.
    """
    negated = False
    if expr.startswith("not "):
        negated, expr = True, expr[4:].strip()

    if prefs is None:
        result = True
    elif expr.startswith("section:"):
        name = expr[len("section:"):].strip()
        sections = prefs.get("daily_note.sections") or []
        result = name in sections
    elif expr.startswith("pref:"):
        key = expr[len("pref:"):].strip()
        result = bool(prefs.get(key))
    else:
        raise ValueError(
            f"Unsupported condition {{% if {expr} %}} in {slug}.md.j2 — "
            "expected 'section:<name>' or 'pref:<key>', optionally negated"
        )

    return not result if negated else result


def _apply_conditionals(raw: str, prefs: dict[str, Any] | None, slug: str) -> str:
    """Strip `{% if COND %}…{% endif %}` blocks the user's prefs switch off.

    A stack-based line scan rather than a regex, because blocks nest (a
    per-section gate wrapping a finer per-preference one) and a non-greedy
    regex would bind an inner `{% endif %}` to the outer `{% if %}` — closing
    the wrong block and producing plausible, wrong output. Markers must sit
    alone on their own line; anything else raises rather than being half-
    interpreted.
    """
    out: list[str] = []
    stack: list[bool] = []          # per-open-block: is this branch emitting?

    for lineno, line in enumerate(raw.split("\n"), start=1):
        stripped = line.strip()

        if_match = _IF_RE.fullmatch(stripped)
        if if_match:
            parent_live = all(stack)
            # Only evaluate when an enclosing block is live, so a condition
            # inside a dropped block can't raise on an unrelated user.
            keep = parent_live and _eval_condition(
                if_match.group(1).strip(), prefs, slug
            )
            stack.append(keep)
            continue

        if _ENDIF_RE.fullmatch(stripped):
            if not stack:
                raise ValueError(
                    f"{slug}.md.j2 line {lineno}: {{% endif %}} with no open "
                    "{% if %}"
                )
            stack.pop()
            continue

        if _STRAY_MARKER_RE.search(line):
            raise ValueError(
                f"{slug}.md.j2 line {lineno}: conditional marker must be alone "
                f"on its own line, got {line!r}"
            )

        if all(stack):
            out.append(line)

    if stack:
        raise ValueError(f"{slug}.md.j2: {len(stack)} unclosed {{% if %}} block(s)")

    # Dropping a block leaves the blank lines that surrounded it; collapse
    # runs of 3+ newlines so the delivered markdown doesn't grow holes.
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def _render_template(
    slug: str,
    ctx: UserCommandContext,
    prefs: dict[str, Any] | None = None,
) -> str:
    raw = (_TEMPLATES_DIR / f"{slug}.md.j2").read_text(encoding="utf-8")
    raw = _apply_conditionals(raw, prefs, slug)
    values = {
        "display_name": ctx.display_name,
        "daily_notes_dir": ctx.daily_notes_dir,
        "triage_state_path": ctx.triage_state_path,
        "lock_in_user_suffix": ctx.lock_in_user_suffix,
        "object_pronoun": ctx.object_pronoun,
    }

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            raise KeyError(f"Unknown template token {{{{ {key} }}}} in {slug}.md.j2")
        return values[key]

    body = _TOKEN_RE.sub(_sub, raw)
    return _GENERATED_HEADER.format(slug=slug) + body


def render_command(
    slug: str,
    user_name: str,
    display_name: str | None = None,
    prefs: dict[str, Any] | None = None,
) -> str:
    """Render one curated command for one user.

    `prefs` is that user's resolved `app.services.preferences` mapping. Pass
    it and the delivered file physically loses the blocks they've switched
    off; omit it and every block renders. Suppressing at *render* time rather
    than only at runtime is the point: `_meta.sections_enabled` stops a
    section being written, but the model still reads and reasons over the
    instructions for it every morning.
    """
    if slug not in CURATED_COMMANDS:
        raise ValueError(f"{slug!r} is not a curated command: {CURATED_COMMANDS}")
    ctx = context_for_user(user_name, display_name)
    return _render_template(slug, ctx, prefs)


def render_command_set(
    user_name: str,
    display_name: str | None = None,
    prefs: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Render the full curated set for one user: {filename: content}."""
    return {
        f"{slug}.md": render_command(slug, user_name, display_name, prefs)
        for slug in CURATED_COMMANDS
    }


# ---------------------------------------------------------------------------
# Per-user CLAUDE.md ("vault rules")
# ---------------------------------------------------------------------------

_ALEX_CLAUDE_MD = """# Comar — Alex's workspace

Alex's repo checkout at `~/Desktop/Code/lios` is authoritative for the
curated command set and vault rules. See `CLAUDE.md` (repo root) and
`vault/CLAUDE.md` for the full operating manual — nothing here supersedes
those. This render exists only so `GET /api/v1/commands` returns a
consistent shape for every user; for Alex it is informational.
"""


def _sam_claude_md(ctx: UserCommandContext) -> str:
    return f"""# Comar — {ctx.display_name}'s workspace

This is your home for talking to comar. You are using Claude Code (the Mac
app). Your vault lives at `./vault/` — everything here is keyed to you;
there is no shared `Alex/`/`Sam/`/`Shared/` split.

## Vault structure

```
Daily Notes/       One note per day — your daily operating surface
Task Backlog.md    The single unified task backlog (source of truth)
Someday.md         Ideas / parked items
Notes/             General personal notes
Projects/          One note per active project/initiative
Inbox/             Landing zone for incoming files
```

## Daily notes

- Location: `{ctx.daily_notes_dir}YYYY-MM-DD.md`
- Frontmatter: `date: YYYY-MM-DD`, `type: daily`

## Tasks

- Every actionable task lives in the single `Task Backlog.md` at your vault
  root, grouped by domain (`#home`, `#renovation`, `#kids`, `#finance`,
  `#admin`). Ideas go in `Someday.md`.
- Task format (Obsidian Tasks plugin):
  `- [ ] Task description \U0001f53a #domain \U0001f4c5 2026-04-15`
- Daily notes never copy tasks in — they surface backlog items live via a
  query, plus a short manual Focus list (3-5 items, set via `/lock-in`).

## Household-shared data

Some things are shared across the household rather than split per person:
the family calendar, the snag register for the house, finance, and
weather. You will see Alex's calendar events and the shared snag register
through the normal tools — that is intentional, not a leak. Your own
Gmail, WhatsApp, health, and vault stay private to you.

## Slash commands you'll use most

- `/daily-note` — Open or refresh today's daily note
- `/add-task` — Add something to your backlog
- `/find` — Search your vault
- `/triage` — Quick routing for new emails / WhatsApp messages
- `/lock-in` — Pick today's focus and sync with Apple Reminders
- `/week-ahead` — What's coming up this week from the family calendar

## General behaviour

- Conversational, concise — this is a day-to-day environment, not a dev
  project.
- Always include frontmatter matching the schema for the note type.
- Use Irish date formatting (DD/MM/YYYY) in prose, YYYY-MM-DD in
  frontmatter and filenames. Currency is Euro (€).
- Never surface Alex's personal content (health, private notes) to you or
  vice versa — the per-user scoping is enforced server-side, but keep the
  same discipline in anything you write.
"""


def render_claude_md(user_name: str, display_name: str | None = None) -> str:
    """Render the per-user CLAUDE.md ("vault rules") for `GET /api/v1/commands`."""
    if user_name == "alex":
        return _ALEX_CLAUDE_MD
    ctx = context_for_user(user_name, display_name)
    return _sam_claude_md(ctx)
