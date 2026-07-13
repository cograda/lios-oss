Scaffold the coming week — distribute tasks, create daily note stubs, set reminders.

Arguments: $ARGUMENTS

Run this after `/finalize-week` (or standalone any time you want to set up the week ahead).

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
- Read the unified backlog:
  - `vault/Task Backlog.md`

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
> **Tasks** — from the unified backlog, extract:
> - 🔺 Urgent and ⏫ High priority items
> - Items with due dates falling in the planning window
> - Carried items from recent daily notes (uncompleted Active/Focus)
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
- **Suggested tasks**: Close old bank accounts (⏫, 5 days overdue), reply to the energy supplier (🔺 due tomorrow)

### Tuesday 8 Apr — moderate 📅
- 11:00  (busy)
- **Suggested tasks**: Finn swimming registration (🔼 due 10 Apr), LinkedIn post draft (🔼)

### Wednesday 9 Apr — light ✨
- No calendar events
- **Suggested tasks**: Renovation tile decision (⏫), garden lights follow-up (🔼)

### Thursday 10 Apr — moderate 📅
- 09:30  Coffee with Cara
- **Suggested tasks**: Finn swimming registration (🔼 due today), tax return prep (🔼)

### Friday 11 Apr — light ✨
- No calendar events
- **Suggested tasks**: Weekly admin catch-up, backlog cleanup

---

**Overdue** (3 items):
- Close old bank accounts — 5 days overdue ⏫
- Finn GP appointment — 2 days overdue 🔺
- Reply to BuildCo re: tiles — 3 days overdue ⏫

**Reminder gaps** (2 items):
- Close old bank accounts — no Apple Reminder set
- Finn GP appointment — no Apple Reminder set
```

Then ask:

> "How does this look? You can:
> - Move tasks between days ('move tiles to Thursday')
> - Add/remove tasks ('drop LinkedIn, add car wash to Friday')
> - Adjust priorities ('the bank closure is urgent')
> - Or say 'go' to scaffold."

### 5. Execute scaffolding

After the user confirms (or says 'go'):

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



### Active

**From plan-week**
- [ ] Task distributed to this day ⏫
- [ ] Another task 🔼 📅 YYYY-MM-DD
- ...

## Email

## WhatsApp

## Meetings

## Notes
```

- The `## Today` section is pre-populated with calendar events for that day
- The `### Active` section has tasks distributed to that day under a "From plan-week" heading
- `### Focus` is left empty — the user picks focus during `/daily-note` each morning
- The `/daily-note` command already handles "if note exists, open and enhance it" — so stubs are additive

**Create reminders** for tasks with reminder gaps:
- Call `reminders_add` for each high-priority task that doesn't have a matching reminder
- Set the due date to the day the task was distributed to
- Use the "Reminders" list (default)

### 6. Summary

Brief summary of what was scaffolded:

> "Week scaffolded:
> - 5 daily note stubs created (Mon-Fri)
> - 8 tasks distributed across the week
> - 3 reminders created (old bank, Finn GP, tiles)
> - 3 overdue items flagged for Monday
>
> Run `/daily-note` each morning to get the full briefing and pick your Focus."

## Notes

- This command creates *stubs*, not full daily notes. `/daily-note` adds weather, email, WhatsApp, health, and the full briefing when run each morning.
- If a daily note already exists for a day, it's skipped entirely — never overwrite.
- Tasks are suggestions. The user adjusts during step 4 before anything is created.
- Weekend days (Sat/Sun) are excluded by default unless the user explicitly asks for them.
