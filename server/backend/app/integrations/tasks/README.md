# tasks

Task and project ledger. The database is the source of truth; `Task
Backlog.md` (`render.py`) is a generated one-way view of it.

Hierarchy: **domains → programs → projects → tasks** (`loops.py`), with
**routines and rounds** (`routines.py`) as the recurring class, added
2026-09-03 (chunk E3).

## Vocabulary (ruled by Alex 2026-09-03 — do not re-open)

- A **loop** is anything with a definition of done. It can be closed, and it
  may contain sub-loops that must close first.
- A **routine** is a recurring loop *template*. It never closes itself.
- A **round** is one occurrence of a routine, and it closes.

**A round IS a `tasks` row** — `Task.routine_id` set. Rounds get every
existing lens, queue, block, comment, history and accept/transfer semantic
for free (`tasks_query`, `tasks_block`, `tasks_note_add`, `tasks_history`,
`tasks_transfer`/`_accept`/`_decline` on the round's own uid, …). There is
deliberately no separate occurrence table.

## `kind` and `severity` are for household bugs and features (revised 2026-09-07)

A household bug report or feature ask (a broken appliance, a wanted household
capability) is a loop like any other — it has a definition of done, an owner,
a project and a history — so it is a `tasks` row with a **`kind`** (`task`
default | `bug` | `feature` | `chore`, `KINDS` in `models.py`) and, for bugs
and features, a nullable **`severity`** (`critical` | `high` | `medium` |
`low`). `tasks_add`/`tasks_update`/`tasks_bulk_update` take both,
`tasks_query` filters on both, every row serialises both, changes land as
field-level `task_events`, and the rendered file shows a compact marker after
the title (`[bug/high]`, `[feature]`, `[chore]`; an ordinary task shows
nothing, so no pre-existing line moved).

**lios development items are GitHub Issues, not ledger rows.** The original
S5.3 decision (2026-09-07 morning) made this ledger the register of lios's
own bugs and features too; that was reversed the same day (2026-09-07,
evening) — a bug or feature in the lios platform itself (Loops, the daily
note, a sync daemon, an MCP tool) is filed as a GitHub Issue on
`cograda/lios` (labels `bug`/`feature`/`other`), never a `tasks` row. The ten
register tasks created under the first decision were moved to issues
#138–#148 and closed here.

## Minting policy — just-in-time, always

Only the **next** round exists as an open row at any time — never a year of
rows up front.

- **Interval routines** (`schedule_kind="interval"`, `schedule_spec` an
  ISO-8601 duration since the last close, e.g. `P28D`) mint their next round
  the instant the current one is completed (`tasks_complete` calls
  `routines.mint_next_on_complete`).
- **Fixed routines** (`schedule_kind="fixed"`, `schedule_spec` an RRULE,
  e.g. `FREQ=WEEKLY;BYDAY=TU;BYHOUR=19`) and **window routines**
  (`schedule_kind="window"`, a named day window) mint on the scheduler tick
  (`routines.run_tick`, every 15 minutes — see `manifest.py`).
- **Skipped, not left open.** If a round is still open when the next is
  due, the tick closes it `dropped` with a `field="skip"` `TaskEvent`, then
  mints the next. A skip is the signal a routine is failing —
  `routines_list` surfaces a 30-day skip count for exactly this reason.

⚠️ **Window-kind schedules are a stopgap in this chunk.** The schema stores
a named window (`"bedtime"`, say) but resolving names to actual times of
day is out of scope here — every window routine is treated as "daily at a
configured hour" (`routines.WINDOW_STOPGAP_HOUR`), regardless of the name
stored.

## Hand-over: two different things

- Handing over **one round** is the existing `tasks_transfer`/`_accept`/
  `_decline` on that round's own `tasks` row — nothing routine-specific.
- Handing over the **routine itself** — who gets every *future* round — is
  `routines_transfer`/`_accept`/`_decline`, the identical request/accept
  shape ("TCP not UDP") applied to `Routine.pending_owner_id`. Accepting
  also moves the *current open round's* owner, so a hand-over never leaves
  the in-flight round pointed at the old owner.

## What excludes rounds, and why

- `tasks_review` — a round's title is the routine's own title, chosen once,
  not written fresh each cycle; flagging it every cycle is noise nobody
  needs to rule on.
- `dupes.duplicate_pairs` / `dupes.similar_tasks` — every round of the same
  routine shares its title with every other round; without the exclusion a
  recurring chore would permanently "duplicate" its own predecessor.
- `render.py`'s per-domain category sections — a round would otherwise salt
  every domain with the same recurring line every cycle. Active routines get
  their own **Routines** section instead (title, schedule, owner, next due,
  current round).

See `models.py` for the schema (`Routine`, `RoutineStep`, `Task.routine_id`,
`TaskEvent.routine_id`, `TaskDomainTag.routine_id`) and `routines.py` for the
scheduling math and every handler's docstring.

## Absence detection (R5, Wave 2, 2026-09-03)

`absence.py` alerts on silence rather than on data: a routine whose window
closed with no round completed, a `waiting` task past due with no note since,
or a snag unanswered for weeks. Three pure, read-only finding functions
(`missed_routine_windows`/`stale_waiting`/`unanswered_snags`) plus
`reconcile_alerts`, which is the only writer of `AbsenceAlert` (models.py) —
one persisted row per distinct finding, deduped on `(kind, ref, since)` so a
given incident alerts exactly once and resolves itself the moment the
underlying gap closes (a round completes, a note lands, a snag's status
moves). Runs as the second step of the existing 15-minute routines tick
(`routines.run_tick`) — no second scheduler entry. Thresholds are
`tasks` manifest config keys (`absence_waiting_grace_days`,
`absence_snag_unanswered_weeks`).

Surfaced via the `tasks_absence_alerts` MCP tool, not a `system_alerts` axis
— `absence.py`'s module docstring explains why: `tasks` already depends on
`notify.push` -> `notifications` -> `system.alerts`, so either `system` or
`notifications` taking a dependency back on `tasks` would close a cycle
`app/plugin/validate.py` rejects at boot. Alerts push directly via
`notify.push` at the moment a new row is created instead.
