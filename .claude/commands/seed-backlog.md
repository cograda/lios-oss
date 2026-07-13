Scan email and WhatsApp history for actionable items and seed the task backlog.

Arguments: $ARGUMENTS

Default lookback: 6 weeks. Can override with arguments like "3 months" or "2 weeks".

## Steps

### 1. Parse lookback window

Default 6 weeks. If arguments specify a different window, use that.

### 2. Read existing backlogs

Read the unified backlog to understand what's already tracked:
- `vault/Task Backlog.md`

Also read the done file to avoid re-suggesting completed items:
- `vault/Task Backlog - Done.md`

### 3. Fetch data in batches

Use MCP tools to pull data. Process in manageable chunks:

**Email**: Use `gmail_recent` with `{"limit": 50}` repeatedly, or `gmail_search` with date-bounded queries. Focus on:
- Personal email — the primary source of household actions
- Skip newsletters, marketing, automated notifications

**WhatsApp**: Use `whatsapp_contacts` to identify key contacts/groups, then `whatsapp_thread` for the most active threads. Focus on:
- Family group chats
- Conversations with tradespeople, school, medical
- 1:1 with Sam (shared household planning)
- Skip large noisy group chats with no actionable content

### 4. Extract candidates (Haiku subagents)

Process in batches of ~50 messages per Haiku call. Run multiple subagents in parallel where possible.

Each Haiku subagent gets these instructions:

> You are scanning messages for actionable items that should be on a family task backlog. Extract ONLY items that represent a concrete action someone needs to take — not information, not conversation, not things already done.
>
> For each candidate, return:
> - **action**: what needs to be done (imperative, concise)
> - **source**: email subject/sender or WhatsApp contact/group
> - **who**: alex, sam, shared, or unclear
> - **urgency**: urgent (overdue/time-sensitive), high (this month), medium (no rush but real), low (nice to have)
> - **category**: household, kids, renovation, vehicle, finance, medical, school, social, or other
> - **context**: one line of supporting context (date mentioned, person involved, etc.)
>
> SKIP:
> - Things that are clearly already done (past tense, "done", "sorted", "booked")
> - Pure information sharing (no action needed)
> - Casual conversation, jokes, memes
> - Newsletter content, marketing
> - Things that are someone else's responsibility (not Alex or Sam)
> - Automated notifications (bank alerts, delivery confirmations for completed deliveries)
>
> Return a JSON array of candidates, or an empty array if nothing actionable found.
>
> ---
> **MESSAGES:**
> [batch of messages]

### 5. Deduplicate and merge

Combine all candidates from all batches. Then:
- Remove duplicates (same action from different messages)
- Remove items already on the existing backlogs (compare against step 2)
- Remove items already in the done files
- Group by category
- Sort by urgency within each group

### 6. Present for review

Show the candidates grouped by category in a clear table format:

```
## Candidates from Email (X items)

### Household
| # | Action | Source | Who | Urgency | Context |
|---|--------|--------|-----|---------|---------|
| 1 | Call electrician about garden lights | Email from BuildCo, 15 Mar | Shared | High | Mentioned in renovation update |

### Kids
...

## Candidates from WhatsApp (X items)
...
```

Ask the user to:
- **Accept** items (by number) to add to the backlog
- **Reject** items that aren't needed
- **Edit** items before adding (change priority, wording, etc.)
- **Accept all** in a category

### 7. Add accepted items

For each accepted item, add to the appropriate backlog file using the same format and logic as `/add-task`:
- Determine the right backlog (shared, Alex, Sam)
- Find the right section within the file
- Format with priority emoji, due date if applicable, wiki links for people/entities
- Use Obsidian Tasks format: `- [ ] Task description ⏫ 📅 2026-04-15 #tag`

### 8. Summary

Show what was added:
- X items added to shared backlog
- X items added to Alex's backlog
- X items added to Sam's backlog
- X items rejected

Suggest running `/daily-note` after to pick up any new urgent items.

## Notes

- This is a one-time seed operation, not a recurring sync. Run it when bootstrapping or when you feel the backlog has drifted.
- For ongoing capture, the daily note's 72h lookback handles new actionable items day-to-day.
- The user can also run this conversationally: "scan my recent emails for things I need to do" triggers the same logic without the slash command.
