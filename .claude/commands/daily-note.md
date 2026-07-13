Create or open today's daily note for Alex.

Arguments: $ARGUMENTS

## Steps

### 1. Determine date, user, and lookback window

- Today's date and day of the week
- Default: Alex (`vault/Daily Notes/Alex/YYYY-MM-DD.md`)
- **Lookback window**: Calculate how far back to pull context:
  - **Monday**: back to Friday (3 days) — cover the full weekend
  - **Saturday or Sunday**: back to Friday (1-2 days)
  - **Tue–Fri**: 48 hours (2 days)
  - Use the lookback start date for WhatsApp, email, and previous daily notes

### 2. Check if note exists

- **If today's note already exists, delegate to `/refresh`.** The full creation pipeline (steps 3–8 below) is for first-time-of-day note creation; once the note exists, the right shape is "re-fetch live data and update sections in place + reconcile reminders," which is exactly what `/refresh` does. Pass through any user arguments that look like section filters (`email`, `transport`, etc.) or `--full`. After delegation, stop — do not continue to step 3.
- If the note doesn't exist, continue to step 3 (full creation flow).

### 3. Gather data

All data comes through MCP tools served by the local comar client (which proxies to the server as needed). No direct server access or shell commands required.

### 4. Fetch data (MCP tools)

Call these MCP tools **in parallel where possible**:

**Core context:**
- **`system_alerts`** — check for integration health issues (failing/stale syncs)
- **`calendar_today`** — today's events
- **`weather_current`** — current conditions
- **`weather_forecast`** — today's high/low
- **`reminders_sync`** with `{"since": "<mtime of last daily note, ISO>"}` — open reminders **plus** what was completed/added/edited on the phone since the last daily note. Prefer this over `reminders_list` so items ticked off on your phone aren't silently dropped. If no previous daily note, omit `since` (defaults to 24h).
  - **Two timestamp fields, two different meanings.** `synced_at` = MAX(Reminder.synced_at) = last time any reminder *changed*. It legitimately sits still during quiet periods — **do not warn on it**. `bridge_verified_at` = last time the comar-client daemon pinged `/reminders/verified` (~30s when healthy). For the "daemon stale" warning, use `bridge_verified_at` — if it's more than ~5 min old, the EventKit bridge is offline; surface as a ⚠️ at the top of the note. If only `bridge_verified_at` is null (server pre-2.2.0 or daemon pre-2.2.0), fall back to `system_alerts` and don't warn from this tool alone.

**Transport (weekdays only — skip on Sat/Sun):**
- **`rail_departures`** with `{"direction": "Southbound", "limit": 5}` — next southbound DARTs from Malahide (the home station is configurable server-side)

**House (Home Assistant):**
- **`ha_home_status`** — appliances, active media, sprinkler valve, lights/switches on, offline/battery attention, staleness flag
- **`ha_history`** with `{"entity_id": "sensor.utility_room_washing_machine_machine_state", "days": 2}` — recent wash cycles
- **`ha_history`** with `{"entity_id": "sensor.utility_room_dryer_machine_state", "days": 2}` — recent dryer cycles
- **`ha_history`** with `{"entity_id": "sensor.dishwasher_operation_state", "days": 2}` — recent dishwasher cycles

**Comms (use lookback window):**
- **`gmail_unread`** — unread emails (limit: 10)
- **`gmail_recent`** with `{"limit": 50}` — last 50 emails (covers full lookback window)
- **`whatsapp_recent`** with `{"limit": 50}` — recent WhatsApp messages across all chats
- **`attachments_pending`** with `{"since_days": 7, "limit": 10}` — documents (PDF/docx/xlsx) shared in WhatsApp that you haven't ingested yet. Surface in the briefing so the user can choose to ingest before the WA CDN copy expires (~30 days).
- **`inbox_pending`** — files dropped via the iOS-Shortcut/automation webhook (PDFs, text, recipes, screenshots) waiting for the user to decide where they go. Call this even if the hourly enrich worker hasn't fired yet — the tool inline-enriches any unprocessed sidecars on read, so freshness is guaranteed. If `count > 0`, the post-briefing Inbox triage step (7.5) fires.

**Health:**
- **`health_summary`** — pre-formatted health snapshot (steps, sleep, HR, workouts)
- **`health_sleep`** — granular sleep stage breakdown (deep, REM, core, awake times and sessions)
- **`health_trends`** with `{"days": 7}` — weekly trends for context (averages, direction)
- **`health_workouts`** with `{"days": 7}` — recent workouts (type, duration, HR, distance)

**Health profile (file read):**
- Read **`vault/Health/Health Profile.md`** — current goals, injury context (shoulder), exercise programming, weekly targets. Pass to Haiku subagent for exercise coaching context.

**Music:**
- **`lastfm_recent`** with `{"limit": 15}` — what's been playing recently
- **`lastfm_stats`** with `{"period": "this_week"}` — weekly listening stats (top artists, genres)

**Coffee:**
- **`coffee_current`** — bags marked `status=current` (what you're drinking). Likely 1-3 rows.
- **`coffee_recent_brews`** with `{"limit": 7}` — last week of brews for trend assessment.

The Coffee section is **omitted entirely** if both calls return empty (no current bags AND no recent brews).

**Task carry-forward:**
Read previous daily notes covering the full lookback window (same user). On Mon–Fri read at least 2 previous notes; on Monday read back to Friday (up to 4 notes). Always read up to **7 previous daily notes** if needed — stop reading further back once you hit a note where all Focus items are completed. Collect all of them for the Haiku subagent.

If any MCP tool returns an error, note the error but continue with the data you have. Do not silently omit a section — show "⚠️ [tool] unavailable" instead.

### 5. Process data (Haiku subagent)

Spawn a **single Haiku subagent** with all the raw MCP results and the recent daily notes. Give it these instructions:

> You are building an opinionated morning briefing from pre-fetched data. All API data is provided below — do NOT try to call any MCP tools or APIs.
>
> **Your job is not just to format data — it's to give a useful opinion.** Flag conflicts, suggest priorities, note trends, and say what matters today. Be direct and concise. Imagine you're a sharp executive assistant who knows the full context.
>
> **Context**: Today is [DAY], [DATE]. The lookback window covers [LOOKBACK_START] to yesterday.
>
> **4z. System Alerts** — check the alerts data:
> - If status is "all_ok", skip this section entirely
> - If status is "degraded", list each alert with its integration name and issues
> - Format as: `⚠️ **integration**: issue description`
> - This goes at the very top of the briefing output
>
> **4a. Calendar** — format the events from the calendar data:
> - `HH:MM  Event title` for timed events
> - `ALL DAY  Event title` for all-day events
> - Events are pre-filtered by the server (hidden calendars excluded, work events show as "(busy)"). Show them as returned.
>
> **4b. Weather** — combine current + forecast into a one-liner:
> `☁️ 12°C, cloudy, high 14°C / low 8°C, light rain expected`
>
> **4c. Reminders** — format the reminder items:
> - **Overdue** (due_date before today): show each item with title and how overdue it is
> - **Due today**: show each item with title
> - **High priority** (priority = high or urgent, no due date): show each item
> - Skip the "Groceries" list unless there are overdue items in it
> - Skip completed items
> - Cap at 15 items total — if more, show the count of remaining
>
> **4d. Email digest** — triage by urgency, not just list:
> - Scan ALL emails (unread + recent in lookback window). Skip newsletters, automated notifications, marketing.
> - **Needs action today**: things with deadlines, direct asks, time-sensitive items. Say WHY it's urgent.
> - **Should respond this week**: important but not urgent. One line each.
> - **FYI only**: notable updates worth knowing about but no action needed. One line each, or skip if nothing interesting.
> - Group by account if multiple accounts
> - On Mondays, be thorough — weekend emails often contain things that need action on Monday morning
> - Be opinionated: if something looks like it's been sitting for days and needs a reply, flag it
>
> **4e. WhatsApp digest** — triage by urgency, not just list:
> - Skip casual chat, memes, group banter
> - **Needs a response**: questions asked of Alex/Sam, decisions awaiting input, logistics that need confirmation. Say who's waiting and how long they've been waiting.
> - **Plans being made**: things being organised that Alex should be aware of. One line each.
> - **FYI**: notable updates, no action needed. Skip if nothing interesting.
> - If nothing actionable, say "Nothing actionable in recent WhatsApp"
> - On Mondays, cover the full weekend — don't miss plans made on Saturday/Sunday
> - Be opinionated: if someone asked a question 2 days ago with no reply, flag it prominently
>
> **4f. Pulse — personal status header**
>
> Build a single cohesive status block with four sub-sections. Each sub-section has two lines: line 1 is **quantitative** (numbers, dense, scannable), line 2 is a **qualitative** *italic* opinion. Sleep & Vitals leads because recovery sets the day's tone.
>
> Format:
>
> **😴 Sleep & Vitals**
> `Xh XXm` total — deep Xh, REM Xh, core Xh, awake Xm. Bed HH:MM → HH:MM. Resting HR XX · HRV XX ms.
> *Opinion on sleep quality and recovery readiness. Cross-reference HRV trend vs 7-day avg (↑/↓). "HRV climbing back — nervous system recovering well" or "HRV dropped to 28 from 42 — go easy today."*
>
> **🏃 Movement**
> X,XXX steps · X.X km · XXX kcal active. Yesterday: X,XXX steps. 7-day avg: X,XXX (↑/↓).
> *Opinion on activity level and trend. "Below average this week — try to get a walk in" or "Consistently hitting 9k — good baseline."*
>
> **💪 Exercise**
> [Yesterday's workout if any: type, duration, avg HR.] This week: X/2 strength sessions. [Next best slot from calendar if target not met.]
> *Coach-like guidance using Health Profile context. Reference shoulder status, weekly targets, and what makes sense today. Examples: "Good lower body session yesterday. One more S&C this week — Thursday evening looks clear. Consider legs again: leg press, hamstring curls, step-ups, then 15 min bike." or "No sessions yet and it's Thursday. Even 30 min of lower body + shoulder rehab counts." or "2/2 done — target met. Active recovery today if you feel like it."*
>
> **🎵 Listening**
> XX scrobbles this week. Heavy rotation: Artist (X plays), Artist (X plays). Top genres: genre, genre.
> *Mood/vibe inference — what the music says about headspace. Note any shifts.*
>
> Rules:
> - **Sleep & Vitals**: Use health_sleep for stage breakdown and session times. Use health_summary for resting HR and HRV. Use health_trends for 7-day averages of sleep, resting HR, and HRV to assess direction.
> - **Movement**: Use health_summary for today's steps/distance/energy and yesterday's comparison. Use health_trends for 7-day step averages to say whether activity is up or down. Do NOT include HR/HRV here (those are in Vitals).
> - **Exercise**: Use health_workouts for recent workout details. Count workouts where type is strength_training, hiit, cross_training, or core_training as "strength sessions" toward the 2/week target. Use the Health Profile (provided below) for injury context, exercise programming, and progression criteria. Cross-reference with today's calendar to suggest workout slots.
> - **Listening**: Use lastfm_stats for counts and top artists/genres. Use lastfm_recent for recent rotation and to detect shifts.
> - If sleep data is missing or shows a sync gap (e.g. 0h or no sessions), say "Sleep data not syncing — check Watch connection."
> - If no workout data, the Exercise section should still appear — note the gap and suggest a session.
> - If no lastfm data, omit the Listening sub-section entirely. Pulse works without it.
> - Keep the whole block to **8-12 lines**. Dense, not verbose. The Exercise section earns extra space for coaching.
> - The opinion lines should be direct and coach-like: "Bad night, go easy" or "Solid recovery, you're good."
> - Cross-reference between sub-sections: "Bad sleep + no workout this week = suggest a light session today, nothing heavy." "HRV recovering well + clear evening = good day for a proper S&C session."
>
> **4f-bis. Coffee** — short, omit if nothing to show:
> - If `coffee_current` returned 0 coffees AND `coffee_recent_brews` returned 0, omit the Coffee section entirely.
> - Otherwise format as: line 1 lists the current bag(s), line 2 the most recent brew result, line 3 (optional) a one-line nudge.
> - Format example:
>   ```
>   ☕ Drinking: **Colombia Vianí** (Hillside Roasters, washed, light) · **Los Chorros Pink Bourbon** (Batch Coffee, filter)
>   Last brew: V60, 15→250g, 3:30, overall 4/5 — "balanced, slight tang"
>   *Last 3 espresso brews trended sour — try a finer grind today.*
>   ```
> - Trend opinion only fires if there are 3+ brews of the same coffee in the last 7 days AND a clear pattern (avg `acidity` ≥ 4 = sour/under, `bitterness` ≥ 4 = over, declining `overall`). Otherwise omit the italic line.
> - Cap at 3 lines total.
>
> **4g. Transport** (weekdays only — omit section entirely on Sat/Sun):
> - Show the next 3-5 southbound DARTs from Malahide with scheduled and expected departure times
> - Flag any delays (expected ≠ scheduled)
> - Note which trains stop at Connolly — DART services (destinations like Bray, Dun Laoghaire) all stop at Connolly. Longer-distance commuter services may skip intermediate stations.
> - Keep to 2-3 lines
> - Format: `🚂 Next DARTs: 10:04 → Bray, 10:34 → Dun Laoghaire, 11:03 → Bray`
> - If a train is delayed: `10:34 → Dun Laoghaire (exp 10:38, +4 min)`
> - If no rail data available, show "⚠️ Rail data unavailable"
>
> **4g-bis. House** — home status from the Home Assistant data (`ha_home_status` + appliance `ha_history`). One always-on ops line plus up to 3 insight lines that only fire when there's something to say. Cap at 4 lines total.
> - **Laundry & dishwasher**: cross-reference the history transitions (last ~36h) with current state. If a cycle finished since the last daily note and the machine is now stopped, flag the hanging job: `🧺 Washer finished 21:36 last night — needs emptying?` If a machine is running now, show when it's done (completion_time / program_finish_time): `🧺 Dryer running — done 10:45.` Nothing ran, nothing running → no line.
> - **Anomalies**: media players still playing this morning, lights/switches on (the curated `lights_switches` section — it excludes config toggles), sprinkler valve currently open, dishwasher door open overnight. One combined line, only if any fire.
> - **Consumables**: any `*_nearly_empty` binary sensor that's `on` → one line, and suggest a `#quick` task (e.g. dishwasher salt).
> - **Ops line (always last)**: `🏠 N entities · M offline (Δ vs yesterday) · batteries OK · live`. Compute the offline delta by parsing the previous daily note's House ops line (previous notes are provided); first run or unparsable → show the count with "(baseline)". If the data has `stale: true`, lead the whole section with `⚠️ HA data stale — event stream may be down.` NEVER enumerate the standing offline fleet (ESPHome boards pending reflash) — the delta is the signal, not the list.
>
> **4h. Stalled-task analysis (read-only — do NOT copy tasks into the note)** — scan ALL provided daily notes (covering the lookback window and beyond) to *inform the Morning assessment and Focus suggestions only*. The daily note no longer carries an Active checkbox list — open tasks live in the single [[Task Backlog]] and are surfaced via a query (see step 6). Your job here is to spot what's drifting:
> - Tasks that were a previous day's Focus but still aren't marked done → flag as carried/stalled
> - For anything that's been hanging around 3+ days, note "(stalled Xd)" so it can be called out in the assessment
> - Uncompleted meeting action items → flag for Focus consideration
> - Produce a short list of stalled/at-risk items for steps 4i (assessment + Focus suggestions). Do NOT emit these as `- [ ]` checkboxes; they already exist in the backlog.
>
> **4h-bis. Renovation prior context** — conditional. Only run if today's calendar, emails, WhatsApp, OR carried tasks mention any of: Barry, Dara Nolan, Niall, Ken, the Mill, BoQ, bill of quantities, recommendation for payment, PC sum, spec / specification, variation, QS, site meeting, drawings, M&E.
>
> If yes, the main model (not the Haiku subagent) calls `renovation_context` once with a query derived from the matching topic (e.g. "shower niche dimensions", "recommendation payment no 5", "M&E specification"). Pass `{"limit": 5}`. The purpose is to surface prior decisions and rates the assistant should already know before producing the briefing.
>
> Fold the top 1-3 hits into the Morning assessment's Conflict flags or Focus suggestions as `**Prior:** <one-line summary> — [[source filename]] (YYYY-MM-DD)`. Do NOT dump the corpus hits verbatim.
>
> If no trigger keywords match today's data, skip this step silently — it should never add noise on a normal day.
>
> **4i. Morning assessment** — cross-reference EVERYTHING and give an honest assessment:
>
> **Day shape**: Look at the calendar, the task list, and the Pulse data (sleep & vitals, movement, exercise, listening). Reference it directly — "You slept badly" not "The health data shows..." Factor in exercise status: if behind on the 2/week target, suggest a slot. If recovering from a session, note it. The Pulse section gives the raw data; your job is to weave it into the day's story. Is this going to be a heavy day or a light one? Say it plainly:
> - "Packed day — 3 meetings and a deadline. Protect your focus time."
> - "Light calendar. Good day to tackle the backlog."
> - "You're running on bad sleep with a busy afternoon. Keep the morning simple."
>
> **Conflict flags**: Surface anything that clashes or needs attention:
> - Bad sleep + packed calendar = warning
> - Overdue items that keep getting carried = call it out
> - Time-sensitive items that could get missed (e.g. tickets on sale at 10am, drop-off at same time)
> - Unanswered messages where someone is waiting
> - Exercise target at risk (e.g. "0/2 strength sessions and it's Friday — last chance this week")
> - Bad recovery markers (low HRV + poor sleep) + planned workout = suggest going light or resting
>
> **Focus suggestions**: Based on everything — due dates, overdue items, stalled tasks, incoming email/WhatsApp, calendar shape, energy level — suggest 3-5 Focus candidates with one-line reasoning for each:
> - "**Close old bank accounts** — quick phone call, clears mental overhead and has been sitting for days"
> - "**Finn / the clinic** — carried from yesterday, quick call"
> - "**Energy supplier meter reading** — 3-day deadline, Sam forwarded it"
> - "**Photo print for Dee** — needs Amazon order today if it's arriving for the weekend"
> - "**Message Pat** — meaty message but gets the renovation moving"
>
> These are suggestions, not decisions — the user picks their own Focus. But give real reasoning, not just a list.
>
> Return all sections clearly labelled with the actual data.
>
> ---
> **SYSTEM ALERTS:**
> [paste system_alerts result]
>
> **CALENDAR DATA:**
> [paste calendar_today result]
>
> **WEATHER DATA:**
> [paste weather_current and weather_forecast results]
>
> **REMINDERS DATA:**
> [paste reminders_sync result]
>
> **EMAIL DATA:**
> [paste gmail_unread result]
>
> **RECENT EMAILS (lookback window):**
> [paste gmail_recent result]
>
> **WHATSAPP RECENT:**
> [paste whatsapp_recent result]
>
> **PENDING ATTACHMENTS:**
> [paste attachments_pending result — surface in briefing under WhatsApp section as "📎 New documents waiting to ingest"; mention filename + sender + age. WhatsApp's CDN purges sender bytes after ~30 days, so flag anything older than 14 days as urgent.]
>
> **HEALTH SUMMARY:**
> [paste health_summary result]
>
> **HEALTH SLEEP (last night):**
> [paste health_sleep result]
>
> **HEALTH TRENDS (7 days):**
> [paste health_trends result]
>
> **HEALTH WORKOUTS (7 days):**
> [paste health_workouts result]
>
> **HEALTH PROFILE:**
> [paste contents of vault/Health/Health Profile.md — goals, injury context, exercise programming, weekly targets]
>
> **LAST.FM RECENT:**
> [paste lastfm_recent result]
>
> **LAST.FM WEEKLY STATS:**
> [paste lastfm_stats result]
>
> **RAIL DATA (weekdays only — omit on weekends):**
> [paste rail_departures result, or "Weekend — no commute data" on Sat/Sun]
>
> **RECENT DAILY NOTES (lookback window+):**
> [paste each note with its date header, most recent first, or "No previous daily notes found"]

### 6. Create the note

```markdown
---
date: YYYY-MM-DD
type: daily
---

# DayName, D Month YYYY

<< [[YYYY-MM-DD]] | [[YYYY-MM-DD]] >>

> ☁️ Weather one-liner from step 4b

<!-- If system_alerts returned degraded status, add warnings here -->
> ⚠️ **integration**: issue (only if alerts present, omit block otherwise)

## Pulse

pulse status header from step 4f...

## Food

-

## Coffee

☕ coffee summary from step 4f-bis (omit section if nothing to show)

## Transport (weekdays only — omit on Sat/Sun)

🚂 DART summary from step 4g...

## House

🏠 house lines from step 4g-bis (1-4 lines: laundry/dishwasher, anomalies, consumables, ops)

## Today

- HH:MM  Event title
- ...

## Tasks

### Focus

<!-- 3-5 manual picks, set at lock-in. The only checkboxes in this note. -->

### From the backlog

> Live view of [[Task Backlog]] — these are not copies. Tick an item here and the Tasks plugin marks it done in the backlog itself.

**🔺 Urgent / overdue**
```tasks
not done
path includes Task Backlog.md
(priority is highest) OR (due before tomorrow)
sort by priority
sort by due
```

**⏫ This week**
```tasks
not done
path includes Task Backlog.md
priority is high
sort by priority
```

## Email

- actionable items from lookback window...

## WhatsApp

- actionable items from recent messages...

## Meetings

## Notes
```

### 7. Present the briefing

Show the morning briefing in two parts:

**Part 1 — The day at a glance** (concise, actual items not counts):

- **Morning assessment**: the day shape and any conflict flags from 4i — lead with this
- **Pulse**: the full status header — sleep, body, listening with opinions (show the formatted block, not a summary of it)
- **System alerts**: if any integrations are degraded, show with ⚠️ prefix
- **Weather**: the one-liner
- **Transport**: next DARTs to Dublin (weekdays only)
- **Calendar**: today's events with times

**Part 2 — What needs attention**:

- **Email**: triaged by urgency (needs action today / should respond this week / FYI)
- **WhatsApp**: triaged by urgency (needs response / plans being made / FYI)
- **Stalled tasks**: backlog items drifting 3+ days (from the 4h analysis) — flag, don't re-list the whole backlog
- **Focus suggestions**: the 3-5 candidates with reasoning from 4i

If the user included content in their arguments (e.g. `/daily-note feeling rough today, need to focus on the tax return`), add it to the Notes section and factor it into the morning assessment and Focus suggestions.

### 7.5. Inbox triage (conditional)

Only run if step 4's `inbox_pending` returned `count > 0`. Otherwise skip silently.

Surface every pending item in a compact table — one row per file, ordered oldest-first (so backlog is visible). Don't paraphrase the previews; show them so the user can decide without opening anything.

```
## Inbox (N pending)

#  | Age  | Kind | Source         | Original filename       | Preview / detail
1  | 2h   | pdf  | webhook        | quote-electrical.pdf    | "Quotation for rewiring ground floor… €4,250 inc VAT…" (3 pages)
2  | 4h   | text | ios-shortcut   | claude-chat-export.md   | "Conversation about kitchen extraction options…" (1.2k chars)
3  | 1d   | pdf  | webhook        | ragu-recipe.pdf         | "Ragu Bolognese — 6-8 servings, 30 min prep…" (3 pages)
```

Then ask:

> "Inbox: route each? Reply with one line per item — `1 corpus renovation`, `2 vault Inbox/`, `3 vault Reference/Recipes/`, `4 archive`, `5 dismiss`. Or `all archive` to clear, or `skip` to leave for later."

Decode the user's reply into one tool call per item:
- `corpus [tags…]` → `inbox_to_corpus` with `project_tags=[…]` (default `["work"]` if no tags)
- `vault <target-path>` → `inbox_to_vault` with `target=<path>` (path is vault-logical; e.g. `Inbox/2026-05-22-quote-electrical.pdf` or `Shared/Household/wifi.pdf`). If the user says just `vault` without a path, default to `Inbox/<YYYY-MM-DD>-<original-filename-or-slug>.<ext>`.
- `archive` → `inbox_archive`
- `dismiss` → `inbox_dismiss`
- `preview` → call `inbox_preview` first, show full text, then re-ask

Run the calls **in parallel** (independent file moves). Confirm in one line: `"Inbox: 2 to corpus (renovation), 1 to vault Inbox/, 1 dismissed."`

If any item was routed to corpus and tagged with a topic that matches today's data (renovation, the Mill, etc.), consider re-running `renovation_context` with a query about it — but only if it changes the day's shape.

If the user replied `skip`, leave everything pending; the items will reappear in tomorrow's daily note (and any `/refresh` between).

### 8. Task triage

After the briefing, present a **unified task triage table**. This is the single view of everything on the plate today.

Combine these sources into one numbered list:

1. **Stalled / urgent backlog items** surfaced in step 4h and the [[Task Backlog]] query (urgent, overdue, or stalled 3+ days)
2. **Actionable email items** identified as "needs action today" in the briefing
3. **Actionable WhatsApp items** identified as "needs response" in the briefing
4. **Reminders** that aren't already represented in the backlog (cross-reference by title — don't double-count)
5. **Overdue items** from the backlog (📅 on/before today)

Present as a numbered table:

```
## Task Triage

#  | Source    | Item                                           | Priority | Suggestion
1  | active    | Old bank — close accounts                          | ⏫       | keep
2  | active    | Finn appointment — the clinic                 | ⏫       | keep
3  | email     | Energy supplier meter reading (due in 3 days)    | 🔺       | → add to backlog
4  | email     | Summer camp payment outstanding                   | ⏫       | → add to backlog
5  | whatsapp  | Workshop booking — reply to Sam                   | 🔼       | keep (already in Active)
6  | whatsapp  | Cara — lock in meeting date              | 🔼       | → add to backlog
7  | reminder  | Buy wiper blades                                | 🔽       | done ✓ ?
8  | active    | LinkedIn update + draft post                    | 🔼       | keep
...
```

For each item, suggest an action:
- **keep** — it's already tracked, no change needed
- **→ add to backlog** — new item from email/WhatsApp, should be tracked
- **done ✓ ?** — looks like it might be completed, confirm
- **drop** — no longer relevant
- **→ calendar** — far-out item that needs a calendar entry (Phase 4)

Then ask the user:

> "Quick triage — respond with changes (e.g. 'drop 7, 3 is urgent, add 6 to household backlog, 4 is done') or just say 'looks good' to accept all suggestions."

**After the user responds:**
- For items marked "→ add to backlog": append to the single unified backlog `vault/Task Backlog.md` under the right domain (`# Home`/`# Renovation`/`# Kids`/`# Finance`/`# Admin`), using Obsidian Tasks format. (The personal `Alex/`·`Sam/` backlogs are retired pointer stubs — don't write there.)
- For items marked "done": if there's a corresponding reminder, call `reminders_complete`. Mark the task `[x]` in `Task Backlog.md` (not the daily note).
- For items with priority changes: update the task line in `Task Backlog.md`
- For items to drop: remove from `Task Backlog.md` (or move to [[Someday]] if it's a "not now" rather than "never")

### 8.5. Reminder reconciliation

After the triage edits land, do a full pass to keep Apple Reminders in sync with the source-of-truth backlogs and today's note. Reminders drift fast (voice-dictated noise, partial titles, duplicates of the same call) and the daily note is the only chance to clean them up regularly.

**Inputs (already in context from earlier steps):**
- Open reminders from `reminders_sync` (step 4)
- Today's daily note (just edited) — the Focus list and any items marked `[x]` in this session
- The single unified backlog: `vault/Task Backlog.md` (+ [[Someday]] / [[Delegated Tasks]] if relevant) — `Read` fresh from disk if not already loaded. The `Alex/`·`Sam/` backlogs are empty pointer stubs; ignore them.

**For each open reminder, classify into one of:**

| Bucket | Meaning | Default action |
|---|---|---|
| `done-high-conf` | Maps cleanly to a task marked `[x]` in today's note (>0.85 fuzzy match OR shares a unique noun like a part number, proper noun, or phone number) | **Auto-complete** the reminder |
| `done-low-conf` | Looks done but match is fuzzy | **Propose** complete, ask before applying |
| `merge` | Multiple reminders collapse into the same single task (e.g. three reminders all referring to one phone call) | **Propose** keeping the most recent, completing the rest |
| `aligned` | Has a corresponding open task in the note or a backlog — already tracked | **Leave alone** (don't list each one — show the count) |
| `orphan-add` | No matching task anywhere; title looks actionable | **Propose** adding to a backlog (suggest which one + which `#action-type`) |
| `orphan-keep` | No matching task; not appropriate to surface as a backlog item (e.g. "Wine with neighbours — 19:30" is a calendar/event reminder, not a task) | **Leave alone** |

**Matching guidance:**
- Use proper nouns and unique tokens as strong signals: phone numbers, account numbers, part numbers, supplier names, person names. A reminder "X123AB4 order instead of the 0" matches the Active item "Call Example Electrical … X123AB0 → X123AB4" via the part number alone — that's a confident merge.
- Don't rely on title length alone. "Call UniFi" and "Call the electrical supplier" and "X123AB4 order instead of the 0" can all be merges into a single backlog task because the captured *intent* (one phone call) matches even though the words don't.
- If the daily note says `[x] Take a photo of the bathroom ✅` and a reminder titled "Take a photo of the bathroom" exists open, that's `done-high-conf`. Auto-complete.
- If a reminder says "Get back to revenue about the House evaluation" and the day's email summary or notes say Sam submitted it today, that's `done-high-conf` even without a matching `[x]` in the note. Cross-source completion is fine when the evidence is explicit.
- Reminders without due dates and no matching task are usually `orphan-keep` (long-tail captures the user wants to keep around) — only suggest moving to backlog if the title is concrete and actionable.

**Present the diff** as a single table grouped by bucket (omit `aligned`, just show count):

```
## Reminder Reconciliation

✅ Auto-completing (high confidence — applied):
- "Take a photo of the bathroom"          ← matches `[x]` in note
- "List for Dara including missed walls"  ← matches `[x]` in note

🤔 Propose completing (please confirm):
1. "Call UniFi"                            ← merges into Focus #1 (Example Electrical)
2. "Call the electrical supplier"                  ← merges into Focus #1 (Example Electrical)
3. "X123AB4 order instead of the 0"        ← merges into Focus #1 (Example Electrical)
4. "Get back to revenue about the House evaluation" ← Sam submitted today

📥 Propose adding to backlog:
5. "Talk to Beth about the end of your party" → Alex backlog, #quick

🟢 Aligned with existing tasks: 12 reminders (no action)
🟤 Long-tail keep: 6 reminders (no action)
```

Then ask:

> "Reconciliation: high-conf completes already applied. For the proposals — say 'all good' to apply, or call out exceptions (e.g. 'keep 2', 'skip 5')."

**On user approval:**
- For each `done-low-conf` and `merge` item not vetoed: call `reminders_complete` with the reminder UID
- For each `orphan-add` not vetoed: append to the suggested backlog file using Obsidian Tasks format, then call `reminders_complete` so the reminder is consolidated into the backlog (the user can re-add a reminder later via `/lock-in` if it becomes Focus)
- Confirm with a one-line summary: `"Reconciled: 4 completed, 1 moved to Alex backlog. 18 reminders aligned with backlog/note (no change)."`

**Safety rules:**
- Never auto-complete a reminder unless the evidence is in *today's session* (a `[x]` mark, a confirmed-done email, an explicit "done" in user input). Don't auto-complete based on backlog state alone — backlogs lag and a stale `[x]` from yesterday isn't enough.
- Never delete reminders. Always use complete (which moves them to the iCloud Recently Deleted view, keeping them recoverable).
- If the reminder list is empty or the user says "skip reconciliation", silently move on to step 9.
- This step runs on every `/daily-note` invocation — including refreshes on an existing note. On a refresh, the auto-complete pass is the main value (it catches things ticked off mid-day on the phone vs. completed in the note).

### 9. Lock in (inline)

After triage is complete and changes are synced, offer to lock in Focus right here:

> "Ready to pick your Focus? (3-5 items max) You can pick from the triaged list by number, or name them directly."

When the user responds:

1. Update the daily note's `### Focus` section with the chosen items (max 5, as checkboxes)
1b. **Mirror into the backlog's `#focus` tags** (drives the backlog's "🎯 Today's Focus" query): `Read` `vault/Task Backlog.md`, strip any stale ` #focus` tokens (yesterday's picks), then append ` #focus` to each chosen item's matching backlog task line. For an ad-hoc pick not in the backlog, add it under the right domain in `## 📌 This Week — active` with a priority + ` #focus`. Surgical token edits only — don't restructure the file. Daily-note Focus and backlog `#focus` must match.
2. Fetch current reminders via `reminders_sync`
3. For each Focus item that doesn't have a matching reminder, call `reminders_add` to create one with today's due date
4. For any Focus items that are 2+ weeks out or time-specific, offer to create a calendar event via `calendar_create_event`
5. Confirm: show the final Focus list and any sync actions taken

If the user says "not yet" or "later", tell them: "No problem — run `/lock-in` when you're ready."

### 10. Done

Keep it brief. They want to start working.
