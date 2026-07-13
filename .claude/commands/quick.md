Show open `#quick` tasks from the unified backlog, sorted by priority. For when there's a 10-minute slot and you want a few wins.

Arguments: $ARGUMENTS

Argument handling:
- No args: show top 8 quick tasks
- A number (e.g. `/quick 5`): show top N
- `work`: filter to anything tagged `#acme`, `#work`, or with a work-shaped phone number
- A domain (`home`/`renovation`/`kids`/`finance`/`admin`): filter to that domain

Multiple filters can combine: `/quick 5 home`.

## Steps

### 1. Determine filter

Honour any filter arg (domain or `work`).

### 2. Read the source

- `Read` `Task Backlog.md` (the single unified backlog — the one place open tasks live)

### 3. Extract `#quick` tasks

Pull every open task (`- [ ]`) where the line contains `#quick`. Keep:
- Full task text
- Domain heading it sits under
- Priority emoji (🔺 / ⏫ / 🔼 / 🔽)
- Due date if present (📅)

### 5. Sort and trim

Sort by priority (🔺 → ⏫ → 🔼 → 🔽), then by stalled days (most stalled first inside each priority band). Trim to N (default 8).

### 6. Present

Format as a numbered list with one line per task. Inline anything that helps you act *right now* — phone numbers, account numbers, links — pulled straight from the task text. Don't dump tags.

```
## Quick wins (top 8)

🔺 Urgent
1. Call Example Electrical +353 1 555 0123 (opt 1) — X123AB4 ducting swap (Focus today)
2. Forward the solar quote #3256 to Dara Nolan

⏫ This week
3. Call Pat re comms cabinet — install date + Cat6 termination
4. Reply to Dara on kitchen drawings — fridge opens left, second shelf above fridge
5. Confirm credit card date with StoneCo

🔼 Planned
6. Bring batteries into school for collection
7. Buy more vitamins
8. Find a comfy fleece in the Patagonia store (next time in town)

(Source: 5 from daily note, 3 from Alex backlog. Filtered to #quick. 12 more 🔼/🔽 quick items not shown.)
```

### 7. Offer to act

After the list, offer:

> "Tackling any of these? Reply with numbers (e.g. '1, 3') and I'll set up — for calls I'll surface notes/context, for tasks with reminders I'll mark them in progress, for completed ones I'll close the loop."

When the user picks numbers:
- For calls: pull any related notes (`Grep` the vault for the supplier/contact name) to surface phone numbers, account numbers, last-correspondence date in one block
- For done items: mark `[x]` in source file + complete reminder if one exists
- For "in progress": no-op visually, but useful as user signal

### 8. Done

If the user says "thanks" / "got it" / "no" — just confirm and step out. Goal is to *enable a 10-minute burst*, not run a triage session.

## Notes

- This is a thin read-then-render skill. No haiku subagent needed — the LLM has all four files in context and the formatting work is trivial.
- Keep the output dense. The whole value prop is "I have 10 minutes, what should I knock out?" — don't pad.
- Don't show stalled items lower than 🔼 unless the user asks (`/quick all`). Stalled 🔽s are usually stalled for a reason.
