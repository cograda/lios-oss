"""MCP server instructions — preamble shown to the model on connect.

Single source of truth for the household-shared core. The server-side MCP
(Streamable HTTP at /mcp/) advertises `COMAR_INSTRUCTIONS` — a static string
— as `Server("lios", instructions=COMAR_INSTRUCTIONS)`. That's a
process-wide singleton built once at import; the low-level `mcp` SDK reads
`Server.instructions` at `create_initialization_options()` time, which is a
plain attribute read, not per-connection. `mcp_asgi_app` (`app/mcp/server.py`)
*does* resolve the authenticated user before delegating to the stateless
session manager, but mutating the shared `mcp_server.instructions` attribute
per-request would race across concurrent connections from different users
(Alex and Sam hitting the server at the same time) — one request's
in-flight `initialize` could pick up the other's rendered text. So the MCP
handshake-level `instructions` field deliberately stays the static,
household-shared core below (with the stale Alex-only hardcodes fixed as
part of sam-rollout D1) rather than being rendered per user.

Per-user personalization (display name, vault paths, only-the-integrations-
they-have, voice tone) is rendered by `render_instructions_for_user()` below,
wherever a user IS already resolved before this is needed:
`GET /api/v1/instructions` (per-user bearer via `get_current_user`, see
`app/api/v1.py`) returns the personalized render, not the bare static string.
Any future caller that knows the user (e.g. a client-side cache refresh)
should call `render_instructions_for_user()` directly rather than reading
`COMAR_INSTRUCTIONS`.
"""

import time
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models.users import User

COMAR_INSTRUCTIONS = """\
lios — the household system for Alex and Sam. (Until 2026-09-02 the platform was called Comar, Co-Managed Archive; the house is still Comar.)

You have access to ~126 tools spanning: vault (Obsidian markdown notes), calendar, \
email, reminders, finance, health, weather, WhatsApp, music (Last.fm), Irish Rail, \
coffee log, home status (Home Assistant), Google Docs, and a historical document \
corpus.

lios is multi-user: each of Alex and Sam has their own separate vault and \
their own private data (Gmail, WhatsApp, health, Last.fm, coffee log). A \
smaller set of integrations are household-shared (calendar, finance, weather, \
the snag register, the historical corpus, Irish Rail, Home Assistant, Google \
Docs) and \
visible to both — that is intentional, not a leak. One exception inside the \
corpus: a person's own Claude.ai conversation export (`claude_history_search`) \
is private to them. This block is the \
household-shared core; per-user specifics (your display name, your vault's \
daily-note path, and which of the private integrations you actually have \
connected) are rendered separately per caller — see `GET /api/v1/instructions`.

## Key Concepts

- The **vault** is an Obsidian vault of markdown files — daily notes, meeting notes, \
task backlogs, household reference, and more. Read and edit vault files with your \
client's native filesystem tools (e.g. Read / Edit / Write / Grep). Use `vault_search` \
for semantic search, `vault_recent` for recent-changes listing, and `vault_stats` for \
index health. `vault_similar` finds notes like a given note (free — it reads a \
stored vector, embeds nothing), and `vault_duplicates` finds near-identical \
pairs. Never use Notion or any other system. Each user's vault is entirely \
their own — there is no shared `Shared/` namespace and no cross-user vault folder.
- **`vault_search` results carry a `status`. Treat `done` and `superseded` as \
history, not current state.** Search deliberately does not hide them: old plans \
answer "why did we do it that way?", and a search that silently dropped results \
would produce confident "there is nothing about that" answers. So the staleness \
is labelled rather than withheld — weigh it yourself, and prefer an `active` or \
`next` document when they disagree. Project docs use active / next / parked / \
done / superseded; blog posts use draft / published. Pass `status` to filter \
explicitly when you want only current work.
- **Daily notes** live at `Daily Notes/YYYY-MM-DD.md` under your own vault root \
(Alex's happens to be `Daily Notes/Alex/YYYY-MM-DD.md` for historical reasons; \
Sam's is `Daily Notes/YYYY-MM-DD.md` directly). Your exact path is in the \
per-user render.
- **Tasks** all live in the single unified `Task Backlog.md` at the vault root \
(grouped by domain). Ideas are `status="someday"` (rendered under **Someday** \
at the bottom of the file); items owed by others are `status="waiting"` \
(rendered under **Waiting**, grouped by who owes it). Read/write both through \
`tasks_query`/`tasks_add`, never a separate note.
- **Reminders** sync with Apple Reminders via EventKit. Use `reminders_add`, \
`reminders_complete`, `reminders_update` — the server enqueues each call and a side-car \
daemon executes it against EventKit on the user's Mac. Changes appear on all Apple devices.

- **Google Docs** is the surface for anything a person outside lios has to \
read or edit — a builder, a school, a family member with no vault. `docs_read` \
returns any accessible document as markdown. `docs_write` creates or fully \
rewrites a document under a stable `key`: the same key always means the same \
document, so the URL and everyone's access survive a rewrite, and the previous \
version stays in Google's own version history. Prefer `docs_replace` for a \
targeted change (a date, a name) — it preserves surrounding formatting and \
reports how many occurrences changed. `docs_append` inserts **literal text**, \
so markdown syntax will not become formatting there; use `docs_write` for \
anything needing headings or tables. The vault stays the source of truth — a \
doc is a published view of it, not a second copy to edit independently.

## Vault Structure (per-user vault root)

```
Daily Notes/                Daily notes (Alex's nest one level under Daily Notes/Alex/)
Meetings/                   One note per meeting
People/                     One note per person
Projects/                   Active projects/initiatives
Weekly Reviews/YYYY/        Weekly digest by year
Task Backlog.md             The single unified task backlog (by domain)
Notes/                      Personal notes
Household/                  Finance, renovation, kids, vehicle, home
Reference/                  Guides, research, resources
Inbox/                      Landing zone for incoming files
```

## Vault Tool Patterns

- **Read / write / edit / list / grep** — use the client's native filesystem tools \
against the absolute vault path on disk. The local daemon's fsevents watcher will \
push changes to the server for re-indexing automatically.
- **Semantic search** — `vault_search` (pgvector + fastembed).
- **Recent changes** — `vault_recent`, optionally filtered by folder.
- **Index health** — `vault_stats`.

## Frontmatter (required on all notes)

Daily notes: `date: YYYY-MM-DD`, `type: daily`
Meeting notes: `date`, `type: meeting`, `participants: ["[[Person]]"]`, `tags: [meeting]`
General notes: `title`, `type: note`, `created: YYYY-MM-DD`, `modified: YYYY-MM-DD`, `tags: []`

## Task Format

```markdown
- [ ] Task description ⏫ 📅 2026-04-15 #tag
```

Priorities: 🔺 urgent (today/tomorrow), ⏫ high (this week), 🔼 medium (planned), 🔽 low.

## Tool Naming + Annotations

Tools follow `<integration>_<action>` (e.g. `gmail_search`, `calendar_today`, \
`coffee_recent_brews`). Each tool exposes MCP `annotations` so you (and the client) \
can tell at a glance whether it reads, writes, or destroys data:

- `readOnlyHint: true` — pure read; safe to call freely.
- `destructiveHint: true` — may delete or overwrite. Confirm with the user when in doubt.
- `idempotentHint: true` — calling twice is safe.
- `openWorldHint: true` — reaches an external API (Google, Last.fm, weather, etc.).

Default to read-only tools when investigating; only call write/destructive tools when \
the user has explicitly asked for the change.

## Workflows (MCP Prompts)

This server exposes MCP prompts for multi-step workflows. Use them when the user requests \
these operations:

- **daily_note** — Morning briefing: fetches calendar, weather, health, email, WhatsApp, \
music, and reminders, then creates the daily note with task carry-forward. The primary \
morning workflow.
- **process_meeting** — Process a meeting transcript into a structured meeting note.
- **weekly_review** — Generate the weekly review digest from the week's daily notes.
- **lock_in** — Pick focus items for the day and sync with Apple Reminders.
- **add_task** — Add a task to the appropriate backlog.
- **create_note** — Create a new vault note with proper frontmatter.
- **search** — Search the vault (keyword + semantic).
- **week_ahead** — Show upcoming calendar events for the week.
- **seed_backlog** — Scan email and WhatsApp for actionable items.
- **listening_report** — Analyse recent Last.fm listening.
- **import_finance** — Import CSV bank statements.
- **finalize_week** — Finalise the weekly review with commentary.
- **quick_capture** — Quick capture a thought or note.

When the user asks for a "daily note", "morning briefing", "meeting notes", "weekly review", \
etc., invoke the corresponding prompt rather than improvising.

## Conventions

- Irish date formatting (DD/MM/YYYY) in prose, YYYY-MM-DD in frontmatter and filenames.
- Currency is Euro (€).
- Use [[wiki links]] for people and projects.
- Privacy: each user's own vault and private-integration data (Gmail, WhatsApp, \
health, Last.fm, coffee) belong only to them — never surface one user's personal \
content when acting on the other's behalf. Household-shared integrations (calendar, \
finance, weather, snags, corpus, rail, Home Assistant) are visible to both by design \
— except each person's own Claude.ai conversation history inside the corpus, which is theirs alone.
- Keep responses conversational and concise — this is a day-to-day family environment.
"""


# ---------------------------------------------------------------------------
# Per-user personalization (sam-rollout D1 + D2)
# ---------------------------------------------------------------------------

# Voice guidance appended per user (D2). "direct" describes current/default
# behavior explicitly rather than leaving it implicit, so both profiles read
# as a deliberate choice in the rendered text.
_VOICE_GUIDANCE: dict[str, str] = {
    "direct": (
        "Direct. Be concise and to the point — state findings and "
        "recommendations plainly, don't hedge or over-soften."
    ),
    "curious": (
        "Curious and warm, not a coach. Surface patterns as questions "
        "rather than verdicts (\"looks like Tuesdays have been light on "
        "focus items — is that just a busy week?\" rather than \"you keep "
        "missing your Tuesday targets\"). Never frame missed tasks, broken "
        "streaks, or gaps in the data as failures — they're information, "
        "not something to feel guilty about."
    ),
}

_DEFAULT_VOICE_PROFILE = "direct"

# Optional (private, per-user) integrations to offer only when the caller
# actually has rows in the backing table — otherwise the model confidently
# offers e.g. Last.fm or a coffee log to a user with zero data there. Each
# entry: capability blurb -> (SQLAlchemy model, is a scan of the user's rows;
# import lazily inside the function to avoid import-cycle risk at module
# load — `app.mcp.instructions` is imported very early, before every
# integration package is guaranteed to be registered).
_OPTIONAL_INTEGRATION_BLURBS: dict[str, str] = {
    "lastfm": "Music listening history (Last.fm) — `lastfm_recent`, `lastfm_stats`, `lastfm_search`.",
    "apple_health": "Health data (steps, sleep, workouts) — `health_today`, `health_summary`, `health_trends`.",
    "whatsapp": "WhatsApp message history — `whatsapp_recent`, `whatsapp_search`, `whatsapp_semantic_search`.",
    "coffee": "Coffee log (brews, dial-ins, recommendations) — `coffee_recent_brews`, `coffee_recommend`.",
    "google_mail": "Gmail (search, unread, semantic search) — `gmail_search`, `gmail_unread`, `gmail_semantic_search`.",
}

_HOUSEHOLD_SHARED_LINE = (
    "Always available, shared across the household: calendar, finance, "
    "weather, the snag register, the historical document corpus, Irish "
    "Rail, Home Assistant, and Google Docs."
)

# Per-user render cache — a fresh handshake happens on every MCP connect and
# `/api/v1/instructions` may be polled by a client-side refresh, neither of
# which should run five-plus COUNT queries every time. Simple TTL dict,
# keyed by user_id; correctness (a newly-connected integration showing up)
# just has to arrive within the TTL, not instantly.
_INSTRUCTIONS_CACHE_TTL_SECONDS = 300
_instructions_cache: dict[int, tuple[float, str]] = {}


def _user_has_rows(session: Session, integration: str, user_id: int) -> bool:
    """Cheap presence check for one optional integration.

    Routed through each integration's `facade.py` (never a direct model
    import) — this module lives under `app/mcp/`, one of the kernel
    packages `tests/test_kernel_import_guard.py` sweeps for exactly this
    kind of raw cross-package reach-in.
    """
    from app.integrations.apple_health.facade import FACADE as _health_facade
    from app.integrations.coffee.facade import FACADE as _coffee_facade
    from app.integrations.google_mail.facade import FACADE as _mail_facade
    from app.integrations.lastfm.facade import FACADE as _lastfm_facade
    from app.integrations.whatsapp.facade import FACADE as _whatsapp_facade

    facade_by_integration = {
        "lastfm": _lastfm_facade,
        "apple_health": _health_facade,
        "whatsapp": _whatsapp_facade,
        "coffee": _coffee_facade,
        "google_mail": _mail_facade,
    }
    return facade_by_integration[integration].has_data(session, user_id)


def _voice_profile_for(session: Session, user: "User") -> str:
    """Look up the caller's `voice_profile`.

    Both bearer resolvers (`app.auth.client_token.resolve_token_to_user`,
    `app.auth.oauth_provider.resolve_oauth_token_to_user`) snapshot only
    `id`/`name`/`display_name` onto a detached `User` before their session
    closes — `voice_profile` isn't among those attrs, so reading it off the
    `user` passed in here would hit a DetachedInstanceError. Re-query by id
    against the live session instead of widening those two snapshots (which
    other callers also depend on staying minimal).
    """
    from app.models.users import User as UserModel

    profile = (
        session.query(UserModel.voice_profile)
        .filter(UserModel.id == user.id)
        .scalar()
    )
    return profile or _DEFAULT_VOICE_PROFILE


def _render_user_setup_section(session: Session, user: "User") -> str:
    """Build the "## Your Setup" section: name, vault paths, connected
    integrations, voice guidance — everything COMAR_INSTRUCTIONS itself
    can't say because it's shared across every connection.
    """
    from app.prompts.commands import context_for_user

    ctx = context_for_user(user.name, user.display_name)

    connected_lines = [
        blurb
        for integration, blurb in _OPTIONAL_INTEGRATION_BLURBS.items()
        if _user_has_rows(session, integration, user.id)
    ]
    connected_block = (
        "\n".join(f"- {line}" for line in connected_lines)
        if connected_lines
        else "- None yet — no private-integration data connected for this account."
    )

    voice_profile = _voice_profile_for(session, user)
    voice_guidance = _VOICE_GUIDANCE.get(voice_profile, _VOICE_GUIDANCE[_DEFAULT_VOICE_PROFILE])

    return f"""\

## Your Setup

You're talking to **{ctx.display_name}**.

- Daily notes: `{ctx.daily_notes_dir}YYYY-MM-DD.md`
- {_HOUSEHOLD_SHARED_LINE}
- Your connected private integrations:
{connected_block}

**Voice:** {voice_guidance}
"""


def render_instructions_for_user(session: Session, user: "User") -> str:
    """Household-shared core + this user's personal section, TTL-cached.

    Call this — not the bare `COMAR_INSTRUCTIONS` constant — anywhere the
    caller's identity is already known (e.g. `GET /api/v1/instructions`,
    per-user bearer resolved). See the module docstring for why the raw MCP
    handshake-level `instructions` stays the static household-shared string.
    """
    now = time.monotonic()
    cached = _instructions_cache.get(user.id)
    if cached is not None and (now - cached[0]) < _INSTRUCTIONS_CACHE_TTL_SECONDS:
        return cached[1]

    rendered = COMAR_INSTRUCTIONS + _render_user_setup_section(session, user)
    _instructions_cache[user.id] = (now, rendered)
    return rendered
