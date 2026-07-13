Triage incoming items since the last check — quick routing for new emails, WhatsApp messages, and reminders.

Arguments: $ARGUMENTS

This is the day-to-day counterpart to `/daily-note` (morning) and `/seed-backlog` (periodic deep scan). Run it whenever you want to process what's come in.

## Steps

### 1. Determine lookback window

Read `vault/Alex/.triage-state.md` to get the last triage timestamp. If the file doesn't exist or can't be read, default to 24 hours ago.

Calculate the `after` timestamp as an ISO 8601 string (e.g. `2026-04-03T10:30:00`).

If arguments specify a lookback like "last 2 hours" or "since lunch", use that instead.

### 2. Fetch new items

Call these MCP tools in parallel:

- **`gmail_recent`** with `{"after": "<timestamp>", "limit": 50}` — new emails since last triage
- **`whatsapp_recent`** with `{"after": "<timestamp>", "limit": 100, "include_groups": true}` — new WhatsApp messages
- **`reminders_list`** — current reminders (for cross-referencing)
- **`attachments_pending`** with `{"since_days": 14, "limit": 20}` — documents shared in WhatsApp not yet ingested. Surface in the triage table as `attachment` rows; suggested action is `→ ingest` (call `attachments_ingest` with the chosen ids) or `dismiss`. Items >14 days old are at risk of CDN expiry — flag with ⚠️.

Also read the daily note for today if it exists: `vault/Daily Notes/Alex/YYYY-MM-DD.md` — to understand what's already on the radar.

### 3. Classify items (Haiku subagent)

Spawn a **single Haiku subagent** with all the raw data. Give it these instructions:

> You are triaging incoming messages for a family knowledge system. Classify each item and check for unanswered threads.
>
> For each new email or WhatsApp message, determine:
> - **item**: what it's about (concise, imperative if action needed)
> - **source**: `email` or `whatsapp` + sender/contact name
> - **type**: one of:
>   - `snag` — a defect or move-in job at [[The Mill]] (the house being renovated). Detect by: a `Snag - ...` or `Task - ...` prefix; OR content from Sam / the snagger / a trade describing a fault, unfinished work, or a fitting job in the new house. Sub-classify:
>     - `snag/defect` — something done wrong that a trade must fix (`Snag - <room> - <defect>`)
>     - `snag/job` — a job for us to do (`Task - <room> - <job>`, e.g. hang a mirror, fit a towel rail)
>   - `action` — someone needs to do something
>   - `reply-needed` — a question or request directed at Alex that hasn't been answered. Check: is the last message in this thread from someone else? Does it contain a question, request, or ask? How many days has it been waiting?
>   - `FYI` — informational, no action needed
>   - `done` — mentions something being completed
> - **wait_days**: for `reply-needed` items, how many days since the message was sent. 0 for today.
> - **priority**: 🔺 urgent, ⏫ high, 🔼 medium, 🔽 low
> - **suggested_action**: one of:
>   - `→ snag-list` — capture into the move-in snag/task list (see routing below)
>   - `→ backlog` — should be tracked as a task
>   - `→ reply` — needs a response (flag it)
>   - `→ calendar` — needs a calendar entry
>   - `→ done` — mark as complete
>   - `dismiss` — no action needed
>
> **Rules:**
> - Skip automated notifications (bank alerts, delivery confirmations, newsletter digests)
> - Skip casual chat, jokes, memes, reactions, media-only messages — **except** snag messages: a photo with a `Snag - ...`/`Task - ...` caption (or any caption describing a house defect/job) is a `snag`, never skip it
> - Each `Snag -`/`Task -` line is its own item — do **not** merge a run of them into one (they are distinct defects/jobs)
> - For WhatsApp groups: only flag as `reply-needed` if Alex was directly addressed or a question was clearly directed at him. Skip general group banter.
> - For email: check the thread — if the most recent message is FROM Alex's account, it's not reply-needed
> - Be conservative with `reply-needed` — only flag genuine outstanding questions/requests
> - Merge related items (e.g. 3 messages in the same WhatsApp thread about the same topic → 1 item)
>
> **EXISTING CONTEXT (already being tracked):**
> [paste today's daily note content if it exists, or "No daily note yet"]
>
> **CURRENT REMINDERS:**
> [paste reminders_list result]
>
> **NEW EMAIL:**
> [paste gmail_recent result]
>
> **NEW WHATSAPP:**
> [paste whatsapp_recent result]

### 4. Present triage table

Show the classified items, with `reply-needed` items first (sorted by wait time), then `action` items, then others:

```
## Triage — since HH:MM DD/MM

### 🔨 Snags — Comar
#  | Source              | Item                                    | Kind   | Priority
1  | whatsapp — snagger  | guest wc — tiling grout messy/uneven    | defect | ⏫
2  | whatsapp — snagger  | guest wc — hang towel rail              | job    | 🔼

### Needs Reply
#  | Source              | Item                                    | Wait  | Priority
3  | email — John Smith  | Quote for garden fence                  | 3d    | ⏫
4  | whatsapp — Sam    | Finn swimming — which day works?       | 1d    | ⏫

### New Actions
#  | Source              | Item                                    | Priority | Suggestion
3  | email — energy co         | Meter reading due next week             | 🔼       | → backlog
4  | whatsapp — BuildCo | Confirm tile colour before Friday       | ⏫       | → backlog 📅 2026-04-04

### FYI / Done
#  | Source              | Item                                    | Note
5  | email — school      | Easter holiday dates confirmed          | dismiss
6  | whatsapp — Alex's mam | Dropped off the books                | → done ✓
```

If there are no items since last triage, say so: "Nothing new since last triage (HH:MM DD/MM). All clear."

### 5. Route items

Ask the user:

> "Route these items — e.g. '1 → reply tomorrow, 3 4 → backlog, dismiss 5 6' — or 'accept all' to go with suggestions."

**After the user responds, execute:**

- **→ snag-list**: **the snag database is the source of truth** (each snag gets a stable `SNAG-NNNN` UID; the vault view `Household/Renovation/Snags.md` regenerates automatically — never hand-edit it).
  - `snag/defect` with a `Snag - ...` prefix (text or photo caption) → call `snag_capture` (`{"since_days": 1}` for a normal triage window). It parses room/trade/detail, links evidence photos from the media store, skips already-captured messages, and re-renders the vault note. One call covers a whole run of snag messages.
  - `snag/defect` WITHOUT the structured prefix (free-text fault reports) → `snag_add` with `title`, `room`, and `trade` if clear (else `unknown`); `severity` only when obvious (water/safety/structural → `major`/`critical`).
  - `snag/job` (`Task - <room> - <job>` — ours to do, not a defect) → NOT a snag: add to the **[[Comar Project Board]]** under `## Us (Alex / Sam)` as `- [ ] **<Area>** — <job> 🔼 #renovation #cat/physical`. Strip the `Task -` prefix.
  - `comar query - <question>` (Sam addressing the system directly, convention started 2026-07-08) → NOT a snag: add to the **[[Comar Project Board]]** under `## Open queries` as a checkbox with the question and who can answer it (Fergal / [[Pat]] / etc.). Answer directly in the triage summary if comar already knows.
  - `SNAG-NNNN` referenced in a message (correction/photo for an existing snag) → `snag_update` on that UID (`title`/`attach_media_ids`/`remove_media` as appropriate), not a new snag.
  - Only add to Alex's [[Task Backlog]] (`#renovation #movein`) if Alex explicitly says it's his to action — that's the "promote" step.
  - Confirm counts: "N snags captured (SNAG-00XX–SNAG-00YY) + M jobs → Project Board."
- **→ backlog**: Use the same logic as `/add-task` — determine the right backlog file and section, format with Obsidian Tasks syntax, add via `Edit` on the backlog file. If a due date was identified, include `📅 YYYY-MM-DD`.
- **→ reply**: Note it prominently in today's daily note under Notes section: `- ⚡ Reply to [[Person]] — topic (waiting X days)`
- **→ calendar**: Call `calendar_create_event` with the identified date/time.
- **→ done**: If there's a matching reminder, call `reminders_complete`. Note in daily note.
- **→ ingest**: Call `attachments_ingest` with `{"ids": [...]}` for the chosen attachment ids. Each becomes a `HistoricalDocument` searchable via `renovation_context`. Confirm count of ingested vs failed.
- **dismiss**: No action — just acknowledge.

For backlog additions, also offer to create a reminder: "Want Apple Reminders for any of these? (say which numbers)"

### 6. Update triage state

Write the current timestamp to `vault/Alex/.triage-state.md`:

```markdown
---
title: Triage State
type: note
created: YYYY-MM-DD
modified: YYYY-MM-DD
---

last_triage: YYYY-MM-DDTHH:MM:SS
```

Use `Write` to create or overwrite.

### 7. Done

Brief summary: "Triaged X items — Y to backlog, Z flagged for reply, W dismissed."

If reply-needed items were identified, remind: "You have N messages waiting for a reply — oldest is X days."

## Notes

- This is designed to be fast — run it 2-3 times per day between focused work blocks.
- Unlike `/seed-backlog` (deep scan, 6 weeks), this is a lightweight sweep of what's new.
- Unlike `/daily-note` (morning ritual, full briefing), this is a quick check-in during the day.
- The triage state file ensures you never process the same messages twice.
