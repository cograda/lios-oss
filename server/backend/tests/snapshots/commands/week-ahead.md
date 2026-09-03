<!-- GENERATED from server/backend/app/prompts/templates/week-ahead.md.j2 via app.prompts.commands — do not hand-edit. Edit the template and run scripts/render_commands.py (or refetch GET /api/v1/commands). -->

Show what's coming up this week from the family calendar.

## Steps

### 1. Gather data (main agent — MCP tools + file reads)

Call the **`calendar_list_events`** MCP tool for the coming 7 days.

Also read the backlog for tasks with due dates this week:
- `vault/Task Backlog.md`

### 2. Process (Haiku subagent)

Spawn a **Haiku subagent** with the raw calendar data and backlog contents. Give it these instructions:

> You are building a week-ahead view from pre-fetched data. All data is provided below — do NOT try to call any MCP tools or APIs.
>
> Events are pre-filtered by the server (hidden calendars excluded, work events show as "(busy)"). Show them as returned.
>
> **Tasks** — extract tasks with due dates in the coming 7 days from the backlogs.
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
> **BACKLOG:**
> [paste Task Backlog.md content]

### 3. Present

Show the subagent's formatted output. Keep it concise and scannable. Quick morning check, not a report.
