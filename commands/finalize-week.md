Finalise the weekly review with personal commentary and plan next week.

Arguments: $ARGUMENTS

Run this after `/weekly-review`. The draft review should already exist.

## Steps

### 1. Find the draft review

Read the most recent file in `vault/Weekly Reviews/YYYY/` (the one created by `/weekly-review`).

If no draft exists, tell the user to run `/weekly-review` first.

### 2. Process commentary

The user's arguments may contain:
- **Voice transcript / dictated notes**: Parse with a Haiku subagent, extract the perspective and priorities
- **Written notes**: Use directly
- **Nothing**: Ask what they thought of the week — what went well, what didn't, what's important for next week

Merge commentary into the review:
- First person, conversational tone (Alex's voice)
- Weight by what's strategically important
- Keep it concise — add perspective, don't bloat

Update the saved review file with the merged version.

### 3. Gather next week data (main agent — MCP tools + file reads)

Call these MCP tools:

- **`calendar_list_events`** — for the next 7 days
- **`reminders_list`** — all current reminders
- **`health_exercise_status`** — current week's exercise adherence (to inform next week's workout planning)

Also read:
- The unified backlog: `vault/Task Backlog.md`
- `vault/Health/Health Profile.md` — exercise targets and injury context
- Current week's daily notes for uncompleted carried items

### 4. Plan next week (Haiku subagent)

Spawn a **Haiku subagent** with all the raw data. Give it these instructions:

> You are planning next week from pre-fetched data. All API data and file contents are provided below — do NOT try to call any MCP tools or APIs.
>
> **Calendar** — apply filtering rules:
> - Personal account: show full detail
> - Work account: show as "(busy)" blocks
> - Family member account: exclude entirely (managed separately)
>
> **Tasks** — extract all open items from backlogs, grouped by priority.
>
> **Carried items** — from the current week's daily notes, what's still uncompleted?
>
> **Reminder sync** — compare reminders with high-priority backlog items and next week's plan. Flag:
> - High-priority tasks without reminders
> - Reminders that are done or no longer relevant
> - Focus suggestions for Monday's daily note
>
> **Exercise planning** — using the Health Profile targets (2x strength/week) and next week's calendar:
> - Identify 2-3 workout-friendly slots (mornings with no early meetings, light days)
> - Note any recovery considerations from this week's exercise status
> - Suggest specific workout slots: "Tuesday AM and Thursday AM look clear for S&C"
>
> Return: next week's calendar day-by-day, all open high-priority tasks (🔺 and ⏫), carried items, reminder sync gaps, and suggested workout slots.
>
> ---
> [paste all raw data here, clearly labelled by source]

### 5. Present the week ahead

Show a day-by-day preview of next week with:
- Calendar events
- Suggested focus areas based on priorities, deadlines, and what carried over
- Any days that look particularly busy or light

Present the reminder sync gaps and ask the user which to action.

### 6. Execute

For approved actions:
- Update backlogs (re-prioritise, move completed to Done, delete stale)
- Create/complete reminders via the `reminders_add` and `reminders_complete` MCP tools
- Pre-populate Monday's daily note Active section if the user wants

### 7. Close out

Summarise what was done:
- Review finalised and saved
- Backlog changes made
- Reminders synced
- Ready for Monday

Then suggest: "Run `/plan-week` to scaffold next week's daily notes and distribute tasks."

Keep it brief and positive.
