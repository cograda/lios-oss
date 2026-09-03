# Backlog Sweep — Sunday Review

Three-phase backlog review run end-of-week. Combines `/stale` and `/defrag` into a single guided pass, then layers a manual read-through and a lock-in stamp.

Run on Sunday before `/weekly-review`.

## Architecture

Haiku does the heavy reading across backlog files and recent activity. Opus presents findings, applies approved changes, and records the result.

⚠️ **`Task Backlog.md` is GENERATED as of 2026-08-31.** The database is the
source of truth and the file is a one-way projection of it, re-rendered after
every write. Editing the file directly does not change a task — the edit is
lost the next time anything writes. Change the row; the file follows.

The other backlog files (`Delegated Tasks.md`, `Someday.md`, the per-person
backlogs) are **still hand-maintained** and are edited as before. Only the
shared `Task Backlog.md` has moved.

## Phases

1. **Auto pass** — scan, propose, apply approved changes
2. **Manual read** — pause for read-through and dictated adjustments
3. **Lock in** — record the sweep, log it

---

## Phase 1 — Auto pass

### Step 1.0: Ask the ledger what it already knows

Call `tasks_review()` **before** delegating any reading. It returns the tasks
that may not be next actions — containers naming an area rather than a move,
lines holding several actions, bare noun phrases — computed from the ledger,
so it costs one call and needs no file parsing.

Two rules on how to use the result:

- ⚠️ **Report the ratio, not the count.** A meaningful share are false
  positives: a long task that is genuinely one action trips the same rules.
  Saying "24 flagged" reads as "24 problems" and is wrong. Read each, propose
  on the ones that are real, and say how many of the flagged you are actually
  raising. The tool returns a `caveat` field saying so.
- **Never auto-fix one.** Deciding what a badly-written task actually means is
  a judgement about the work, not about the sentence. Every one goes into the
  Phase 2 numbered list for a human ruling.

Then call `tasks_block()` for the dependency picture. Two things in its
response are worth more than the raw graph:

- **`most_blocking`** ranks blockers by how much they hold up. One task gating
  four others is the finding; a chain of pairs is not.
- **`priority_inverted`** marks a blocker whose priority is *lower* than the
  work waiting on it. That usually means the priority is wrong, not just that
  the link was missing — raise it as a priority question, not a graph one.

While reading task text this sweep, watch for blocking stated in prose and
never recorded: "blocking", "blocked until", "before any", "waiting on",
"once X", "after X", "contingent on". Each is a link you can propose using
the task's own words as the reason. And where something is blocked, check the
unblocking is itself a task — **if it is not, that is the missing task, and it
matters more than anything it blocks.**

The response also carries `file.state`. If it is `drifted`, `Task Backlog.md`
has been hand-edited since it was last rendered and **every write will be
refused** until that is reconciled — deal with it before proposing anything,
or the whole sweep lands in a database whose file will not accept it.

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

- For `Task Backlog.md`: apply changes as **tool calls against uids**, never
  as text edits. Merge, archive, re-tag, re-prioritise and move-between-projects
  are all semantic operations on a task — they were always DB mutations
  wearing a text edit's clothes.
- For the hand-maintained files: update as approved, as before.
- Move completed items to the matching `* - Done.md` file
- Move ideas to `vault/Someday.md`
- Preserve all section headers, Obsidian Tasks query blocks, and wiki links

⚠️ **The old instruction "do NOT touch the Tasks query blocks" now inverts for
the generated file.** The renderer owns them: it emits them from a template on
every write. So the rule is no longer "don't edit them", it is **"don't
hand-write them"** — change the template in `app/integrations/tasks/render.py`
if a lens needs to change. On the hand-maintained files, the original rule
stands unchanged.

---

## Phase 2 — Manual read

After Phase 1 changes are applied:

1. Print absolute paths to the modified files so they can be opened in Obsidian
2. Print the new totals (shared / Alex / Sam / delegated / someday)
3. Wait. Don't proceed until the user comes back with adjustments or "looks good"

The user will read through the cleaned files and dictate edits.

- For the hand-maintained files, apply them as plain edits — no re-analysis
  required, as before.
- ⚠️ **For `Task Backlog.md`, a plain edit is not durable** — the file is
  regenerated from the database, so a dictated change must become a
  `tasks_bulk_update` against the affected uids, then a re-render. The uids
  are in the ledger; the file shows the same tasks.

Repeat until they're happy.

---

## Phase 3 — Lock in

When the user confirms ("lock it in" or similar):

1. Record the sweep, then update frontmatter:
   - For `Task Backlog.md`: call `record_sweep()` — a `task_events` row with a
     **null task_id**, because a sweep is a review of the whole backlog and
     belongs to no single task. `last-reviewed` in the rendered file is then
     COMPUTED from the newest sweep, not stamped. Re-render afterwards.
     ⚠️ Do not hand-edit that frontmatter: the next render overwrites it, and
     a render deliberately refuses to move the date itself — otherwise merely
     printing the file would claim somebody read it.
   - For the hand-maintained files: `modified: <today>` and
     `last-reviewed: <today>` as before (create the field if missing).
2. If a weekly review note exists for the current week at `vault/Weekly Reviews/YYYY/YYYY-MM-DD.md` (Sunday date), append a line under a `## Backlog sweep` section noting the date and the totals. Create the section if it doesn't exist. If the weekly review doesn't exist yet, skip this step.
3. Report the final totals.

---

## Rules

- Never delete a task without proposing it in Phase 1
- Tasks with `📅` deadlines cannot be archived without explicit approval
- Don't merge delegated items across people/entities
- The goal is fewer, cleaner tasks — not reorganisation for its own sake
- Compute today's date programmatically before writing frontmatter
- Never hand-edit `Task Backlog.md` — it is generated. Change the row.
