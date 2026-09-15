<!-- GENERATED from server/backend/app/prompts/templates/meeting.md.j2 via app.prompts.commands — do not hand-edit. Edit the template and run scripts/render_commands.py (or refetch GET /api/v1/commands). -->

Process a meeting transcript or conversation recording.

Arguments: $ARGUMENTS

The user will either paste a transcript directly, provide a file path, or describe the meeting for manual note creation.

## Step 1 — Parse (Haiku subagent)

Read `vault/Reference/Name Corrections.md` and pass its contents to the subagent alongside the transcript, so it can correct garbled proper nouns (Tines colleagues, renovation contacts, family) against known names rather than guessing.

Spawn a Haiku subagent with the transcript text. Instruct it to return structured JSON:

```json
{
  "title": "Meeting title",
  "date": "YYYY-MM-DD",
  "participants": ["Person 1", "Person 2"],
  "type": "planning | review | medical | school | renovation | financial | general",
  "summary": "2-4 sentences",
  "topics": [
    {
      "heading": "Topic name",
      "points": ["Key point", "Another point"]
    }
  ],
  "action_items": [
    {"owner": "Alex", "task": "Do the thing", "priority": "high"},
    {"owner": "Sam", "task": "Other thing", "priority": "medium"}
  ],
  "follow_ups": ["Next meeting date", "Thing to chase"],
  "corrections": ["Malahide not Graystones", "Finn not Oskar"],
  "new_people": ["Person not yet in People/"]
}
```

Parsing rules:
- Strip small talk and filler
- Preserve nuance, candour, and specific numbers
- Attribute opinions and commitments to people
- Flag transcription errors on proper nouns
- For Alex/Sam conversations: focus on decisions, commitments, and unresolved items

## Step 1.5 — Pull prior decisions (renovation meetings only)

If `type` is `renovation` OR participants include any of Cameron, David Moran, Neil McGroary, Kev O Sullivan, OR topics mention BoQ, recommendation for payment, PC sums, drawings, specification, or variation costs:

Call `corpus_search` once per topic heading with `{"query": "<topic heading + 2-3 keywords from points>", "limit": 5}`. Purpose: surface prior decisions, prior quotes, and stated rates BEFORE writing the meeting note, so the note can cross-reference what was already agreed.

Embed any matching prior context under the topic's bullets as:
> **Prior:** <one-line summary> — [[source filename]] (YYYY-MM-DD)

Skip silently for non-renovation meetings — this step should never add noise to a medical/school/financial meeting.

## Step 2 — Integrate (main model)

Take the structured output and:

### 2a. Create meeting note

Save to `vault/Meetings/YYYY-MM-DD Topic.md`:

```markdown
---
date: YYYY-MM-DD
type: meeting
participants:
  - "[[Person 1]]"
  - "[[Person 2]]"
tags: [meeting, type-tag]
---

# Meeting Title — D Month YYYY

**Participants:** [[Person 1]], [[Person 2]]

## Summary

Summary text.

## Notes

### Topic 1

- Key point
  - Sub-detail

## Action Items

- [ ] **Alex:** Task ⏫
- [ ] **Sam:** Task 🔼

## Follow-ups

- Next steps
```

### 2b. Update daily note

If today's daily note exists for Alex (`vault/Daily Notes/Alex/<today>.md`):
- Add meeting link under `## Meetings`
- Action items go to the backlog (below), not the daily note — the note surfaces them via its [[Task Backlog]] query. Only add an item to `### Focus` if the user wants to commit to it today.

### 2c. Update the ledger

> **The backlog is a ledger, not a file.** Never read `Task Backlog.md` for state and never edit it — it is a rendered view and a hand edit locks every task write. Read with `tasks_query`, write with `tasks_add` / `tasks_update` / `tasks_complete` / `tasks_bulk_update`; every write re-renders the file.

For each action item:
- Check it is not already tracked: `tasks_query(text=<distinctive token>)`. If it is, `tasks_note_add(uid=..., body="Discussed in [[<meeting note>]] — <what changed>")` rather than a duplicate.
- Ours to do → `tasks_add(title=..., priority=<from the JSON>, project=<from tasks_structure() if one fits>, description="From [[<meeting note>]] (<date>)", source="meeting")`. Keep `**Alex:**` / `**Sam:**` out of the title; the owner goes in `description` until owners are a column.
- Owed to us by someone else → `tasks_add(..., status="waiting", description="Waiting on <person> — from [[<meeting note>]]")`. (`Delegated Tasks.md` is folded into the ledger — the ledger's `waiting` status is the whole of it now; do not add to the file.)
- Put the resulting uids into the meeting note's `## Action Items` lines, e.g. `- [ ] **Alex:** Task ⏫ · TASK-0261`, so the note and the ledger point at each other.

### 2d. Create entity notes

For people in `new_people`:
- Create `vault/People/Person Name.md` with basic frontmatter
- Ask the user for context if the role isn't clear from the transcript

### 3. Show summary

Display: what was created, key action items, and any entities that need attention.
