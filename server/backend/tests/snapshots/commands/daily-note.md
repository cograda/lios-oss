<!-- GENERATED from server/backend/app/prompts/templates/daily-note.md.j2 via app.prompts.commands — do not hand-edit. Edit the template and run scripts/render_commands.py (or refetch GET /api/v1/commands). -->

Create or open today's daily note for Alex.

Arguments: $ARGUMENTS

## Steps

### 1. Determine date, user, and lookback window

- Today's date and day of the week
- Default: Alex (`vault/Daily Notes/Alex/YYYY-MM-DD.md`)
- **Lookback window**: don't compute this by hand. `system_daily_brief` returns it as `_meta.lookback_start`, already resolved for today's weekday and the user's `daily_note.lookback_days` preference. For reference, the rule it applies is: Monday reaches back to Friday, the weekend reaches back to Friday, and Tue–Fri use the configured lookback (2 days by default). Use that date for WhatsApp, email, and previous daily notes.

### 2. Check if note exists

- **If today's note already exists, delegate to `/refresh`.** The full creation pipeline (steps 3–8 below) is for first-time-of-day note creation; once the note exists, the right shape is "re-fetch live data and update sections in place + reconcile reminders," which is exactly what `/refresh` does. Pass through any user arguments that look like section filters (`email`, `transport`, etc.) or `--full`. After delegation, stop — do not continue to step 3.
- If the note doesn't exist, continue to step 3 (full creation flow).

### 3. Gather data

All data comes through MCP tools served by the local comar client (which proxies to the server as needed). No direct server access or shell commands required.

### 4. Fetch data — three calls, not twenty-four

Make all three **in a single parallel tool block**:

1. **`system_daily_brief`** with `{"since": "<mtime of last daily note, ISO>"}` — this is the big one. It returns every read-only source the note needs in a single round-trip: system alerts, today's calendar, weather now + forecast, reminders (open *plus* what changed since `since`), transport departures, home status and appliance history, unread + recent email, recent WhatsApp, pending attachments, health summary/sleep/trends/workouts, recent listening + weekly stats, and current coffee + recent brews.
   - Omit `since` if there's no previous daily note (defaults to 24h).
   - **Do not** call `calendar_today`, `weather_current`, `gmail_recent`, `health_sleep` etc. separately. They are all inside this payload. Calling them individually costs a round-trip each and is what this tool exists to replace.
   - **The result will be spooled to a file, not returned inline.** The unfiltered brief is routinely 100–150k chars — over the inline tool-output ceiling — so the harness writes it to a file and hands back the path. This is the expected outcome every time, not an error and not worth remarking on. Do **not** read the file linearly; pull only the keys you need with `jq` (each source sits under its own top-level key): `jq '._meta' <file>` first, then e.g. `jq '.calendar, .weather_current, .weather_forecast' <file>`, `jq '.reminders' <file>`. Several keys can go in one `jq` call.
2. **`inbox_pending`** — files dropped via the Tines/iOS-Shortcut webhook (PDFs, text, recipes, screenshots) awaiting a routing decision. Not part of the brief on purpose: it feeds the *interactive* triage step 7.5, not the read-only snapshot. It inline-enriches unprocessed sidecars on read, so it's always fresh. If `count > 0`, step 7.5 fires.
3. **`snag_capture`** — scans WhatsApp for `Snag - room - [element -] [trade -] detail` shaped messages and registers any not already captured, re-rendering the vault note and the shared Google Sheet. Separate from the brief because it **writes**; the brief is read-only. No arguments needed — idempotent against its own prior captures (default `since_days: 7`). Surface `snags_created` if > 0; skip the section if 0.
   - **Caveat**: idempotency is tracked via `snag_source_messages`, which only `snag_capture` writes to. A snag entered manually via `snag_add` leaves the underlying WhatsApp message unmarked, so a later run can re-surface it as apparently new. Don't assume every `snags_created` result is genuinely new — glance at the titles against recent manual adds.

#### Reading the brief

Each source lands under its own key: `alerts`, `calendar`, `weather_current`, `weather_forecast`, `reminders`, `rail`, `home`, `appliance:<entity_id>` (one per configured appliance), `mail_unread`, `mail_recent`, `whatsapp`, `attachments`, `health_summary`, `health_sleep`, `health_trends`, `health_workouts`, `lastfm_recent`, `lastfm_stats`, `coffee_current`, `coffee_recent_brews`.

`alerts` carries more than the degraded list: `alerts.data_freshness` is one entry per staleness probe — `integration`, `latest` (ISO, or `null` for an empty table), `age`, `threshold`, and `user_id` on per-owner probes — and it is present **whether or not** anything is stale. Read freshness from that list and nowhere else: never add a row for a source that isn't in it (an integration with no probe is not "fresh", it's unmeasured), and never compute an age yourself from another key's timestamps.

A source that failed returns `{"error": "..."}` under its own key. Render `⚠️ [source] unavailable` for it and carry on — never silently omit a section. One dead source does not invalidate the rest of the payload.

`_meta` carries everything you need to interpret the rest:

| Field | Use it for |
|---|---|
| `preferences` | **All personalisation.** See below. |
| `lookback_start` | The window comms and previous notes should cover — already computed for today's weekday, don't recompute it |
| `is_weekend` | Whether the Transport section applies |
| `sections_enabled` | Which sections this user wants, in render order |
| `sources_skipped_no_data` | Sources skipped because this user has no such data — **omit those sections entirely, don't render them empty** |
| `sources_from_cache` | Which sources came from the pre-warm cache rather than live |

#### Personalisation — read it from `_meta.preferences`, never assume

These are per-user and change without this command being re-rendered, so read them at runtime:

| Preference | Effect |
|---|---|
| `daily_note.sections` | Which sections to render, in order |
| `daily_note.focus_count` | How many Focus items to suggest and lock in |
| `daily_note.tone` | Voice for the opinion lines — e.g. `direct`, `gentle`, `terse` |
| `health.profile_path` | Vault-relative path to the health profile note. **Read this file** for goals, injury context and programming. Empty → skip the exercise coaching line entirely |
| `health.strength_target` | Weekly strength-session target. **0 means no target** — report activity without grading it, and never invent a number |
| `house.appliance_entities` | Which appliances the House section reports on. Empty → no laundry/dishwasher lines at all |
| `rail.direction` | Direction filter already applied to the `rail` payload |

If a preference is absent, fall back to its default rather than to anything you remember from a previous note.

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
> **4z-bis. Data freshness** — build a table from `alerts.data_freshness` (present on every run, including when `status` is `all_ok`):
>
> ```
> | Source | Latest | Age | Threshold | |
> |---|---|---|---|---|
> | whatsapp | 08:12 | 47m | 6h | ✅ |
> | lastfm | 17 Aug 15:26 | 3d 1h | 12h | ⚠️ |
> | apple_health | — | never | 24h | ⚠️ |
> ```
>
> - One row per entry, **stale rows first** (age over threshold), then the rest by age descending — the table is read top-down and the thing that needs attention should not be row nine.
> - `Latest`: `HH:MM` if it's today, `D Mon HH:MM` otherwise, `—` when `latest` is `null`.
> - `Age`: the payload's `age` string verbatim; `never` when `age` is `null`. Don't reformat or round it — the server already chose the unit.
> - Last column: `⚠️` when age exceeds threshold or there are no records at all, `✅` otherwise. No third state — "unmeasured" sources are absent from the list, not shown as unknown.
> - If a row is a per-owner probe (`user_id` present), it is *this user's* own row — the payload is already scoped to the caller — so don't add an owner column and don't imply it covers the household.
> - Only the rows are yours to compute. If `data_freshness` is missing or empty, write `_No freshness probes reported._` under the heading rather than inventing rows or dropping the section.
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
> Build a single cohesive status block. Each sub-section has two lines: line 1 is **quantitative** (numbers, dense, scannable), line 2 is a **qualitative** *italic* opinion.
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
> [Yesterday's workout if any: type, duration, avg HR.]
> This week: X/N strength sessions, where N is `health.strength_target`. [Next best slot from calendar if the target isn't met.]
> *Coach-like guidance using the health profile for injury context and programming. Examples: "Good upper body session yesterday. One more this week — Thursday evening looks clear." or "No sessions yet and it's Thursday. Even 30 minutes counts."*
>
>
> **🎵 Listening**
> XX scrobbles this week. Heavy rotation: Artist (X plays), Artist (X plays). Top genres: genre, genre.
> *Mood/vibe inference — what the music says about headspace. Note any shifts.*
>
> Rules:
> - **Sleep & Vitals**: Use health_sleep for stage breakdown and session times. Use health_summary for resting HR and HRV. Use health_trends for 7-day averages of sleep, resting HR, and HRV to assess direction.
> - If sleep data is missing or shows a sync gap (e.g. 0h or no sessions), say "Sleep data not syncing — check Watch connection."
> - **Movement**: Use health_summary for today's steps/distance/energy and yesterday's comparison. Use health_trends for 7-day step averages to say whether activity is up or down. Do NOT include HR/HRV here (those are in Vitals).
> - **Exercise**: Use health_workouts for recent workout details. Use the Health Profile (provided below) for injury context, exercise programming, and progression criteria. Cross-reference with today's calendar to suggest workout slots.
> - **Listening**: Use lastfm_stats for counts and top artists/genres. Use lastfm_recent for recent rotation and to detect shifts. If no lastfm data, omit the Listening sub-section entirely — Pulse works without it.
> - If no workout data, note the gap and suggest a session.
> - Keep the whole block to **8-12 lines**. Dense, not verbose. The Exercise section earns extra space for coaching.
> - The opinion lines should be direct and coach-like: "Bad night, go easy" or "Solid recovery, you're good."
> **4f-bis. Coffee** — short, omit if nothing to show:
> - If `coffee_current` returned 0 coffees AND `coffee_recent_brews` returned 0, omit the Coffee section entirely.
> - Otherwise format as: line 1 lists the current bag(s), line 2 the most recent brew result, line 3 (optional) a one-line nudge.
> - Format example:
>   ```
>   ☕ Drinking: **Colombia Vianí** (Cloud Picker, washed, light) · **Los Chorros Pink Bourbon** (3fe, filter)
>   Last brew: V60, 15→250g, 3:30, overall 4/5 — "balanced, slight tang"
>   *Last 3 espresso brews trended sour — try a finer grind today.*
>   ```
> - Trend opinion only fires if there are 3+ brews of the same coffee in the last 7 days AND a clear pattern (avg `acidity` ≥ 4 = sour/under, `bitterness` ≥ 4 = over, declining `overall`). Otherwise omit the italic line.
> - Cap at 3 lines total.
>
> **4g. Transport** — from the `rail` key. Omit the section entirely when `_meta.is_weekend` is true, or when `rail` is absent from the payload (no station configured).
> - Show the next 3-5 departures with scheduled and expected times. The direction filter in `rail.direction` has already been applied server-side — don't re-filter.
> - Flag any delays (expected ≠ scheduled)
> - Keep to 2-3 lines
> - Format: `🚂 Next trains: 10:04 → Destination, 10:34 → Destination, 11:03 → Destination`
> - If a train is delayed: `10:34 → Destination (exp 10:38, +4 min)`
> - If the payload carries a `message` rather than departures, the integration is unconfigured — show that message once, don't retry
> - If `rail` holds an `error`, show "⚠️ Rail data unavailable"
>
> **4g-bis. House** — home status from the `home` key plus one `appliance:<entity_id>` key per configured appliance. One always-on ops line plus up to 3 insight lines that only fire when there's something to say. Cap at 4 lines total.
> - **Appliances**: only for the entities present as `appliance:*` keys — if `house.appliance_entities` is empty there are none, and this line is simply absent. Never guess an entity id. Cross-reference the history transitions (last ~36h) with current state. If a cycle finished since the last daily note and the machine is now stopped, flag the hanging job: `🧺 Washer finished 21:36 last night — needs emptying?` If one is running now, show when it's done (completion_time / program_finish_time): `🧺 Dryer running — done 10:45.` Nothing ran, nothing running → no line.
> - **Anomalies**: media players still playing this morning, lights/switches on (the curated `lights_switches` section — it excludes config toggles), sprinkler valve currently open, dishwasher door open overnight. One combined line, only if any fire.
> - **Consumables**: any `*_nearly_empty` binary sensor that's `on` → one line, and suggest a `#quick` task (e.g. dishwasher salt).
> - **Ops line (always last)**: `🏠 N entities · M offline (Δ vs yesterday) · batteries OK · live`. Compute the offline delta by parsing the previous daily note's House ops line (previous notes are provided); first run or unparsable → show the count with "(baseline)". If the data has `stale: true`, lead the whole section with `⚠️ HA data stale — event stream may be down.` NEVER enumerate the standing offline fleet (ESPHome boards pending reflash) — the delta is the signal, not the list.
>
> **4g-ter. Snags** — omit entirely if `snags_created` is 0.
> - If > 0, one line per newly captured snag: `SNAG-0163 · Outdoor · Wrong thermostats installed throughout (electrician)`
> - Close with the shared Sheet link if `sheet_url` is present: `📋 Sheet: <url>`
> - Keep it short — this is a "here's what landed" note, not analysis. Fold anything noteworthy (e.g. a snag that looks urgent/safety-related) into the Morning assessment's Conflict flags instead of expanding this section.
>
> **4h. Stalled-task analysis (read-only — do NOT copy tasks into the note)** — scan ALL provided daily notes (covering the lookback window and beyond) to *inform the Morning assessment and Focus suggestions only*. The daily note no longer carries an Active checkbox list — open tasks live in the single [[Task Backlog]] and are surfaced via a query (see step 6). Your job here is to spot what's drifting:
> - Tasks that were a previous day's Focus but still aren't marked done → flag as carried/stalled
> - For anything that's been hanging around 3+ days, note "(stalled Xd)" so it can be called out in the assessment
> - Uncompleted meeting action items → flag for Focus consideration
> - Produce a short list of stalled/at-risk items for steps 4i (assessment + Focus suggestions). Do NOT emit these as `- [ ]` checkboxes; they already exist in the backlog.
>
> **4h-bis. Renovation prior context** — conditional. **The family moved in during June 2026.** The house is *Comar*; "Riverside" survives only in build-era records. The active phase is **move-in + snagging**, tracked on [[Comar Project Board]] grouped by driver — so trigger on snagging-era vocabulary, not build procurement.
>
> Only run if today's calendar, emails, WhatsApp, OR carried tasks mention any of:
> - **Snags and trades** — snag, SNAG-####, snag list, snagger, Kev, Phil, Paddy, Matt Wells, plumber, electrician, painter, joinery
> - **House systems still being finished** — underfloor heating, manifold, heat miser, thermostats, Wi-Fi access point, server rack, blinds, flooring, expansion gaps
> - **The contractual tail** — Cameron, David Moran, Neil McGroary, variation, solicitor. The architect/contractor dispute outlived the build. **Cameron left the project in February 2026 and is now a case file, not a participant** — treat a Cameron mention as legal/record context, never as live design input.
>
> ⚠️ **Retired as triggers**, because these events stopped when the build did: BoQ, bill of quantities, recommendation for payment, PC sum, QS, site meeting, drawings, M&E. Their *content* is still in the corpus and still worth retrieving — but reached via a live snagging topic, never on its own. A trigger list that only matches a finished phase is dead code that reads as working.
>
> If yes, the main model (not the Haiku subagent) calls `corpus_search` once with a query derived from the matching topic (e.g. "shower niche dimensions", "heat miser thermostat locations", "utility room joinery scope"). Pass `{"limit": 5}`. The purpose is to surface prior decisions and rates the assistant should already know before producing the briefing — the corpus still holds the build-era detail, so a snagging question often has its answer in a BoQ line or a site-meeting note.
>
> Fold the top 1-3 hits into the Morning assessment's Conflict flags or Focus suggestions as `**Prior:** <one-line summary> — [[source filename]] (YYYY-MM-DD)`. Do NOT dump the corpus hits verbatim.
>
> If no trigger keywords match today's data, skip this step silently — it should never add noise on a normal day.
>
> **4i. Morning assessment** — cross-reference EVERYTHING and give an honest assessment:
>
> **Day shape**: Look at the calendar, the task list, and the Pulse data. Reference it directly — "You slept badly" not "The health data shows..." The Pulse section gives the raw data; your job is to weave it into the day's story. Is this going to be a heavy day or a light one? Say it plainly:
> - "Packed day — 3 meetings and a deadline. Protect your focus time."
> - "Light calendar. Good day to tackle the backlog."
>
> **Conflict flags**: Surface anything that clashes or needs attention:
> - Overdue items that keep getting carried = call it out
> - Time-sensitive items that could get missed (e.g. tickets on sale at 10am, drop-off at same time)
> - Unanswered messages where someone is waiting
> - Exercise target at risk (e.g. "0/2 strength sessions and it's Friday — last chance this week")
> - Bad sleep + packed calendar = warning
> - Bad recovery markers (low HRV + poor sleep) + planned workout = suggest going light or resting
>
> **Focus suggestions**: Based on everything — due dates, overdue items, stalled tasks, incoming email/WhatsApp, calendar shape, energy level — suggest up to `daily_note.focus_count` Focus candidates (from `_meta.preferences`) with one-line reasoning for each:
> - "**HSBC close accounts** — quick phone call, clears mental overhead and has been sitting for days"
> - "**Finn / Cara Clinic** — carried from yesterday, quick call"
> - "**SSE meter reading** — 3-day deadline, Sam forwarded it"
> - "**Photo print for Sue** — needs Amazon order today if it's arriving for the weekend"
> - "**Message Paddy** — meaty message but gets the renovation moving"
>
> These are suggestions, not decisions — the user picks their own Focus. But give real reasoning, not just a list.
>
> Return all sections clearly labelled with the actual data.
>
> ---
> **DAILY BRIEF (all sources + `_meta`):**
> [the brief was spooled to a file — give the subagent the **file path** and tell it to read the file itself (it has Read/Bash, so `jq` per key or a chunked Read both work); do NOT paste 130k chars into its prompt. It already contains alerts, calendar, weather, reminders, rail, home + appliances, mail, WhatsApp, attachments, health, listening and coffee, each under its own key, plus `_meta.preferences` which governs every personalisation decision above]
>
> Notes on individual keys the subagent should apply:
> - `attachments` — surface under the WhatsApp section as "📎 New documents waiting to ingest"; mention filename + sender + age. WhatsApp's CDN purges sender bytes after ~30 days, so flag anything older than 14 days as urgent.
> - `rail` — absent entirely at the weekend or when unconfigured; say nothing rather than explaining its absence.
> - any key holding `{"error": ...}` — render "⚠️ [source] unavailable" and move on.
> - keys named in `_meta.sources_skipped_no_data` — the user doesn't use that integration. Omit the section silently; do not say "no data".
>
> **HEALTH PROFILE:**
> [paste the contents of the file at `_meta.preferences["health.profile_path"]`, or "No health profile configured" if that preference is empty or the file is missing]
>
> **SNAG CAPTURE RESULT:**
> [paste snag_capture result — snags_created, messages_seen, not_snag_shaped — plus sheet_url from the response if present]
>
> **RECENT DAILY NOTES (lookback window+):**
> [paste each note with its date header, most recent first, or "No previous daily notes found"]

### 6. Create the note

The skeleton below is already scoped to this user's sections — sections they
don't use are absent from it rather than listed-and-conditional. Still drop any
whose sources appear in `_meta.sources_skipped_no_data`, and honour the order in
`_meta.sections_enabled`. A section with no data should be absent, not
present-and-empty.

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

## Snags (omit section entirely if snags_created is 0)

snag capture summary from step 4g-ter...

## Today

- HH:MM  Event title
- ...

## Tasks

### Focus

<!-- Manual picks, set at lock-in, capped at `daily_note.focus_count`. The only checkboxes in this note. -->

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

## Data freshness

table from step 4z-bis...
```

### 7. Present the briefing

Show the morning briefing in two parts:

**Part 1 — The day at a glance** (concise, actual items not counts):

- **Morning assessment**: the day shape and any conflict flags from 4i — lead with this
- **Pulse**: the full status header (show the formatted block, not a summary of it)
- **System alerts**: if any integrations are degraded, show with ⚠️ prefix
- **Data freshness**: name only the sources whose row is `⚠️`, one line — the full table lives in the note, and reading twelve healthy rows aloud is not a briefing. All green → say nothing here
- **Weather**: the one-liner
- **Transport**: next DARTs to Dublin (weekdays only)
- **Calendar**: today's events with times

**Part 2 — What needs attention**:

- **Snags**: if any were auto-captured from WhatsApp overnight/today (step 4g-ter), mention the count + sheet link — this is new information the user hasn't seen yet, unlike Email/WhatsApp which they've likely skimmed
- **Email**: triaged by urgency (needs action today / should respond this week / FYI)
- **WhatsApp**: triaged by urgency (needs response / plans being made / FYI)
- **Stalled tasks**: backlog items drifting 3+ days (from the 4h analysis) — flag, don't re-list the whole backlog
- **Focus suggestions**: the candidates with reasoning from 4i

If the user included content in their arguments (e.g. `/daily-note feeling rough today, need to focus on the tax return`), add it to the Notes section and factor it into the morning assessment and Focus suggestions.

### 7.5. Inbox triage (conditional)

Only run if step 4's `inbox_pending` returned `count > 0`. Otherwise skip silently.

Surface every pending item in a compact table — one row per file, ordered oldest-first (so backlog is visible). Don't paraphrase the previews; show them so the user can decide without opening anything.

```
## Inbox (N pending)

#  | Age  | Kind | Source         | Original filename       | Preview / detail
1  | 2h   | pdf  | tines          | quote-electrical.pdf    | "Quotation for rewiring ground floor… €4,250 inc VAT…" (3 pages)
2  | 4h   | text | ios-shortcut   | claude-chat-export.md   | "Conversation about kitchen extraction options…" (1.2k chars)
3  | 1d   | pdf  | tines          | ragu-recipe.pdf         | "Ragu Bolognese — 6-8 servings, 30 min prep…" (3 pages)
```

Then ask:

> "Inbox: route each? Reply with one line per item — `1 corpus renovation`, `2 vault Inbox/`, `3 vault Reference/Recipes/`, `4 archive`, `5 dismiss`. Or `all archive` to clear, or `skip` to leave for later."

Decode the user's reply into one tool call per item:
- `corpus [tags…]` → `inbox_to_corpus` with `project_tags=[…]` (default `["tines"]` if no tags)
- `vault <target-path>` → `inbox_to_vault` with `target=<path>` (path is vault-logical; e.g. `Inbox/2026-05-22-quote-electrical.pdf` or `Shared/Household/wifi.pdf`). If the user says just `vault` without a path, default to `Inbox/<YYYY-MM-DD>-<original-filename-or-slug>.<ext>`.
- `archive` → `inbox_archive`
- `dismiss` → `inbox_dismiss`
- `preview` → call `inbox_preview` first, show full text, then re-ask

Run the calls **in parallel** (independent file moves). Confirm in one line: `"Inbox: 2 to corpus (renovation), 1 to vault Inbox/, 1 dismissed."`

If any item was routed to corpus and tagged with a topic that matches today's data (renovation, riverside, etc.), consider re-running `corpus_search` with a query about it — but only if it changes the day's shape.

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
1  | active    | HSBC — close accounts                          | ⏫       | keep
2  | active    | Finn appointment — Cara Clinic                 | ⏫       | keep
3  | email     | SSE Airtricity meter reading (due in 3 days)    | 🔺       | → add to backlog
4  | email     | TOPS camp payment outstanding                   | ⏫       | → add to backlog
5  | whatsapp  | Kaleidoscope — reply to Sam                   | 🔼       | keep (already in Active)
6  | whatsapp  | Stef Murray — lock in meeting date              | 🔼       | → add to backlog
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
**The backlog is a ledger, not a file.** `Task Backlog.md` is a rendered view of comar's task ledger; a hand edit to it locks every task write until someone forces a render. Every change below goes through the `tasks_*` tools, which re-render the file themselves:
- For items marked "→ add to backlog": `tasks_add(title=..., project=..., priority=...)`. Find the project with `tasks_structure` if unsure; a task with no project is fine until it has company.
- For items marked "done": `tasks_complete(uid=...)` (find the uid with `tasks_query(text=...)`). If there's a corresponding reminder, call `reminders_complete` too.
- For items with priority changes: `tasks_update(uid=..., priority=...)`.
- For items to drop: `tasks_update(uid=..., status="dropped")`, or `status="someday"` if it's a "not now" rather than "never". Never delete.

### 8.5. Reminder reconciliation

After the triage edits land, do a full pass to keep Apple Reminders in sync with the source-of-truth backlogs and today's note. Reminders drift fast (voice-dictated noise, partial titles, duplicates of the same call) and the daily note is the only chance to clean them up regularly.

**Inputs (already in context from earlier steps):**
- Open reminders from `reminders_sync` (step 4)
- Today's daily note (just edited) — the Focus list and any items marked `[x]` in this session
- The task ledger: `tasks_query(limit=300)` for open tasks (+ [[Delegated Tasks]] if relevant). Do not read `Task Backlog.md` for this — it is the rendered view, and the query is the truth.

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
- Use proper nouns and unique tokens as strong signals: phone numbers, account numbers, part numbers, supplier names, person names. A reminder "Z861KR1 order instead of the 0" matches the Active item "Call Armagh Electrical … Z861KR0 → Z861KR1" via the part number alone — that's a confident merge.
- Don't rely on title length alone. "Call UniFi" and "Call Emma Electrical" and "Z861KR1 order instead of the 0" can all be merges into a single backlog task because the captured *intent* (one phone call) matches even though the words don't.
- If the daily note says `[x] Take a photo of the bathroom ✅` and a reminder titled "Take a photo of the bathroom" exists open, that's `done-high-conf`. Auto-complete.
- If a reminder says "Get back to revenue about the House evaluation" and the day's email summary or notes say Sam submitted it today, that's `done-high-conf` even without a matching `[x]` in the note. Cross-source completion is fine when the evidence is explicit.
- Reminders without due dates and no matching task are usually `orphan-keep` (long-tail captures the user wants to keep around) — only suggest moving to backlog if the title is concrete and actionable.

**Present the diff** as a single table grouped by bucket (omit `aligned`, just show count):

```
## Reminder Reconciliation

✅ Auto-completing (high confidence — applied):
- "Take a photo of the bathroom"          ← matches `[x]` in note
- "List for David including missed walls"  ← matches `[x]` in note

🤔 Propose completing (please confirm):
1. "Call UniFi"                            ← merges into Focus #1 (Armagh Electrical)
2. "Call Emma Electrical"                  ← merges into Focus #1 (Armagh Electrical)
3. "Z861KR1 order instead of the 0"        ← merges into Focus #1 (Armagh Electrical)
4. "Get back to revenue about the House evaluation" ← Sam submitted today

📥 Propose adding to backlog:
5. "Talk to Amy about the end of your party" → Alex backlog, #quick

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

> "Ready to pick your Focus? (up to `daily_note.focus_count` items) You can pick from the triaged list by number, or name them directly."

When the user responds:

1. Update the daily note's `### Focus` section with the chosen items (capped at `daily_note.focus_count`, as checkboxes)
1b. **Mirror into the ledger's focus queue** (drives the backlog's "🎯 Today's Focus" section and the tasks app's Focus lens): `tasks_query(queue="focus")` for yesterday's picks and `tasks_update(uid=..., queue="week")` on any not chosen today (or `queue=null` if it is not this week's work either); then `tasks_update(uid=..., queue="focus")` for each chosen item. For an ad-hoc pick not in the ledger, `tasks_add(title=..., queue="focus", priority=...)`. Never edit `Task Backlog.md` — the tools re-render it. Daily-note Focus and the focus queue must match.
2. Fetch current reminders via `reminders_sync`
3. For each Focus item that doesn't have a matching reminder, call `reminders_add` to create one with today's due date
4. For any Focus items that are 2+ weeks out or time-specific, offer to create a calendar event via `calendar_create_event`
5. Confirm: show the final Focus list and any sync actions taken

If the user says "not yet" or "later", tell them: "No problem — run `/lock-in` when you're ready."

### 10. Done

Keep it brief. They want to start working.

Then one line, no more: if today is Monday, or `tasks_review()` flagged more than a handful, or the backlog has not been tuned in a week, suggest `/tunetasks` — it audits the ledger against the rules (containers, duplicates, what blocks what, placement) and hands the tuned result to `/youdoit`. Suggest; do not run it.
