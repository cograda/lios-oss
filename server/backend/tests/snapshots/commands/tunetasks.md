<!-- GENERATED from server/backend/app/prompts/templates/tunetasks.md.j2 via app.prompts.commands — do not hand-edit. Edit the template and run scripts/render_commands.py (or refetch GET /api/v1/commands). -->

# Tune tasks

Audit the backlog against the rules and propose fixes. Usage: `/tunetasks [program | project | "week" | "focus"]`

Reads through comar's `tasks_*` tools, never by parsing `Task Backlog.md` — that file is a
generated, write-only view of the ledger; every render overwrites it unconditionally, and a
hand edit to it is simply discarded on the next write, never a reason to stop. With no
argument it covers every open task **you own**; with one it scopes to that
program, project, or queue. Every `tasks_*` read defaults to the caller's own loops —
the ledger is household-shared, and `owner="household"` is the explicit way to widen a
call to everyone's. Do not widen unless Alex asks for the household view.

Ported from the work `taskdb` tool on 2026-09-02, the night after `/youdoit`. The passes are
unchanged; the tools are comar's, notes are a task's `description`, and there is no
`publish` step because every write re-renders the file itself.

## What this owns

Everything inside the ledger: task wording, duplicates, blocking links, placement under
programs and projects, queues — plus, since 2026-09-07, **lock-in** (Step 5, below). Orphan
capture from other notes belongs to `/kickoff`'s intake pass (Track C) and `/meeting`, not
here; `Someday.md` and `Delegated Tasks.md` are folded into the ledger
(`status="someday"` / `status="waiting"`) and nothing writes to them.
`/backlog-sweep`, `/defrag` and `/stale` were retired 2026-09-03 — this command is what
replaced them, together with the loops app's Problems and Duplicates views. `/triage` was
retired the same way on 2026-09-07, folded into `/kickoff`'s intake pass.

## Step 1: Gather

Call, in this order:

- `tasks_structure()` — domains, programs and projects with their end and done conditions,
  and the count of open tasks filed under no project
- `tasks_review()` — tasks failing the next-action rule, by flag. It also says whether the
  rendered file has been hand-edited since the last render; if it has, stop and say so
  before proposing anything — the ledger is locked until that is resolved
- `tasks_duplicates(threshold=0.75)` — wider than the default
  (`0.80`), so related-work pairs surface too. It excludes pairs already
  ruled distinct
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

At or above `0.80` is usually the same task said twice (the live ledger's
measured duplicate cluster sits at 0.82-0.84, well
inside this band): propose `tasks_merge`, naming which survives and why. Between
`0.75` and `0.80` is usually related work, not a
duplicate. Read both before calling it. **Say when a pair is not a duplicate** rather than
staying silent, so the same pair is not re-litigated next week; record it with
`tasks_merge(action="distinct")`, which keeps the
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
- **An open task that is really a lios bug or feature** (a bug or improvement
  in Loops, the daily note, a sync daemon, an MCP tool) rather than household
  work — lios development items belong in GitHub Issues on `cograda/lios`,
  not this ledger (decided 2026-09-07). Propose moving it: `gh issue create
  -R cograda/lios --title ... --label bug|feature|other --body ...`, then
  `tasks_complete(uid=...)` with a note `"Moved to cograda/lios#N"`.

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
publish, and nothing to confirm — the render always succeeds unconditionally.

Report the counts grouped the way the changes were made, not the way they were proposed, and
say explicitly what was not applied and what will therefore resurface next time.

## Step 5: Lock in

Moved here from `/kickoff`'s old step 9 on 2026-09-07 — the note no longer blocks on this,
and `/kickoff` does not chain into it. Run it as the last thing this command does, after
Step 4's writes have settled.

Offer Focus, capped at `daily_note.focus_count` (from the caller's preferences — read it
fresh, don't assume a number):

> "Ready to pick your Focus? (up to `daily_note.focus_count` items) Pick from what we just
> tuned by number, or name items directly."

When the user responds:

1. **Mirror into the ledger's focus queue** (drives the backlog's "🎯 Today's Focus" section
   and the loops app's Focus lens): `tasks_query(queue="focus")` for yesterday's picks and
   `tasks_update(uid=..., queue="week")` on any not chosen today (or `queue=null` if it isn't
   this week's work either); then `tasks_update(uid=..., queue="focus")` for each chosen
   item. For an ad-hoc pick not already in the ledger, `tasks_add(title=..., queue="focus",
   priority=...)`. Never edit `Task Backlog.md` directly — the tools re-render it.
2. Fetch current reminders via `reminders_sync`; for each Focus item with no matching
   reminder, `reminders_add` one with today's due date.
3. For any Focus item that's 2+ weeks out or time-specific, offer a calendar event via
   `calendar_create_event`.
4. **Write the picks into today's note's `### Focus` section** — path from `daily_notes_dir`
   (`vault/Daily Notes/Alex/<today>.md`). **If today's note is absent, say so and skip the
   file write** — the ledger and reminders updates above still happen regardless; only the
   note edit is conditional on the note existing.
5. Confirm: show the final Focus list and what was synced (reminders created, calendar
   events offered/created).

If the user says "not yet" or "later": "No problem — say 'lock in' whenever you're ready, or
set focus in the loops app."

Then point at `/youdoit`. A tuned, locked-in backlog is the input it needs, because a
container cannot be scored for how much of it an agent could do. One line at the end of the
report is enough.

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
- Never edit `Task Backlog.md` directly. It is a generated, write-only view — every render
  overwrites it unconditionally, so a hand edit is silently discarded on the next write, not
  refused. Use the `tasks_*` tools for every change.
