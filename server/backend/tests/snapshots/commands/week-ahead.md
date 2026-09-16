<!-- GENERATED from server/backend/app/prompts/templates/week-ahead.md.j2 via app.prompts.commands — do not hand-edit. Edit the template and run scripts/render_commands.py (or refetch GET /api/v1/commands). -->

Show what's coming up this week from the family calendar.

## Steps

### 1. Gather data (main agent — MCP tools)

Call the **`calendar_list_events`** MCP tool for the coming 7 days.

> **The backlog is a ledger, not a file.** Never read `Task Backlog.md` for state and never
> edit it — it is a rendered view and a hand edit locks every task write. Read with
> `tasks_query`, write with `tasks_add` / `tasks_update` / `tasks_complete` /
> `tasks_bulk_update`; every write re-renders the file.

Also call **`tasks_query(due_before=<7 days from today, ISO date>)`** for tasks with due
dates this week.

### 2. Process (Haiku subagent)

Spawn a **Haiku subagent** with the raw calendar data and the `tasks_query` result. Give it these instructions:

> You are building a week-ahead view from pre-fetched data. All data is provided below — do NOT try to call any MCP tools or APIs.
>
> Events are pre-filtered by the server (hidden calendars excluded, work events show as "(busy)"). Show them as returned.
>
> **Tasks** — extract tasks with due dates in the coming 7 days from the `tasks_query` result.
>
> Format as a day-by-day view:
> - Use Irish date formatting: "Monday 24th March"
> - Show time, event title, and calendar source
> - Null summary events → "(busy)" without detail
> - All-day events at the top of each day
> - Highlight anything needing preparation (travel, appointments with locations, early starts, fasting)
>
> End with a **Heads up** section if there are:
> - Overdue tasks
> - Events needing prep
> - Schedule clashes
> - Unusually busy days
>
> ---
> **CALENDAR DATA:**
> [paste calendar_list_events result]
>
> **TASKS:**
> [paste tasks_query result]

### 3. Present

Show the subagent's formatted output. Keep it concise and scannable. Quick morning check, not a report.
