<!-- GENERATED from server/backend/app/prompts/templates/plan-week.md.j2 via app.prompts.commands — do not hand-edit. Edit the template and run scripts/render_commands.py (or refetch GET /api/v1/commands). -->

Scaffold the coming week — distribute tasks, create daily note stubs, set reminders.

Arguments: $ARGUMENTS

Run this after `/weekly-review` (or standalone any time you want to set up the week ahead).

## Steps

### 1. Determine planning window

If today is Sunday, plan Monday to Friday of the coming week.
If today is Monday-Saturday, plan from tomorrow through the end of the week (next Sunday).

If arguments specify a week (e.g. "next week", "week of 14 April"), use that.

### 2. Gather context

**Read the most recent weekly review** (if one exists for this week or last week):
- `vault/Weekly Reviews/YYYY/` — find the latest file. Extract priorities, carry-forward items, and Week Ahead notes.

**Fetch calendar data:**
- `calendar_list_events` with `days` covering the planning window

**Fetch current state:**
- `reminders_list` — all current reminders
- The ledger, not the file:

> **The backlog is a ledger, not a file.** Never read `Task Backlog.md` for state and never edit it — it is a rendered view and a hand edit locks every task write. Read with `tasks_query`, write with `tasks_add` / `tasks_update` / `tasks_complete` / `tasks_bulk_update`; every write re-renders the file.

  - `tasks_query(queue="week", limit=200)` — what is already committed to this week
  - `tasks_query(priority="highest")`, `tasks_query(priority="high")` — candidates
  - `tasks_query(due_before=<end of window>)` and `tasks_query(overdue=true)`
  - `tasks_block(action="list")` — a blocked task cannot be distributed to a day

**Read recent daily notes** (last 3 days) for uncompleted carried items.

### 3. Analyse the week shape (Haiku subagent)

Spawn a **Haiku subagent** with all the raw data:

> You are planning a week for a family knowledge system. All data is provided below — do NOT call any MCP tools.
>
> **Calendar** — apply filtering rules:
> - Personal account: show full detail
> - Work account: show as "(busy)" blocks
> - Family member account: exclude entirely
>
> **Tasks** — from the ledger rows provided (uid, title, priority, due_at, queue, project, blocked_by):
> - highest and high priority items
> - Items with due dates falling in the planning window
> - Items already in the `week` queue (keep them; they were chosen)
> - Skip anything with an open blocker
>
> **Reminders** — current reminders that overlap with the planning window
>
> **Weekly review priorities** — if available, what did the user say matters most?
>
> Produce:
> 1. **Day-by-day shape**: For each day (Mon-Fri), list calendar events and rate the day as "packed", "moderate", or "light"
> 2. **Task distribution**: Suggest which tasks fit which days. Put demanding tasks on light calendar days. Put deadline items on or before their due date. Group related tasks.
> 3. **Overdue items**: List anything overdue with how many days overdue
> 4. **Reminder gaps**: High-priority tasks that don't have a matching Apple Reminder
> 5. **Week summary**: one line on the overall shape ("Front-loaded week — heavy Mon/Tue, lighter end of week")
>
> ---
> [paste all raw data here, clearly labelled]

### 4. Present the week shape

Show the day-by-day breakdown:

```
## Week of D Month — D Month YYYY

**Shape**: Front-loaded — packed Monday/Tuesday, lighter Thursday/Friday.

### Monday 7 Apr — packed 📅
- 09:00  Team standup
- 10:30  Dentist (Finn)
- 14:00  (busy)
- **Suggested tasks**: HSBC close accounts (high, 5 days overdue · TASK-0031), reply to SSE (highest, due tomorrow · TASK-0088)

### Tuesday 8 Apr — moderate 📅
- 11:00  (busy)
- **Suggested tasks**: Finn swimming registration (🔼 due 10 Apr), LinkedIn post draft (🔼)

### Wednesday 9 Apr — light ✨
- No calendar events
- **Suggested tasks**: Renovation tile decision (⏫), garden lights follow-up (🔼)

### Thursday 10 Apr — moderate 📅
- 09:30  Coffee with Stef
- **Suggested tasks**: Finn swimming registration (🔼 due today), tax return prep (🔼)

### Friday 11 Apr — light ✨
- No calendar events
- **Suggested tasks**: Weekly admin catch-up, backlog cleanup

---

**Overdue** (3 items):
- HSBC close accounts — 5 days overdue ⏫
- Finn GP appointment — 2 days overdue 🔺
- Reply to Crannarc re: tiles — 3 days overdue ⏫

**Reminder gaps** (2 items):
- HSBC close accounts — no Apple Reminder set
- Finn GP appointment — no Apple Reminder set
```

Then ask:

> "How does this look? You can:
> - Move tasks between days ('move tiles to Thursday')
> - Add/remove tasks ('drop LinkedIn, add car wash to Friday')
> - Adjust priorities ('HSBC is urgent')
> - Or say 'go' to scaffold."

### 5. Execute scaffolding

After the user confirms (or says 'go'):

**Commit the plan to the ledger first.** The week's shape lives in the queue and the due dates, not in the stubs:

- Every task the user kept → `tasks_bulk_update(updates=[{"uid": ..., "queue": "week"}, ...])`.
- A task assigned to a specific day that has no due date → include `"due_at": "<that day>"` in the same update, so the daily note's overdue/due-today query surfaces it on the day. Do not move an existing due date earlier without saying so.
- Anything the user dropped from the plan that was in the `week` queue → `"queue": null`.

**Create daily note stubs** for each weekday (Mon-Fri) via `Write`. Skip any day where a daily note already exists.

Stub format:

```markdown
---
date: YYYY-MM-DD
type: daily
---

# DayName, D Month YYYY

<< [[YYYY-MM-DD]] | [[YYYY-MM-DD]] >>

## Today

- HH:MM  Calendar event title
- ...

## Tasks

### Focus

<!-- Manual picks, set at lock-in via /tunetasks — capped at daily_note.focus_count
     (read fresh from the caller's preferences at lock-in, never a fixed number). -->

### From the backlog

> Live view of [[Task Backlog]] — these are not copies.

```tasks
not done
path includes Task Backlog.md
(priority is highest) OR (due before tomorrow)
sort by priority
```

## Email

## WhatsApp

## Meetings

## Notes
```

- The `## Today` section is pre-populated with calendar events for that day
- Tasks are **not** copied into the stub. The day's tasks come from the ledger via the live query block (the same one `/kickoff` renders), driven by the due dates and `week` queue set above — so a task moved in the app on Tuesday shows correctly on Wednesday without anyone editing a note
- `### Focus` is left empty — the user picks focus during `/tunetasks`' lock-in step each morning (or in the loops app)
- The `/kickoff` command already handles "if note exists, tell me to run `/checkin` instead" — so stubs are additive

**Create reminders** for tasks with reminder gaps:
- Call `reminders_add` for each high-priority task that doesn't have a matching reminder
- Set the due date to the day the task was distributed to
- Use the "Reminders" list (default)

### 6. Summary

Brief summary of what was scaffolded:

> "Week scaffolded:
> - 5 daily note stubs created (Mon-Fri)
> - 8 tasks committed to the week queue, 3 given due dates
> - 3 reminders created (HSBC, Finn GP, tiles)
> - 3 overdue items flagged for Monday
>
> Run `/kickoff` each morning to get the full briefing, then `/tunetasks` to pick your Focus."

## Notes

- This command creates *stubs*, not full daily notes. `/kickoff` adds weather, calendar, pulse, coffee, transport, consumables, snags and intake candidates when run each morning.
- If a daily note already exists for a day, it's skipped entirely — never overwrite.
- Tasks are suggestions. The user adjusts during step 4 before anything is created.
- Weekend days (Sat/Sun) are excluded by default unless the user explicitly asks for them.
