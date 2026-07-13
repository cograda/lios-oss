# Backlog Sweep — Sunday Review

Three-phase backlog review run end-of-week. Combines `/stale` and `/defrag` into a single guided pass, then layers a manual read-through and a lock-in stamp.

Run on Sunday before `/weekly-review`.

## Architecture

Haiku does the heavy reading across backlog files and recent activity. Opus presents findings, applies approved changes, and stamps the result.

## Phases

1. **Auto pass** — scan, propose, apply approved changes
2. **Manual read** — pause for read-through and dictated adjustments
3. **Lock in** — stamp `last-reviewed` date, log the sweep

---

## Phase 1 — Auto pass

### Step 1.1: Delegate reading to Haiku

Spawn an Agent with `model: "haiku"`. Ask it to read:

- `vault/Task Backlog.md`
- `vault/Delegated Tasks.md`
- `vault/Someday.md`
- Last 10 daily notes from `vault/Daily Notes/Alex/`
- Last 5 meeting notes from `vault/Meetings/`

Ask Haiku to return a single structured report covering:

**Staleness**
- Non-parked backlog items not surfaced in any daily note Focus/Active in the last 2 weeks
- Delegated items where the owner hasn't been mentioned in any meeting or daily note in the last 4 weeks

**Duplicates**
- Tasks in daily Active sections that duplicate backlog items
- Backlog items that say the same thing in different words
- Cross-backlog duplicates (e.g. same task in shared and personal)

**Orphans**
- Significant tasks in daily/meeting notes not tracked in any backlog

**Hierarchy integrity (Task Backlog files only)**
- Missing or multiple domain tags (`#home` `#renovation` `#kids` `#finance` `#admin`)
- H2 project headings that aren't `[[wiki links]]`
- Tasks under the wrong domain (e.g. `#kids` task under `# Home`)

**Cluster map**
- Group all open tasks by project
- Within each cluster, flag merge candidates (same task said differently) and fold-in candidates (small tasks that are really sub-steps of a parent)

**Section fitness**
- Tasks in the wrong file/section (e.g. delegated item in the main backlog, idea in the backlog instead of Someday, personal task in the shared backlog)

Return raw findings. Opus will interpret and present.

### Step 1.2: Present a single consolidated plan

Combine Haiku's findings into one plan, organised by file:

```
## Task Backlog (shared)
- Merges: "Task A" + "Task B" → "Consolidated task" (keeps priority/tags)
- Fold-ins: "Sub-task" → context bullet under "Parent task"
- Archive: "Old task" — reason
- Re-tag: "Task" — missing domain tag
- To Someday: "Task" — reason
- Moves: "Task" — from [Section] to [Section]

## Alex / Sam Task Backlogs
- Same shape

## Delegated Tasks
- Stale items, owner not mentioned in 4+ weeks
- Cross-tracking with backlog

## Someday
- Promotions to backlog (ready to act)
- Items that should be deleted

## Orphans to add
- Task from meeting [[YYYY-MM-DD topic]]
```

End with a summary line: "Shared: N → M. Alex: N → M. Sam: N → M. Delegated: N → M. Someday: N → M."

### Step 1.3: Apply approved changes

When the user approves (whole plan or item-by-item edits):

- Update each backlog file as approved
- Move completed items to the matching `* - Done.md` file
- Move ideas to `vault/Someday.md`
- Preserve all section headers, Obsidian Tasks query blocks, and wiki links
- Preserve priority emojis, tags, and dates from the highest-priority version of merged items

Do NOT touch the Tasks query blocks at the top of any backlog.

---

## Phase 2 — Manual read

After Phase 1 changes are applied:

1. Print absolute paths to the modified files so they can be opened in Obsidian
2. Print the new totals (shared / Alex / Sam / delegated / someday)
3. Wait. Don't proceed until the user comes back with adjustments or "looks good"

The user will read through the cleaned files and dictate edits. Apply those as plain edits — no re-analysis required. Repeat until they're happy.

---

## Phase 3 — Lock in

When the user confirms ("lock it in" or similar):

1. Update frontmatter on all touched files:
   - `modified: <today>`
   - `last-reviewed: <today>` (create the field if missing)
2. If a weekly review note exists for the current week at `vault/Weekly Reviews/YYYY/YYYY-MM-DD.md` (Sunday date), append a line under a `## Backlog sweep` section noting the date and the totals. Create the section if it doesn't exist. If the weekly review doesn't exist yet, skip this step.
3. Report the final totals.

---

## Rules

- Never delete a task without proposing it in Phase 1
- Tasks with `📅` deadlines cannot be archived without explicit approval
- Don't merge delegated items across people/entities
- The goal is fewer, cleaner tasks — not reorganisation for its own sake
- Compute today's date programmatically before writing frontmatter
