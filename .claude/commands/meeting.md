Process a meeting transcript or conversation recording.

Arguments: $ARGUMENTS

The user will either paste a transcript directly, provide a file path, or describe the meeting for manual note creation.

## Step 1 — Parse (Haiku subagent)

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
  "corrections": ["Malahide not Malahyde", "Finn not Fin"],
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

If `type` is `renovation` OR participants include any of Barry, Dara Nolan, Niall, Ken, OR topics mention BoQ, recommendation for payment, PC sums, drawings, specification, or variation costs:

Call `renovation_context` once per topic heading with `{"query": "<topic heading + 2-3 keywords from points>", "limit": 5}`. Purpose: surface prior decisions, prior quotes, and stated rates BEFORE writing the meeting note, so the note can cross-reference what was already agreed.

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

If today's daily note exists for Alex (or Sam, if they're running this):
- Add meeting link under `## Meetings`
- Action items go to the backlog (below), not the daily note — the note surfaces them via its [[Task Backlog]] query. Only add an item to `### Focus` if the user wants to commit to it today.

### 2c. Update backlog

For action items:
- Add to the single unified backlog `vault/Task Backlog.md` under the right domain heading (`#home`/`#renovation`/`#kids`/`#finance`/`#admin`). The `Alex/`·`Sam/` files are retired stubs — don't write there.
- Add items owed to us by others to `vault/Delegated Tasks.md` (grouped by person/entity)
- Don't duplicate — check if the task already exists first

### 2d. Create entity notes

For people in `new_people`:
- Create `vault/People/Person Name.md` with basic frontmatter
- Ask the user for context if the role isn't clear from the transcript

### 3. Show summary

Display: what was created, key action items, and any entities that need attention.
