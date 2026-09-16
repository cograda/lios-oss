"""Per-user slash-command registry (sam-rollout Phase B2; widened 2026-09-06).

Single generator for the day-to-day `.claude/commands/*.md` set, rendered
per user with substitution (display name, vault-relative paths, pronouns,
partner). `COMMAND_TABLE` below is the one place that says which user gets
which command; the delivered files are a REGENERATED artifact of the
templates in `templates/`, never hand-edited.

Where the rendered output lands differs per user:

  * Sam — `~/lios/.claude/commands/`, written by the installer from
    `GET /api/v1/commands` (`app/routes/install.py`). She never has the repo.
  * Alex — `vault/.claude/commands/`, written by
    `scripts/render_commands.py`. Since the dev/day-to-day project split
    (2026-07-28) the vault is his day-to-day Claude Code project, which is
    the direct analogue of Sam's `~/lios/`. Both targets are outside
    version control; the committed record is these templates plus the
    golden snapshots in `tests/snapshots/commands/`.

History: until 2026-09-06 only five commands (daily-note, add-task, find,
triage, week-ahead) were templates; twelve more were hand-authored markdown
under the repo's `commands/` directory, Alex-only, copied through verbatim
by `scripts/render_commands.py`. Alex curated them that day — eight are
shared and became templates (their pre-fold text is the golden snapshot, so
his render is provably unchanged apart from the tokens), four stay
Alex-only and stay static (`STATIC`), because nothing in them varies per
user and `deploy/release_manifest.py` excludes one of them by its
`commands/` path.

**2026-09-07**: `daily-note` was renamed `kickoff` (the note is one output
of a morning kickoff, not the point of it — see the vault plan `Projects/
lios/Plans/Daily Kickoff — Rebuilding Daily Note as a Morning Ritual.md`)
and `triage` was retired, folded into `kickoff`'s intake pass. Both old
slugs are in `RETIRED_COMMANDS` below, so a client that fetched them under
their old names gets them deleted rather than left stale on disk.

The old `app/prompts/*.yaml` files (gRPC-era MCP "prompt" definitions) and
the loader/endpoint that served them (`registry.get_all_prompts()`,
`GET /api/v1/prompts`) have all been deleted (2026-08-08) — nothing consumed
them post-Phase-4 (the daemon's prompt-mirroring code path was a confirmed
no-op, see `client/src/comar/daemon.py`). This module replaces that
subsystem for the day-to-day slash commands only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import re

from app.integrations.tasks.dupes import (
    DEFAULT_THRESHOLD as DUPES_DEFAULT_THRESHOLD,
    MEASURED_DUPLICATE_RANGE,
    RELATED_WORK_THRESHOLD as DUPES_RELATED_WORK_THRESHOLD,
)

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Render-time constants (lios#223) — a numeric threshold/business rule
# hand-typed into a `.md.j2` template drifts silently from the Python
# constant it describes. These trace `{{ dupes_* }}` tokens below back to
# `app.integrations.tasks.dupes` so the two can never disagree; see
# `tests/test_template_constants.py` for the mechanical guard.
_CONSTANT_VALUES: dict[str, str] = {
    "dupes_threshold": f"{DUPES_DEFAULT_THRESHOLD:.2f}",
    "dupes_related_threshold": f"{DUPES_RELATED_WORK_THRESHOLD:.2f}",
    "dupes_measured_low": f"{MEASURED_DUPLICATE_RANGE[0]:.2f}",
    "dupes_measured_high": f"{MEASURED_DUPLICATE_RANGE[1]:.2f}",
}

# ---------------------------------------------------------------------------
# The per-user command table — Alex's curation call of 2026-09-06.
#
# Change WHO GETS WHAT here and nowhere else. `users` is EVERYONE or a frozenset
# of canonical user names. `source` is TEMPLATE (rendered per user from
# `templates/<slug>.md.j2`) or STATIC (hand-authored `commands/<slug>.md` at the
# element root, copied verbatim by `scripts/render_commands.py`; never served
# by the endpoint, so a STATIC row must also be user-restricted).
# ---------------------------------------------------------------------------

EVERYONE: frozenset[str] = frozenset()   # sentinel: no restriction
ALEX_ONLY: frozenset[str] = frozenset({"alex"})

TEMPLATE = "template"
STATIC = "static"


@dataclass(frozen=True)
class CommandSpec:
    users: frozenset[str]   # EVERYONE, or the users who get it
    source: str = TEMPLATE  # TEMPLATE | STATIC

    def available_to(self, user_name: str) -> bool:
        return not self.users or user_name in self.users


COMMAND_TABLE: dict[str, CommandSpec] = {
    # slug               who         source
    "kickoff":        CommandSpec(EVERYONE),
    "add-task":       CommandSpec(EVERYONE),
    "find":           CommandSpec(EVERYONE),
    "week-ahead":     CommandSpec(EVERYONE),
    # Folded in from commands/ on 2026-09-06 — the eight shared ones.
    "tunetasks":      CommandSpec(EVERYONE),
    "meeting":        CommandSpec(EVERYONE),
    "note":           CommandSpec(EVERYONE),
    "plan-week":      CommandSpec(EVERYONE),
    "weekly-review":  CommandSpec(EVERYONE),
    "checkin":        CommandSpec(EVERYONE),
    "harvest":        CommandSpec(EVERYONE),
    "youdoit":        CommandSpec(EVERYONE),
    # Alex-only, and still hand-authored files under commands/.
    "finance":        CommandSpec(ALEX_ONLY, STATIC),
    "import-finance": CommandSpec(ALEX_ONLY, STATIC),
    "linkedin":       CommandSpec(ALEX_ONLY, STATIC),
    "listening":      CommandSpec(ALEX_ONLY, STATIC),
}

for _slug, _spec in COMMAND_TABLE.items():
    if _spec.source == STATIC and _spec.users is EVERYONE:
        raise RuntimeError(
            f"COMMAND_TABLE[{_slug!r}] is STATIC and EVERYONE — a static file is "
            "never served by GET /api/v1/commands, so it cannot be a shared command. "
            "Make it a template."
        )
del _slug, _spec

# Command files a client may have previously fetched and written under a
# now-retired name. `GET /api/v1/commands` reports these alongside the live
# set so a caller can delete its own stale copy — see `core/client`'s
# `server_client.py::get_commands` and wherever it writes fetched commands
# into `.claude/commands/`. Never anything but a slug that once lived in
# `COMMAND_TABLE` and no longer does; this is cleanup, not a second registry.
RETIRED_COMMANDS: tuple[str, ...] = ("daily-note.md", "triage.md")

# Every slug rendered from a template, in table order. This is what the
# endpoint and the snapshot test iterate; `CURATED_COMMANDS` is the historical
# name and stays as an alias for existing callers.
TEMPLATE_COMMANDS: tuple[str, ...] = tuple(
    slug for slug, spec in COMMAND_TABLE.items() if spec.source == TEMPLATE
)
CURATED_COMMANDS = TEMPLATE_COMMANDS

# The hand-authored ones `scripts/render_commands.py` copies verbatim.
STATIC_COMMANDS: tuple[str, ...] = tuple(
    slug for slug, spec in COMMAND_TABLE.items() if spec.source == STATIC
)


def commands_for_user(user_name: str) -> tuple[str, ...]:
    """Template slugs this user receives from `GET /api/v1/commands`, in table order."""
    return tuple(
        slug for slug, spec in COMMAND_TABLE.items()
        if spec.source == TEMPLATE and spec.available_to(user_name)
    )


def static_commands_for_user(user_name: str) -> tuple[str, ...]:
    """Static slugs this user receives — delivered by `scripts/render_commands.py` only."""
    return tuple(
        slug for slug, spec in COMMAND_TABLE.items()
        if spec.source == STATIC and spec.available_to(user_name)
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
    object_pronoun: str  # "him" / "her" / "them"
    subject_pronoun: str = "they"  # "he" / "she" / "they"
    # The standalone form ("the call is his/hers/theirs"), which is the only
    # one the templates use — as a determiner ("his call") it would need a
    # separate token, because "her" and "hers" differ.
    possessive_pronoun: str = "theirs"  # "his" / "hers" / "theirs"
    # The other adult in the household, for lines like "1:1 with Sam".
    partner_display_name: str = "Alex"


# Known per-user literals. Alex's values were chosen to faithfully reproduce
# the then-hand-written command content byte-for-byte. Sam's mirror the
# sam-rollout decision that her vault has no per-user subfolder.
# (`triage_state_path` was dropped 2026-09-07 when `/triage` was retired.)
_USER_CONTEXTS: dict[str, UserCommandContext] = {
    "alex": UserCommandContext(
        display_name="Alex",
        daily_notes_dir="Daily Notes/Alex/",
        object_pronoun="him",
        subject_pronoun="he",
        possessive_pronoun="his",
        partner_display_name="Sam",
    ),
    "sam": UserCommandContext(
        display_name="Sam",
        daily_notes_dir="Daily Notes/",
        object_pronoun="her",
        subject_pronoun="she",
        possessive_pronoun="hers",
        partner_display_name="Alex",
    ),
}

# Fallback for any future user not yet in _USER_CONTEXTS: behave like sam
# (single vault root, no subfolder) rather than crashing. Pronouns fall back
# to singular they — a template written around "he means" will read "they
# means" for such a user; the only real users are the two above, so that is
# noted rather than engineered around.
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
        "object_pronoun": ctx.object_pronoun,
        "subject_pronoun": ctx.subject_pronoun,
        "possessive_pronoun": ctx.possessive_pronoun,
        "partner_display_name": ctx.partner_display_name,
        **_CONSTANT_VALUES,
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
    spec = COMMAND_TABLE.get(slug)
    if spec is None or spec.source != TEMPLATE:
        raise ValueError(f"{slug!r} is not a template command: {TEMPLATE_COMMANDS}")
    if not spec.available_to(user_name):
        raise ValueError(f"{slug!r} is not available to {user_name!r} (COMMAND_TABLE)")
    ctx = context_for_user(user_name, display_name)
    return _render_template(slug, ctx, prefs)


def render_command_set(
    user_name: str,
    display_name: str | None = None,
    prefs: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Render this user's full template set: {filename: content}.

    Per user, per `COMMAND_TABLE` — a Alex-only row never appears in Sam's
    set. Static commands are not included (they are files, not renders;
    `scripts/render_commands.py` copies them for the users the table names).
    """
    return {
        f"{slug}.md": render_command(slug, user_name, display_name, prefs)
        for slug in commands_for_user(user_name)
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
Task Backlog.md    The single unified task backlog (source of truth; ideas
                   render under Someday, items owed by others under Waiting)
Notes/             General personal notes
Projects/          One note per active project/initiative
Inbox/             Landing zone for incoming files
```

## Daily notes

- Location: `{ctx.daily_notes_dir}YYYY-MM-DD.md`
- Frontmatter: `date: YYYY-MM-DD`, `type: daily`

## Tasks

- Every actionable task lives in the household **task ledger** — a database
  the `tasks_*` tools read and write (`tasks_query`, `tasks_add`,
  `tasks_update`, `tasks_complete`). `Task Backlog.md` at your vault root is a
  rendered view of it: read it in Obsidian, never edit it — the next write
  re-renders it. Ideas are `status="someday"`; something you are waiting on
  someone else for is `status="waiting"`.
- Daily notes never copy tasks in — they surface ledger items live via a
  query, plus a short Focus list (3-5 items) that `/tunetasks` sets at the end
  of its run and mirrors into the ledger's focus queue.

## Household-shared data

Some things are shared across the household rather than split per person:
the family calendar, the snag register for the house, finance, and
weather. You will see Alex's calendar events and the shared snag register
through the normal tools — that is intentional, not a leak. Your own
Gmail, WhatsApp, health, and vault stay private to you.

**Alex's household notes are readable from here too (since 6 September 2026).**
The renovation file — builder correspondence, snag lists, the Comar House
notes — and the household reference live in Alex's vault, and rather than
keep a second copy you have a read-only grant to two folders of it:
`Household/` and `People/`. Pass `as_user="alex"` to any vault read tool
(`vault_search`, `vault_recent`, `vault_similar`, `vault_stats`) to search
there; leave it off for your own vault. Nothing outside those two folders is
in scope — his daily notes, health and personal notes are refused, not
merely hidden — and the grant is read-only: you cannot change his notes.
Copies of about fifty of his notes were seeded into your vault on the same
day, each with a `source_vault` line saying his copy is the original.

## Slash commands you'll use most

Every day:

- `/kickoff` — Create today's daily note and kick off the day (three quick questions, then intake candidates to confirm)
- `/checkin` — Re-fetch live data into today's note mid-day (`/checkin email`, `/checkin calendar`, …) — was `/refresh` until 2026-09-10
- `/add-task` — Add something to your backlog
- `/find` — Search your vault
- `/note` — Create a new vault note with the right frontmatter and folder

Weekly rhythm:

- `/week-ahead` — What's coming up this week from the family calendar
- `/weekly-review` — Build the week's review digest, section by section
- `/plan-week` — Shape the coming week: commit tasks to the week queue, write the day stubs

Keeping the backlog honest:

- `/tunetasks` — Audit the ledger: wording, duplicates, what blocks what, placement
- `/youdoit` — Find the tasks an agent session could take most of the way, and write the prompt for each
- `/harvest` — Sweep recent email and WhatsApp for actions that never made it into the ledger
- `/meeting` — Turn a transcript or conversation into a meeting note, with action items filed to the ledger

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
