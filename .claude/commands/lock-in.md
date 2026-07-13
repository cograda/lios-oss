Lock in today's focus and sync with Apple Reminders.

Arguments: $ARGUMENTS

Run this after `/daily-note`. The daily note should already exist; Focus is picked from the live [[Task Backlog]] query in the note (tasks are no longer copied into an Active list).

## Steps

### 1. Read today's daily note

Read `vault/Daily Notes/Alex/YYYY-MM-DD.md` (or Sam's if specified in arguments).

If the note doesn't exist, tell the user to run `/daily-note` first.

### 2. Check for Focus items

Look at the `### Focus` section. If the user has already filled it in (either manually in Obsidian or via arguments), use those items. If it's empty, ask the user what they want to focus on today — suggest candidates from the note's backlog query (urgent / overdue / this-week), and any actionable items from Email/WhatsApp/Health sections.

If arguments contain focus items (e.g. `/lock-in tax return, finn school form, call plumber`), parse those as the focus list.

### 3. Update the daily note

Write the chosen Focus items into the `### Focus` section:
```markdown
### Focus

- [ ] Focus item 1
- [ ] Focus item 2
- [ ] Focus item 3
```

Cap at 5 items. If the user lists more, ask them to prioritise.

### 3.5. Mirror Focus into the backlog (`#focus` tags)

The backlog's "🎯 Today's Focus" query (`vault/Task Backlog.md`) surfaces tasks tagged `#focus`. Keep it showing **only today's** picks:

1. `Read` `vault/Task Backlog.md`.
2. **Clear stale focus first** — remove the ` #focus` token from every task line that still carries it (yesterday's picks). Use a careful find-and-replace on the tag token only; don't touch anything else on the line.
3. **Tag today's picks** — for each chosen Focus item that matches an existing backlog task (fuzzy match on text, ignore emoji/dates/links), append ` #focus` to the end of that task line.
4. **Ad-hoc picks** — if a Focus item isn't in the backlog yet, add it under the right domain in the **`## 📌 This Week — active`** section with a sensible priority (🔺/⏫) and ` #focus`, so it's tracked in the one place rather than living only in the daily note.
5. Save. Edits are surgical (add/remove a tag token, or append one task line) — never reorder or restructure the file.

The daily note's `### Focus` checkboxes (step 3) stay as the human-facing list; the `#focus` tag is purely the mechanism that drives the backlog's Focus query. The two should always match.

### 4. Fetch reminder and backlog data (main agent)

Call the **`reminders_list`** MCP tool to get all incomplete reminders with their titles, lists, due dates, and priorities.

Also read the single unified backlog:
- `vault/Task Backlog.md` (the one list — the `Alex/`·`Sam/` files are retired stubs)

### 5. Sync analysis (Haiku subagent)

Spawn a **Haiku subagent** with the reminders data and backlog contents. Give it these instructions:

> You are syncing Apple Reminders with the vault task backlogs. All data is provided below — do NOT try to call any MCP tools or APIs.
>
> **Fuzzy match:**
> For each reminder, find the closest matching backlog task (case-insensitive, partial match, ignore emoji/dates). Score as:
> - **Match**: reminder clearly corresponds to a backlog item
> - **Reminder only**: exists in Reminders but not in any backlog
> - **Backlog only**: exists in backlog but not in Reminders
>
> **Report:**
> Return three lists:
> 1. **Matched** — reminder ↔ backlog pairs (no action needed)
> 2. **In Reminders, not in backlog** — suggest adding to appropriate backlog
> 3. **In backlog, not in Reminders** — suggest creating reminders for high-priority items (🔺 and ⏫ only)
>
> Also flag any Focus items from today that don't have a corresponding reminder.
>
> ---
> **REMINDERS DATA:**
> [paste reminders_list result]
>
> **UNIFIED BACKLOG:**
> [paste Task Backlog.md content]
>
> **TODAY'S FOCUS:**
> [paste Focus items from daily note]

### 6. Present sync results

Show the Haiku agent's sync report. For each gap:
- **Reminder → Backlog**: "Add 'X' to shared/personal backlog?"
- **Backlog → Reminder**: "Create reminder for 'X'?"
- **Focus without reminder**: "Sync 'X' to Reminders for today?"

### 7. Execute sync actions

For items the user approves:
- **Add to backlog**: Append task to the appropriate backlog file with sensible priority
- **Create reminder**: Call the `reminders_add` MCP tool with the task title, list name, and due date
- **Complete reminder**: Call the `reminders_complete` MCP tool for any done items still showing in Reminders
- **Create calendar event**: For items that are 2+ weeks out or time-specific, offer to create a Google Calendar event via the `calendar_create_event` MCP tool. Examples: "Gig tickets on sale 10am April 2nd" → calendar event. "Dee's birthday next weekend" → all-day event. Ask the user: "This is far out — want a calendar entry too?"

### 8. Confirm

Tell the user they're locked in. Show their Focus list and any sync actions taken. Keep it brief — they want to start working.
