# Day-to-day slash commands (versioned source)

Hand-authored, **Alex-only** Claude Code slash commands — the four that vary
for nobody (`finance`, `import-finance`, `linkedin`, `listening`). **This
directory is the source of record for those four** — edit these files
directly, commit them normally. Everything shared with Sam is a template
under `server/backend/app/prompts/templates/` instead (see below).

They are not loaded from here. `scripts/render_commands.py` copies them into
`vault/.claude/commands/`, which is the day-to-day Claude Code project (see
`vault/CLAUDE.md`), alongside Alex's render of every template. That directory
is gitignored, so it is a build artifact:

```bash
server/backend/.venv/bin/python scripts/render_commands.py
```

Run that after editing anything here or in the templates, and after pulling
changes.

## Two sources, one delivery directory, one table

**`COMMAND_TABLE` in `server/backend/app/prompts/commands.py` is the one place
that says which user gets which command, and from which source.** Change
curation there; the tests pin the table so a change is a visible decision.

| Source | Files | Who | Edit |
|---|---|---|---|
| `server/backend/app/prompts/templates/*.md.j2` | 13: `daily-note`, `add-task`, `find`, `triage`, `week-ahead`, `tunetasks`, `meeting`, `note`, `plan-week`, `weekly-review`, `checkin`, `harvest`, `youdoit` | both users, rendered per user | the template, then re-render |
| `commands/*.md` (here) | 4: `finance`, `import-finance`, `linkedin`, `listening` | Alex only | directly |

Rendered commands carry a `<!-- GENERATED -->` header and are rendered per
user by `app.prompts.commands` — Sam gets her own variants of all thirteen
from `GET /api/v1/commands` (her installer writes them to
`~/lios/.claude/commands/`). Never hand-edit a delivered file with that header;
the next render overwrites it.

A filename must not appear in both sources, and the files here must be
exactly the table's `STATIC` rows. `render_commands.py` raises on either, and
`tests/test_command_registry.py` catches both in CI.

## Why the split

The repo root is the **development** project (kernel, integrations, deploys).
The vault is the **day-to-day** project (daily notes, triage, meetings,
writing). All 17 commands are day-to-day, so none belong in the dev
project's own `.claude/commands/` — a test asserts it stays empty.
`/sync-docs` is user-level (`~/.claude/commands/`) and so is available in both.

## The fold (2026-09-06) — was "Open"

Until 2026-09-06 twelve hand-authored commands lived here, all Alex-only,
because folding them into the registry needed per-user curation first.
Alex made that call on 2026-09-06 — Sam was missing `/tunetasks` and the
rest — and eight moved into templates: `tunetasks`, `meeting`, `note`,
`plan-week`, `weekly-review`, `checkin`, `harvest`, `youdoit`. The four
above stay here and Alex-only: nothing in them refers to "the current user",
so a template would have nothing to substitute, and
`deploy/release_manifest.py` excludes `commands/linkedin.md` from the public
release by that path — moving it would have silently un-excluded it.

The fold replaced only literals that meant *the current user* (`Daily
Notes/Alex/`, "Alex marks tasks in the loops app", "reflection (Alex)",
"Alex's voice", "1:1 with Sam") with tokens (`{{display_name}}`,
`{{daily_notes_dir}}`, `{{subject_pronoun}}` / `{{object_pronoun}}` /
`{{possessive_pronoun}}`, `{{partner_display_name}}`). Literals that mean Alex
or Sam the person — the owner examples in `/meeting`, "Alex and Sam read
together on Sunday morning" — stayed. Two lines were reworded for both users
because they only made sense while one person ran everything: `/checkin` lost
its `sam` argument (each user refreshes their own note), and `/meeting`'s
"for Alex (or Sam, if they're running this)" names the caller's own note
path. The pre-fold files became the golden snapshots
(`server/backend/tests/snapshots/commands/`), so Alex's render is provably
the old text plus the header.

## The ledger port (2026-09-03)

Since the task ledger shipped (2026-08-29) `Task Backlog.md` is a rendered view.
(At the time this section was written it also locked every task write on a hand
edit until a forced render — that guard was removed 2026-09-14 after it caused
an outage; the render is unconditional now. See `core/server/CLAUDE.md`'s tasks
section.) Found 2026-09-02: twelve commands still read or edited the file.
Resolved 2026-09-03, each one decided port-or-retire:

**Retired** — `backlog-sweep`, `defrag`, `stale`. All three were "read every
backlog file into Haiku, cluster, propose merges and moves, then hand-edit the
files". `/tunetasks` does the four passes against the ledger (conformance,
duplicates and clusters via stored vectors, blocking, placement), the tasks
app shows Problems and Duplicates live, and `tasks_merge` / `tasks_split` /
`tasks_bulk_update` are the writes. Nothing they did is lost; the file-reading
half was the part that had become wrong.

**Also retired, same day, on Alex's second look** — `quick` (the app's quick lens
plus one tap does it) and `finalize-week` (its commentary step is now
`/weekly-review` step 8; its planning half was already `/plan-week`).
`seed-backlog` was renamed **`harvest`** — it is a periodic sweep of email and
WhatsApp for missed actions, not a one-time seed. **`lock-in` is retired too**,
from the curated set: `/daily-note` locks in inline as its last step. It was
one of the six templates Sam's installer delivers; Alex's ruling was that
her set is not a reason to keep it — she can be reinstalled from scratch — so
the curated set is five.

**Ported** — `add-task` (template),
`harvest`, `meeting`, `plan-week`, `weekly-review`. Every
read is a `tasks_query`, every write a `tasks_*` call; each carries the same
ledger callout at the top. Two shapes changed on the way: `lock-in` sets the
`focus` queue instead of a `#focus` tag, and `plan-week` commits the week to the
`week` queue and due dates instead of copying task lines into daily-note stubs
(the stub carries the live query block instead).

⚠️ **`reconcile-reminders` is RETIRED (2026-09-04), not ported.** It was a
stopgap for exactly one disease — Apple Reminders and the task ledger were two
stores kept in line by hand — and said so at the top from the day it was
written. It retired the moment that stopped being true: the `apple_reminders`
integration now treats Reminders as an **inlet only** on its own periodic tick
— a new reminder becomes a ledger task, a task done in the ledger completes
its reminder, and a reminder completed on the phone completes its linked
task. See `server/backend/app/integrations/apple_reminders/README.md` for the
contract, including the deletion rule (a reminder deleted on the device does
not drop the task; a task dropped in the ledger completes, not deletes, its
reminder).

✅ **`Someday.md` and `Delegated Tasks.md` are folded into the ledger** (E6a,
2026-09-04): `status="someday"` and `status="waiting"` respectively, the
`## [[Person]]` heading on a delegated item captured as a `waiting_on` link,
imported once via `scripts/import_someday_delegated.py`. `Task Backlog.md`
renders both back out as dedicated **Someday** and **Waiting** sections. The
two files themselves are left on disk as a historical record — nothing writes
to them any more, and neither should be read for current state.
