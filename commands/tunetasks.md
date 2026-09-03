# Tune tasks

Audit the backlog against the rules and propose fixes. Usage: `/tunetasks [program | project | "week" | "focus"]`

Reads through comar's `tasks_*` tools, never by parsing `Task Backlog.md` — that file is a
rendered view of the ledger, and a hand edit to it locks every task write until someone
forces a render. With no argument it covers every open task; with one it scopes to that
program, project, or queue.

Ported from the work `taskdb` tool on 2026-09-02, the night after `/youdoit`. The passes are
unchanged; the tools are comar's, notes are a task's `description`, and there is no
`publish` step because every write re-renders the file itself.

## What this owns

Everything inside the ledger: task wording, duplicates, blocking links, placement under
programs and projects, queues. Orphan capture from other notes, `Someday.md` and
`Delegated Tasks.md` are outside it and belong to `/backlog-sweep`.

## Step 1: Gather

Call, in this order:

- `tasks_structure()` — domains, programs and projects with their end and done conditions,
  and the count of open tasks filed under no project
- `tasks_review()` — tasks failing the next-action rule, by flag. It also says whether the
  rendered file has been hand-edited since the last render; if it has, stop and say so
  before proposing anything — the ledger is locked until that is resolved
- `tasks_duplicates(threshold=0.75)` — wider than the default, so related-work pairs
  surface too. It excludes pairs already ruled distinct
- `tasks_block(action="list")` — what is already recorded as blocked, and `most_blocking`
- `tasks_query(...)` scoped to the argument (`program=`, `project=`, `queue=`), or unscoped
  with `limit=500`

**Read the descriptions.** They carry the constraints and half-decisions Pass C runs on,
and blocking relationships are usually already stated there in prose.

## Step 2: Four passes

### Pass A — conformance

**Most flags are false positives.** Long context text and a prose "and" both trip the
heuristic on tasks that are genuinely one move. Read all of them, propose on the few that
are real, and say the ratio when presenting so the number is not misread as the count of
problems.

For each task that is genuinely flagged, decide which it is:

- **Container.** Names an area, not a move. The fix is usually to replace it with the first
  real action and put the rest in the program's hub note, *not* to split it into more
  containers.
- **Several actions.** Propose `tasks_split`. If the parts must happen in order, they get
  `tasks_block` links, not just separate rows.
- **A decision with no proposal behind it.** "Decide what X is" cannot be started. Writing
  the proposal can. This is the most common container in practice.
- **Actually a project.** It needs its own done condition, so it is not a task. Propose
  `tasks_project(action="create", done_when=...)` with an inferred done-when, and carve out
  the single action that starts it.
- **Actually standing work.** Recurs by nature and will never be ticked. Say so; routines
  are not modelled yet, so for now it moves to `status="someday"` with the recurrence in
  its description, and the taxonomy note gets a line.

Do not rewrite prose for its own sake. A long task that is genuinely one action is fine.
When narrowing a task, the detail that gets cut goes into `description`, never dropped.

### Pass B — duplicates and clusters

Above 0.85 is usually the same task said twice: propose `tasks_merge`, naming which survives
and why. Between 0.75 and 0.85 is usually related work. Read both before calling it. **Say
when a pair is not a duplicate** rather than staying silent, so the same pair is not
re-litigated next week; record it with `tasks_merge(action="distinct")`, which keeps the
pair out of every future run.

Two shapes come up repeatedly and neither is duplication:

- **Sequential pairs.** "Send X the document" and "Talk the document through with X" score
  above 0.90 and are two steps, not one. They want a `tasks_block` link, not a merge.
- **Same-subject clusters.** Every task in a program about the same subject scores high
  against every other one, because they share vocabulary. Distinct work, correctly one
  program.

A cluster of four or more related tasks in one program is a project waiting to be named.

### Pass C — what blocks what

This is the pass that cannot be done by heuristic, and the most valuable.

**Start by searching titles and descriptions for blocking language,** because a good share
of the links are already stated in prose and were simply never recorded. The phrasings that
recur: "blocking", "blocked until", "before any", "wait for", "contingent on", "on hold
until", "once X", "after X", "depending on". Each hit is a `tasks_block(action="add")` you
can propose with the task's own words as the reason.

Then move to judgement: tasks that cannot start until another finishes. A doc that needs
reviewing before a conversation, a definition that has to settle before a build, a decision
that gates everything under it.

Watch for one blocker gating a whole cluster. `most_blocking` marks blockers whose priority
is lower than the work waiting on them; that says the priority is wrong as much as the link
was missing.

Where something is blocked, check the unblocking is itself a task. If it is not, that is
the missing task, and it matters more than anything it blocks.

### Pass D — placement and hygiene

- **Program end conditions against the tasks under them.** The highest-value check in this
  pass. A program whose end condition does not cover half its tasks is either mis-scoped or
  holding work that belongs elsewhere, and the fix is a decision about which. Name the
  tasks that outlive the end condition and give the options.
- Projects with no `done_when`, or one that no longer matches the tasks under it
- Tasks in a program that has no bearing on them
- Loose tasks (no project) clustering into something that wants naming
- Overdue dates that are stale rather than urgent
- High-priority tasks due within a day or two that are in neither queue

## Step 3: Present

One numbered list, grouped by pass, most consequential first. Each line says what and why.

**Use task text, never the uid.** The uid is a join key, not a label, and a list of
`TASK-0xxx` is unreadable. Quote enough of the task to identify it, in italics, and name the
program it sits in. Keep the uids to yourself and hold the mapping.

Give a one-line total at the end, broken down by pass.

Ask for answers by number, and say plainly that **silence is a no**. Expect the answers to
arrive with modifiers rather than as a clean yes: "these two are the same, not blockers",
"drop that one, it is stale". Take the modifier as the instruction and apply that version,
not the one proposed.

Expect a follow-on round. An approved item often opens a question that needs its own
numbered list, particularly anything in Pass D.

## Step 4: Apply

Make the approved calls. Each write re-renders `Task Backlog.md` itself; there is nothing to
publish. Confirm at the end with `tasks_review()` that the render is clean.

Report the counts grouped the way the changes were made, not the way they were proposed, and
say explicitly what was not applied and what will therefore resurface next time.

Then point at `/youdoit`. A tuned backlog is the input it needs, because a container cannot
be scored for how much of it an agent could do. One line at the end of the report is enough.

## Dropping a task

"Drop it" is a common answer and it is `tasks_update(status="dropped")`, which keeps the
row readable in history rather than deleting it.

**When the task is substantive, put the original scope and provenance in the description
before dropping.** A high-priority task with named dependents should not vanish leaving one
line. Record what it was for and where it came from, so it is recoverable if the reason it
existed comes back.

## Rules

- A merge keeps the survivor's uid; never mint a new id for existing work.
- Do not invent facts into task text or descriptions. If provenance is unclear, say so and
  leave it.
- Task text is read by a person. Keep it plain.
- Never edit `Task Backlog.md`. If `tasks_review` reports a hand edit, stop and say so.
