Re-fetch live data and update today's daily note in place. Use this any time the note is stale — mid-morning after meetings, after lunch, before the commute, or when you said something to Claude that referenced data that turns out to be old.

Arguments: $ARGUMENTS

Argument forms:
- No args: refresh all sections — calendar, weather, transport, house, reminders, email, whatsapp, attachments, inbox, health, listening, coffee, snags. Then run reminder reconciliation (step 8.5 from `/daily-note`).
- `email`, `whatsapp`, `calendar`, `transport`, `house`, `health`, `coffee`, `attachments`, `inbox`, `weather`, `listening`, `reminders`, `snags` — refresh only that section. Multiple filters can combine: `/refresh email transport`.
- `--full`: rebuild the whole briefing including the Pulse opinion lines and the Morning assessment block. Slower (Haiku subagent runs again), only use if the day's shape has fundamentally changed (a session got cancelled, recovery markers shifted hard, a major new email landed).
- `sam`: operate on Sam's daily note instead of Alex's.

If today's note doesn't exist yet, this command bails out with a one-liner: *"No daily note for today. Run `/daily-note` first."* — that's deliberate. `/refresh` is a *update* command; creation goes through `/daily-note`.

## Steps

### 1. Locate the note

`vault/Daily Notes/<user>/<today>.md`. If absent → bail.

### 2. Resolve which sections to refresh

**One call does almost all of it.** `system_daily_brief` takes a `sections` argument, so a filtered refresh fetches only the sources feeding those sections rather than everything:

```
system_daily_brief {"sections": ["email", "transport"], "since": "<previous note mtime ISO>", "refresh": true}
```

Pass `refresh: true` — a refresh is explicitly asking for current data, so the brief's cache should be bypassed. (The volatile sources — transport, house, calendar, reminders — are never cached anyway.)

A filtered call usually fits inline, but an unfiltered / `--full` brief is routinely 100–150k chars and **will be spooled to a file** rather than returned inline — that's the expected outcome, not an error. Don't read the spool file linearly; pull only the keys you need with `jq` (each source sits under its own top-level key, `_meta` first).

Map the user's filters onto section names:

| Filter | `sections` value | Notes |
|---|---|---|
| `calendar` | `today` | |
| `weather` | *(none)* | weather is always in the payload, it has no section gate |
| `transport` | `transport` | absent at the weekend or if unconfigured |
| `reminders` | `tasks` | |
| `email` | `email` | |
| `whatsapp` | `whatsapp` | also carries pending attachments |
| `attachments` | `whatsapp` | same section |
| `health` | `pulse` | |
| `listening` | `pulse` | same section |
| `coffee` | `coffee` | |
| `house` | `house` | rebuild per `/daily-note` step 4g-bis, keeping the ops line's offline-delta baseline from the existing note |

System alerts are returned on **every** call regardless of filter — if an integration is degraded you need to know the data is stale before trusting the refresh.

That includes `alerts.data_freshness`, so **if the note has a `## Data freshness` table, rewrite it on every refresh** — whatever the filters, and even when nothing else changed. A freshness table stamped 07:40 sitting under a section refreshed at 14:20 is worse than no table: it reports ages that are hours out of date as if they were current. Same shape and ordering as `/daily-note` step 4z-bis. If the note has no such heading, don't add one — the user has it switched off.

Two things sit outside the brief and are called alongside it, in the same parallel block:

- `inbox_pending` — for the `inbox` filter (and unfiltered runs). Cheap; inline-enriches on read.
- `snag_capture` — for the `snags` filter (and unfiltered runs). It **writes**, which is why it isn't in the read-only brief. This is the main reason to run `/refresh snags` mid-day: WhatsApp `Snag - room - detail` messages logged after the morning note won't appear anywhere else until this runs. Rebuild the `## Snags` heading per `/daily-note` step 4g-ter (omit if `snags_created` is 0 — this refresh, not cumulative for the day).

Everything personalisable — appliance entities, rail direction, focus count, health targets — comes back in `_meta.preferences`. Read it there rather than assuming; see `/daily-note` step 4.

### 3. Edit the note in place

For each refreshed section, locate the heading in the note (`## Today`, `## Email`, `## WhatsApp`, etc.) and replace its body with the new content. Keep the user's hand-edits where possible — if the section has obvious user notes (e.g. handwritten lines under a heading without bullet markers), preserve them and append the refreshed data below.

`## Snags` is a conditional heading (per `/daily-note` step 6, omitted from the note entirely when nothing's been captured) — if today's note predates it or omitted it this morning, insert it fresh (right after `## House`) rather than searching for a heading that isn't there. Remove it again if a refresh finds `snags_created: 0`.

Mark the refreshed sections with a small `*(refreshed HH:MM)*` italics tag at the heading. Don't proliferate these — replace the previous tag if one exists.

If a section's data hasn't materially changed (e.g. same calendar events, same DARTs), still update the timestamp but don't rewrite the body.

For the Email and WhatsApp sections specifically: re-triage *since the previous note's last refresh timestamp*, not from scratch. Use the existing triage as a baseline; only surface new items, completed items, and items whose status changed. The goal is "delta", not "rebuild".

### 3.5. Inbox triage (conditional)

Only fires if `inbox_pending` returned `count > 0` and the user did *not* pass section filters (or did pass `inbox` explicitly). Mid-day refreshes are exactly when Tines-dropped PDFs and quick-share notes accumulate, so this is where they get caught.

Pattern is identical to step 7.5 in `/daily-note`: present a numbered table with age/kind/source/original-filename/preview, ask for one-line-per-item routing decisions, then call `inbox_to_corpus` / `inbox_to_vault` / `inbox_archive` / `inbox_dismiss` in parallel.

If the user wants to defer, say `skip` and the items stay pending — they'll resurface on the next refresh or tomorrow's daily note.

### 4. Run reminder reconciliation (step 8.5)

Always run reconciliation after a refresh (unless the user passed `reminders` as a filter, in which case only do reminder reconciliation and skip the rest). Pattern is identical to `/daily-note` step 8.5 / `/reconcile-reminders`. High-confidence completes apply automatically; propose the rest.

### 5. Update the Notes section delta

At the bottom of `## Notes`, append a one-paragraph delta of what changed since the previous refresh: `**HH:MM refresh delta:** Revenue submitted (Sam), Finn choir paid, Kevin Ryan QS final invoice landed, photo shoot 13 Jun confirmed paid.` — mirrors the manual delta we wrote on 8 May.

If the user passed `--full`, regenerate the Pulse opinion lines + Morning assessment block via Haiku.

### 6. Confirm

Short summary line: `"Refreshed: calendar, transport, email, whatsapp, attachments, health, listening, coffee, snags (2 captured). 4 reminders auto-completed, 2 proposed for confirmation. New since last check: <delta>."`

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
