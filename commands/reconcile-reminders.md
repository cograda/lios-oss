Reconcile Apple Reminders against the source-of-truth task lists (today's daily note Focus + the single unified backlog).

Use this any time reminders feel out of sync — duplicates piling up, things ticked off in the note but still showing on the phone, or voice-dictated reminders that have never been routed anywhere.

## When to use

- After a busy morning where the note has moved fast but reminders haven't caught up
- When `reminders_sync` shows >25 open items and several look stale or duplicative
- Mid-week, in between daily notes
- When `/daily-note` already ran and the reconciliation step there was skipped

`/daily-note` already includes a reconciliation pass as step 8.5 — this command is just that step in isolation.

## Steps

### 1. Determine the user

Reconcile for Alex.

### 2. Gather state

Call in parallel:

- `reminders_sync` (no `since` — full snapshot of open reminders)
- `Read` `Daily Notes/<user>/<today>.md` (today's note if it exists)
- `Read` the single unified backlog `Task Backlog.md` (+ `Someday.md` / `Delegated Tasks.md` if useful). The `Alex/`·`Sam/` backlogs are empty pointer stubs — skip them.

### 3. Classify each open reminder

Follow the same matching rules as `/daily-note` step 8.5 — see that file for the full taxonomy. In summary:

| Bucket | Default action |
|---|---|
| `done-high-conf` | Auto-complete |
| `done-low-conf` | Propose complete, await user confirmation |
| `merge` | Propose merging duplicates into one canonical task |
| `aligned` | Leave alone (count only, don't list) |
| `orphan-add` | Propose adding to a backlog |
| `orphan-keep` | Leave alone |

Strong matching signals: proper nouns, phone numbers, account numbers, part numbers, supplier names, person names.

### 4. Present the diff

Same format as the daily-note step 8.5 output: an `Auto-completing` block (already applied, listed for transparency), a `Propose completing` numbered list, an `Add to backlog` numbered list, and a summary line for `aligned` + `orphan-keep` counts.

### 5. Apply on approval

- `reminders_complete` for confirmed completes/merges
- Append to backlog file + `reminders_complete` for `orphan-add`
- Never delete (complete only, so the iCloud Recently Deleted view keeps them recoverable)

### 6. Confirm

One-line summary: `"Reconciled: N completed, M moved to backlog. K aligned, J kept (no change)."`

## Safety rules

- Never auto-complete based on backlog state alone — needs evidence from today's session
- Never delete reminders, only complete them
- If a reminder has a future-dated due date and no matching task, treat it as `orphan-keep` (it's a reminder *for that future moment*, not a stale capture)
- Calendar-shaped reminders ("Wine with neighbours — 19:30") are `orphan-keep` — they're event reminders, not tasks
