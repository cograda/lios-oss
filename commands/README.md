# Day-to-day slash commands (versioned source)

Hand-authored, Alex-specific Claude Code slash commands. **This directory is
the source of record** — edit these files directly, commit them normally.

They are not loaded from here. `scripts/render_commands.py` copies them into
`vault/.claude/commands/`, which is the day-to-day Claude Code project (see
`vault/CLAUDE.md`). That directory is gitignored, so it is a build artifact:

```bash
server/backend/.venv/bin/python scripts/render_commands.py
```

Run that after editing anything here, and after pulling changes.

## Two sources, one delivery directory

| Source | Files | Edit |
|---|---|---|
| `commands/*.md` (here) | 18 hand-authored, Alex-only (incl. `/youdoit` and `/tunetasks`, ported from the work `taskdb` tool 2026-09-02) | directly |
| `server/backend/app/prompts/templates/*.md.j2` | 6 curated + cross-user | the template, then re-render |

The six generated ones (`daily-note`, `add-task`, `find`, `triage`,
`lock-in`, `week-ahead`) carry a `<!-- GENERATED -->` header and are rendered
per-user by `app.prompts.commands` — Sam gets her own variants of exactly
those six from `GET /api/v1/commands`. Never hand-edit a delivered file with
that header; the next render overwrites it.

A filename must not appear in both sources. `render_commands.py` raises if it
does, and `tests/test_command_registry.py` catches it in CI.

## Why the split

The repo root is the **development** project (kernel, integrations, deploys).
The vault is the **day-to-day** project (daily notes, triage, meetings,
writing). All 24 commands here are day-to-day, so none belong in the dev
project's own `.claude/commands/` — a test asserts it stays empty.
`/sync-docs` is user-level (`~/.claude/commands/`) and so is available in both.

## Open

Folding these 18 into the server-side registry would give one delivery
mechanism instead of two, and would let the set self-update from the server
the way Sam's does. It needs per-user curation decisions first (most of
these are Alex-only), so it is deliberately not done yet.

## ⚠️ Commands that still hand-edit `Task Backlog.md` (found 2026-09-02)

Since the task ledger shipped (2026-08-29) that file is a rendered view, and a
hand edit locks every task write until a forced render. `daily-note`, `youdoit`
and `tunetasks` go through the `tasks_*` tools. These have not been ported yet
and will trip the drift guard if followed literally: `add-task`, `lock-in`
(templates); `backlog-sweep`, `plan-week`, `weekly-review`, `defrag`, `meeting`,
`quick`, `reconcile-reminders`, `seed-backlog` (static). Port them, or retire
the ones the app now covers, before relying on them.
