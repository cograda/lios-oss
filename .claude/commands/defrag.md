# Defrag — Task Consolidation & Cleanup

Aggressively consolidate, merge, and reorganise the task backlogs. Goes beyond `/stale` (which finds staleness and exact duplicates) — this clusters semantically related tasks, merges overlapping items, and tightens each backlog into fewer, cleaner entries.

## Architecture

Delegates heavy reading to a Haiku subagent. Opus handles judgement, presentation, and file writes.

## Instructions

### Step 1: Delegate reading to Haiku

Spawn an Agent with `model: "haiku"`. Ask it to read:

- `vault/Task Backlog.md` (full file)
- `vault/Delegated Tasks.md`
- `vault/Someday.md`
- The last 10 daily notes from `vault/Daily Notes/Alex/`
- The last 5 meeting notes from `vault/Meetings/`

Ask Haiku to return a structured analysis:

**1. Cluster map:** Group every open task by theme/project. Each cluster should have:
- A cluster name (e.g. "Driveway & Gates", "Mill Finishes", "Kids Medical")
- All tasks that belong to it (with their exact text, priority, and tags)
- Whether any tasks in the cluster are essentially the same thing said differently
- Whether any sub-tasks could be folded into a parent task as context rather than standalone items

**2. Staleness check:** For each task, note whether it appeared in any daily note Focus/Active section in the last 2 weeks.

**3. Delegation coherence:** For delegated items, flag any that overlap with non-delegated backlog items (double-tracking).

**4. Section fitness:** Flag tasks that look miscategorised — wrong domain heading, in the shared backlog when they should be personal (or vice versa), in the active backlog when they belong in Someday, etc.

**5. Backlog-to-Someday candidates:** Items currently in a Task Backlog that read as "nice to have" or have sat untouched for 6+ weeks.

Return the raw analysis — Opus will interpret and present.

### Step 2: Analyse and propose consolidation plan

Review Haiku's analysis. For each cluster, decide:

**Merge candidates:** Tasks that say essentially the same thing or where one is clearly a sub-task of another. Propose a single consolidated task that captures the intent of all merged items. Keep the highest priority from the group. Preserve important context as sub-bullets under the merged task (but only if genuinely useful — don't keep noise).

**Fold-ins:** Small tasks that are really just "next steps" on a bigger task. Propose folding them as context into the parent rather than standalone backlog items.

**Section moves:** Tasks in the wrong section.

**Move to Someday:** Items currently active but not really actionable now.

**Archive candidates:** Tasks that are done, no longer relevant, or superseded.

**No change:** Tasks that are fine as-is. Don't force consolidation where it doesn't help.

Present the plan organised by cluster:

```
## Cluster: [Name]
Current: [N] tasks
Proposed: [M] tasks

### Merges
- "Task A" + "Task B" + "Task C" → "New consolidated task text"
  - Keeps: [priority] [tags]
  - Sub-context preserved: [any important detail]

### Fold-ins
- "Small task" → folded into "Parent task" as sub-bullet

### To Someday
- "Task" — reason

### Archive
- "Old task" — reason

### Moves
- "Task" — from [Section] to [Section]
```

End with a summary: "X tasks → Y tasks (Z consolidated, W archived, V moved, U to Someday)"

### Step 3: Execute (on approval)

When the user says go ahead (they may adjust the plan first):

1. Update `vault/Task Backlog.md` with consolidated tasks
2. Update `vault/Delegated Tasks.md` if delegated items were consolidated
4. Move ideas/someday items to `vault/Someday.md`
5. Move completed items to the matching `* - Done.md` file
6. Update `modified:` and `last-reviewed:` in frontmatter on all modified files
7. Preserve ALL sections, headers, and overall structure — only change task lines
8. Do NOT touch the Obsidian Tasks query blocks at the top of the backlog

### Important rules

- Never delete a task without proposing it first — always get approval
- Preserve wiki links (`[[...]]`) in all consolidated tasks
- Preserve priority emojis, tags, and dates from the highest-priority version
- When merging, keep the most specific/actionable phrasing
- Delegated items: consolidate the prose bullets under each person/entity, but don't merge across people
- If a task has a `📅` date, it cannot be archived unless explicitly approved
- The goal is fewer, cleaner tasks — not reorganisation for its own sake
