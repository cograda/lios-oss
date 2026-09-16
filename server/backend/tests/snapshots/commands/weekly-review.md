<!-- GENERATED from server/backend/app/prompts/templates/weekly-review.md.j2 via app.prompts.commands — do not hand-edit. Edit the template and run scripts/render_commands.py (or refetch GET /api/v1/commands). -->

Generate the weekly review digest.

Arguments: $ARGUMENTS

Supports an `--auto` argument to skip interactive pauses (original one-shot behavior). Default is interactive.

## Phase 1 — Gather and synthesise

### 1. Determine review period

Monday to Sunday of the current or most recent full week. If today is Sunday, review the current week. If today is Monday-Saturday, review the week that just ended.

### 2. Gather data (main agent — MCP tools + file reads)

Call these MCP tools:

- **`calendar_list_events`** — for the review period (Monday to Sunday)
- **`lastfm_stats`** — for the week's listening overview (top artists, total scrobbles)
- **`gmail_recent`** — recent messages (limit: 20) to spot significant threads
- **`health_weekly_summary`** with `{"week_start": "YYYY-MM-DD"}` — aggregated sleep, vitals, movement, exercise, recovery
- **`health_exercise_status`** with `{"week_of": "YYYY-MM-DD"}` — workout adherence vs the
  weekly strength-session target it returns (`target.strength_sessions`)
- **`ha_history`** with `{"days": 7}` for each household rhythm entity: washer (`sensor.utility_room_washing_machine_machine_state`), dryer (`sensor.utility_room_dryer_machine_state`), dishwasher (`sensor.dishwasher_operation_state`), sprinkler (`switch.sonoff_hydro_duo_1`, `switch.sonoff_hydro_duo_2`) — appliance cycles and watering runs for the week

Also read these vault files directly:

- **`vault/Health/Health Profile.md`** — current goals, injury context, exercise targets
- All daily notes from the week: `vault/Daily Notes/Alex/YYYY-MM-DD.md` for each day
- Meeting notes from the week: glob `vault/Meetings/` for files with dates in the review period
- The ledger, not the file:

> **The backlog is a ledger, not a file.** Never read `Task Backlog.md` for state and never edit it — it is a rendered view and a hand edit locks every task write. Read with `tasks_query`, write with `tasks_add` / `tasks_update` / `tasks_complete` / `tasks_bulk_update`; every write re-renders the file.

  - `tasks_history(limit=300)` — every completion and field change this week, with who did it (`done` and `tidied` counts come back with it)
  - `tasks_query(overdue=true)` and `tasks_query(queue="week")` — what is overdue, what was committed to this week and did not close
  - `tasks_query(status="someday", limit=200)` — parked items, for the Stale section (aging is `created_at`, or unknown for imported rows)
- Any other vault files modified in the review period (glob, exclude daily notes and meetings)

### 3. Process data (Haiku subagent)

Spawn a **single Haiku subagent** with all the raw data. Give it these instructions:

> You are building a weekly review from pre-fetched data. All API data and file contents are provided below — do NOT try to call any MCP tools or APIs.
>
> **Calendar** — apply filtering rules:
> - Personal account: show full detail
> - Work account: show times only as "(busy)"
> - Family member account: exclude entirely (managed separately)
> - Holidays: note any public holidays that fell in the week
>
> **Daily notes** — extract: Focus items (done vs not), Active items, meeting references, Notes content
>
> **Meeting notes** — extract: title, participants, key decisions, action items
>
> **Ledger changes** — from the tasks_history events: what was completed this week (status → done), what was tidied (field events) without being done; from the queries: what is newly overdue, what was in the week queue and is still open, and someday items older than 3 months
>
> **Listening** — brief colour note if interesting (e.g. "Heavy techno week" or "Lots of new discoveries")
>
> **Health** — extract from health_weekly_summary and health_exercise_status data:
> - Sleep: average hours, best/worst nights, stage quality
> - Vitals: resting HR, HRV with trend direction, recovery assessment
> - Movement: average daily steps, total distance
> - Exercise: workouts completed vs the strength-session target `health_exercise_status`
>   reports (`target.strength_sessions`), workout types and durations
> - Food: scan daily note `## Food` sections for patterns (home-cooked vs takeaway, variety, skipped meals)
> - Cross-reference Health Profile for context (knee rehab progress, BJJ timeline)
>
> **House** — from the ha_history data, count the week's household cycles: washes and dries (transitions to `run` in `counts_by_new_state`), dishwasher runs (transitions to `run`), sprinkler runs per channel (transitions to `on`). Note anything off-rhythm — no washes all week, sprinkler running daily, dishwasher door left open. One short labelled block; if all history is empty (integration just installed), say so rather than reporting zeros as fact.
>
> **Email** — note any significant volume or patterns, actionable items, important threads
>
> Return all sections clearly labelled with raw data. Do not synthesise — the main agent will write the review.
>
> ---
> [paste all raw data here, clearly labelled by source]

## Phase 2 — Interactive review (skip if `--auto`)

If `--auto` was passed in arguments, skip to Phase 3.

Otherwise, present the review section by section and pause for input at each:

### 4a. This Week + Kids

Present the "This Week" and "Kids" sections (highlights, outcomes, notable events, child updates). Then ask:

> "Anything to add, correct, or reframe? Or say 'good' to continue."

Incorporate any feedback before continuing.

### 4a½. Health & Fitness

Present the "Health & Fitness" section (sleep, vitals, movement, exercise, food patterns). Then ask:

> "Health check — anything to add about how the week felt physically? Knee status, energy levels, anything the data doesn't capture? Or 'good' to continue."

Incorporate any feedback. If the user mentions knee progress, pain changes, or physio updates, note them for the Health Profile.

### 4b. Decisions

Present the "Decisions" section (decisions made this week from meetings, conversations, daily notes). Then ask:

> "Any decisions I missed, or corrections? Or 'good' to continue."

### 4c. Tasks

Present the "Tasks" section — Done, Still Open, Stale. Then ask:

> "Any items to update? E.g. 'X is actually done', 'drop Y', 'Z should be urgent'. Or 'good' to continue."

Apply any changes to the review draft AND to the ledger: `tasks_complete(uid)` for "X is actually done", `tasks_update(uid, priority=...)` for priority calls, `tasks_update(uid, status="dropped")` or `status="someday"` for "drop Y". Never delete; never edit the file.

### 4d. Week Ahead

Present the "Week Ahead" section (calendar preview, things needing prep). Then ask:

> "What are your priorities for next week? Anything I should highlight or flag?"

Incorporate into the Week Ahead section and note priorities for `/plan-week`.

## Phase 3 — Finalise and save

### 5. Synthesise the draft

If interactive (Phase 2 was run), merge all feedback into a clean final draft. If `--auto`, synthesise directly from the Haiku output.

Write the review as a briefing — something Alex and Sam read together on Sunday morning. Warm, useful, scannable. Not a data dump.

```markdown
---
title: "Week of YYYY-MM-DD"
type: review
created: YYYY-MM-DD
modified: YYYY-MM-DD
tags: [review, weekly]
week_start: YYYY-MM-DD
week_end: YYYY-MM-DD
---

# Week of D Month — D Month YYYY

## This Week

What happened. Highlights, outcomes, notable events. 3-5 bullets. Include a listening colour note if interesting (e.g. "Heavy techno week" or "Lots of new discoveries").

## Health & Fitness

**Sleep**: Averaged X.Xh (deep X.Xh, REM X.Xh). Best night: day (Xh). Rough night: day (Xh, reason if known). Trend: stable/improving/declining.

**Vitals**: Resting HR XX, HRV XXms (↑/↓ vs prior week). Recovery: good/fair/poor.

**Movement**: X,XXX avg daily steps. X.X km total distance.

**Exercise**: X/2 strength sessions. [List each workout: type, duration, day]. *Adherence note and coaching comment — on track / need to prioritise consistency / well done.*

**Food**: Observational patterns from daily note Food sections. Home-cooked vs takeaway, variety, notable gaps.

*Brief overall assessment: how the body is doing relative to goals (knee rehab, BJJ prep, general fitness).*

## House

Household rhythm from Home Assistant: `X washes · X dries · X dishwasher runs · sprinkler Xx (zone 1) / Xx (zone 2)`, plus one line on anything off-rhythm. Omit the section if the history is empty (integration newly installed).

## Kids

Finn and Isla updates — school, activities, medical, milestones. Only include if there's something to report.

## Decisions

Any decisions made this week (from meetings, conversations, or daily notes). Brief and factual.

## Tasks

### Done
- What got completed this week (from `tasks_history`, plus daily-note checkoffs that were not also in the ledger)

### Still Open
- What's overdue or carried forward. Grouped by shared/personal.

### Stale
- Items parked 3+ months — flag for deletion or re-prioritisation.

## Week Ahead

Preview of next week from the calendar. Flag anything that needs prep. Include priorities from the interactive session if available.

## Notes

New notes or reference material added this week. Brief mentions.
```

### 6. Save

Save to `vault/Weekly Reviews/YYYY/YYYY-MM-DD.md` (Sunday date). Create the year subfolder if it doesn't exist.

### 7. Ledger hygiene

As part of the review:
- Anything marked `[x]` in a daily note this week that is still open in the ledger → `tasks_complete`
- `tasks_query(status="waiting")` — has anything we were waiting on come in? `tasks_update(uid, status="next")` or complete it
- Point at `/tunetasks` if the review surfaced more than a handful of badly-written or duplicate lines — that is its job, not this one's
- Clear the week queue for the new week at the end, if the user agrees: `tasks_query(queue="week")` → `tasks_bulk_update` those uids to `queue: null` (or leave the ones being carried)

### 8. Commentary (was `/finalize-week`, folded in 2026-09-03)

Ask for the user's own read of the week — what went well, what didn't, what matters next week. Voice transcript, written notes, or a conversation. Merge it into the saved review in the first person, Alex's voice; weight by what is strategically important; add perspective, don't bloat. Update the file. If the user has nothing to add, skip without fuss.

### 9. Present

Show the final draft and tell the user: "Run `/plan-week` to shape next week — it commits tasks to the week queue and writes the day stubs."

## Privacy

- Don't include content from `Alex/` or `Sam/` private spaces
- Only surface shared content and calendar events
- Work calendar events with null summaries → "work commitments"
- Use Irish date formatting in prose (Monday 17th March)
- Keep it to one page. Brevity is a feature.
