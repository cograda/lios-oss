# apple_reminders

Two-way bridge to Apple Reminders via EventKit on the client daemon. The
daemon pushes reminder snapshots to `sync.py::sync_from_push`; the server
queues EventKit writes (add/complete) that the daemon executes and acks
(`commands.py`) — see `server/CLAUDE.md`'s Known Issues for why a queued
write is not a completed write ("queued != applied").

## Reminders is an inlet, not a second task store (E chunk 6b, 2026-09-04)

`/reconcile-reminders` used to exist because Apple Reminders and the task
ledger (`app.integrations.tasks`) were two stores of the same information,
kept in line by a person running a command. `tasks/reminders_inlet.py`'s
periodic tick (`reminders_inlet_tick`, every 15 minutes) replaces that by
hand-reconciling nothing: it keeps the two converged automatically.

**The contract:**

1. A reminder that is open and not yet linked to a ledger task becomes one
   (status `inbox`, owner = the reminder's own user, `source="apple_reminders"`).
2. A task that goes `done` **or `dropped`** in the ledger, with its linked
   reminder still open, gets a queued EventKit `complete` command.
3. A reminder the device reports completed, with its linked task not yet
   done, completes that task.

**The deletion rule:** a reminder deleted on the device is indistinguishable
from a completion once it disappears from the daemon's push snapshot
(`sync.py`'s own stale-reaper marks it `completed`, unchanged by this
feature) — so deleting on the device **completes** the linked task, it does
not drop it. Symmetrically, dropping a task in the ledger **completes** its
reminder, it does not delete it — consistent with this package's existing
rule that a reminder is completed, never deleted, so the iCloud Recently
Deleted view keeps everything recoverable.

`Reminder.linked_task_uid` is the durable link (a plain string holding
`tasks.uid`, not a foreign key — cross-package references in this codebase
are by uid, and `tasks` may not import this package's models directly, only
its facade, see `tests/test_capability_boundaries.py`). It is set once, on
capture, and never cleared.

**Where the logic actually lives, and why it's not in this package:** the
inlet tick is `app/integrations/tasks/reminders_inlet.py`, not a module
here, because `tasks` has to be the one that `depends_on` this package
(`reminders.query` / `reminders.write`, both resolved through `facade.py`
below) rather than the other way around — `apple_reminders` already
`provides=["reminders.query"]`, consumed by `system`, consumed by
`notifications`, already consumed by `tasks`; a dependency running the
other direction would close a `depends_on` cycle boot validation rejects.
See `tasks/manifest.py`'s `depends_on` comment and
`tasks/reminders_inlet.py`'s module docstring for the loop-safety argument
(why a task created from a reminder can never dispatch a reminder write,
and why a device-confirmed completion the inlet itself caused can never
re-complete its task) and `tests/test_reminders_inlet.py` for the tests
that pin both.

`facade.py` here is what the inlet actually calls: `open_unlinked`,
`link_task`, `linked_open`, `linked_completed`, `has_pending_complete`,
`dispatch_complete` — the `reminders.write` half added alongside the
pre-existing `reminders.query` (`list_reminders`, `sync`, `pending_writes`,
`drain_pending_commands`, consumed by `system`).

**`backlog_sync.py` (fuzzy vault-note matching against `Task Backlog.md`)
was deleted 2026-09-15**, once `Task Backlog.md` became a strictly one-way,
unconditionally-regenerated render and the ledger became the sole source of
truth — nothing may read that file as input any more. `reminders_inlet.py`
above is its full replacement: an explicit `linked_task_uid` link rather
than difflib fuzzy matching, on a 15-minute tick rather than 30. The
reactive trigger that used to fire `sync_backlogs` after a meaningful
`/api/v1/reminders/push` now fires `reminders_inlet.tick_once` instead (see
`app/api/v1.py::_trigger_reminders_inlet`).
