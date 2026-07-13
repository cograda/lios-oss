Re-fetch live data and update today's daily note in place. Use this any time the note is stale — mid-morning after meetings, after lunch, before the commute, or when you said something to Claude that referenced data that turns out to be old.

Arguments: $ARGUMENTS

Argument forms:
- No args: refresh all sections — calendar, weather, transport, house, reminders, email, whatsapp, attachments, inbox, health, listening, coffee. Then run reminder reconciliation (step 8.5 from `/daily-note`).
- `email`, `whatsapp`, `calendar`, `transport`, `house`, `health`, `coffee`, `attachments`, `inbox`, `weather`, `listening`, `reminders` — refresh only that section. Multiple filters can combine: `/refresh email transport`.
- `--full`: rebuild the whole briefing including the Pulse opinion lines and the Morning assessment block. Slower (Haiku subagent runs again), only use if the day's shape has fundamentally changed (a session got cancelled, recovery markers shifted hard, a major new email landed).
- `sam`: operate on Sam's daily note instead of Alex's.

If today's note doesn't exist yet, this command bails out with a one-liner: *"No daily note for today. Run `/daily-note` first."* — that's deliberate. `/refresh` is a *update* command; creation goes through `/daily-note`.

## Steps

### 1. Locate the note

`vault/Daily Notes/<user>/<today>.md`. If absent → bail.

### 2. Resolve which sections to refresh

If filters in args, only fetch those. Otherwise fetch all. Map filter → MCP tool calls:

| Filter | Tools |
|---|---|
| `calendar` | `calendar_today` |
| `weather` | `weather_current`, `weather_forecast {days: 2}` |
| `transport` | `rail_departures {direction: "Southbound", limit: 5}` (skip on Sat/Sun) |
| `reminders` | `reminders_sync {since: <previous note mtime ISO>}` |
| `email` | `gmail_unread {limit: 10}`, `gmail_recent {limit: 50}` |
| `whatsapp` | `whatsapp_recent {limit: 50}` |
| `attachments` | `attachments_pending {since_days: 7, limit: 10}` |
| `inbox` | `inbox_pending` (always cheap — inline-enriches on read) |
| `health` | `health_summary`, `health_sleep`, `health_trends {days: 7}`, `health_workouts {days: 7}` |
| `listening` | `lastfm_recent {limit: 15}`, `lastfm_stats {period: "this_week"}` |
| `coffee` | `coffee_current`, `coffee_recent_brews {limit: 7}` |
| `house` | `ha_home_status`, plus `ha_history {days: 2}` for washer/dryer/dishwasher state entities (see `/daily-note` step 4) — rebuild the House section per `/daily-note` step 4g-bis, keeping the ops line's offline-delta baseline from the existing note |
| `system` | `system_alerts` (always called; cheap; surfaces sync gaps) |

Always include `system_alerts` — if any integration is degraded, you need to know the data is stale before trusting the refresh.

Always call all selected tools **in parallel** in a single tool-call block.

### 3. Edit the note in place

For each refreshed section, locate the heading in the note (`## Today`, `## Email`, `## WhatsApp`, etc.) and replace its body with the new content. Keep the user's hand-edits where possible — if the section has obvious user notes (e.g. handwritten lines under a heading without bullet markers), preserve them and append the refreshed data below.

Mark the refreshed sections with a small `*(refreshed HH:MM)*` italics tag at the heading. Don't proliferate these — replace the previous tag if one exists.

If a section's data hasn't materially changed (e.g. same calendar events, same DARTs), still update the timestamp but don't rewrite the body.

For the Email and WhatsApp sections specifically: re-triage *since the previous note's last refresh timestamp*, not from scratch. Use the existing triage as a baseline; only surface new items, completed items, and items whose status changed. The goal is "delta", not "rebuild".

### 3.5. Inbox triage (conditional)

Only fires if `inbox_pending` returned `count > 0` and the user did *not* pass section filters (or did pass `inbox` explicitly). Mid-day refreshes are exactly when webhook-dropped PDFs and quick-share notes accumulate, so this is where they get caught.

Pattern is identical to step 7.5 in `/daily-note`: present a numbered table with age/kind/source/original-filename/preview, ask for one-line-per-item routing decisions, then call `inbox_to_corpus` / `inbox_to_vault` / `inbox_archive` / `inbox_dismiss` in parallel.

If the user wants to defer, say `skip` and the items stay pending — they'll resurface on the next refresh or tomorrow's daily note.

### 4. Run reminder reconciliation (step 8.5)

Always run reconciliation after a refresh (unless the user passed `reminders` as a filter, in which case only do reminder reconciliation and skip the rest). Pattern is identical to `/daily-note` step 8.5 / `/reconcile-reminders`. High-confidence completes apply automatically; propose the rest.

### 5. Update the Notes section delta

At the bottom of `## Notes`, append a one-paragraph delta of what changed since the previous refresh: `**HH:MM refresh delta:** Revenue submitted (Sam), Finn choir paid, the QS final invoice landed, photo shoot 13 Jun confirmed paid.` — mirrors the manual delta we wrote on 8 May.

If the user passed `--full`, regenerate the Pulse opinion lines + Morning assessment block via Haiku.

### 6. Confirm

Short summary line: `"Refreshed: calendar, transport, email, whatsapp, attachments, health, listening, coffee. 4 reminders auto-completed, 2 proposed for confirmation. New since last check: <delta>."`

## Why a separate skill

`/daily-note` step 2 (when the note exists) delegates here. Splitting it out means:
- `/daily-note` stays creation-shaped — heavy data fetch + Haiku briefing + write-from-scratch.
- `/refresh` stays update-shaped — re-fetch + diff + section edits + reconciliation.
- Filter args (`/refresh transport` before commute) are fast and intent-clear.
- Both share step 8.5 reconciliation logic, so reminder hygiene runs frequently throughout the day.

## Safety rules

- Never overwrite handwritten user notes inside a section. If unsure, append rather than replace.
- Never silently change the Focus list — that's a `/lock-in` action, not a refresh.
- Never edit sections the user didn't ask for when filters are passed.
- If a filter is unrecognised, list valid filters and bail.
