from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, TaskSpec

MANIFEST = IntegrationManifest(
    name="tasks",
    display_name="Tasks",
    version="1.0.0",
    type="capability",
    description=(
        "Task and project ledger (DB is source of truth); Task Backlog.md "
        "becomes a generated one-way view of it."
    ),
    icon="ListChecks",
    models=[
        "TaskProgram", "TaskDomainTag", "TaskProject", "Task", "TaskLink",
        "TaskComment", "TaskEvent", "Routine", "RoutineStep", "AbsenceAlert",
        "IntakeMarker",
    ],
    # Open tasks are embedded (title + description) so tasks_duplicates can
    # compare them for free and household search finds them. See dupes.py.
    # Rounds (routine_id set) are deliberately excluded — see routines.py.
    embedding_sources=["task"],
    reads_from=[],
    writes_to=[],
    # Capture is user-gated and rendering is write-triggered; nothing to poll
    # on a cron *sync*. `routines_tick` below is a background task, not a
    # sync — it mints/skips rounds, it doesn't pull external data.
    schedule=None,
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    staleness_probe=None,
    background_tasks=[
        TaskSpec(
            name="tasks_routines_tick",
            target="app.integrations.tasks.routines:run_tick",
            kind="cron",
            # Every 15 minutes — same cadence as notifications' alert sweep.
            # The schedules this reads are hour/day-scale, so nothing here
            # needs tighter, and a looser cadence delays a due round showing
            # up in the backlog by up to that long.
            cron="*/15 * * * *",
        ),
        # The reminders inlet (E chunk 6b, 2026-09-04) — retires
        # /reconcile-reminders. Same cadence as `tasks_routines_tick` above,
        # hooked as its own cron entry on the existing scheduler rather than
        # folded into that function (a broken inlet tick must not stop
        # rounds minting, and vice versa — `run_tick`'s own try/except
        # writes SyncState per-tick, so the two must stay independent
        # entries to fail independently). See reminders_inlet.py's module
        # docstring for the full contract and the loop-safety argument.
        TaskSpec(
            name="reminders_inlet_tick",
            target="app.integrations.tasks.reminders_inlet:run_tick",
            kind="cron",
            cron="*/15 * * * *",
        ),
    ],
    routes=[],
    config_schema={
        # Absence detection (R5, Wave 2) — see app/integrations/tasks/absence.py.
        "absence_waiting_grace_days": ConfigFieldSpec(
            type="int",
            required=False,
            default=3,
            description=(
                "Days a `waiting` task may sit past its due date, with no "
                "note added since, before it becomes an absence finding. "
                "Routine windows need no threshold (the window closing is "
                "the event itself)."
            ),
        ),
        "absence_snag_unanswered_weeks": ConfigFieldSpec(
            type="int",
            required=False,
            default=3,
            description=(
                "Weeks a snag may sit `open`/`reported` (no trade response) "
                "before it becomes an absence finding."
            ),
        ),
        "absence_burst_threshold": ConfigFieldSpec(
            type="int",
            required=False,
            default=5,
            description=(
                "If a single reconcile pass creates more new absence "
                "findings than this, send one combined digest push instead "
                "of one push per finding. Guards against a backfill burst — "
                "e.g. lowering absence_snag_unanswered_weeks, or a newly "
                "shipped absence check, retroactively crossing the "
                "threshold for a whole pre-existing backlog at once (as "
                "happened on 2026-09-04, unanswered_snags' first run: 172 "
                "individual pushes in one day). Findings below the "
                "threshold still push individually and immediately, "
                "unchanged."
            ),
        ),
        # Intake (S5, 2026-09-07) — see app/integrations/tasks/intake.py.
        "intake_match_threshold": ConfigFieldSpec(
            type="float",
            required=False,
            default=0.78,
            description=(
                "Cosine-similarity floor (0-1) above which an intake "
                "candidate's stored embedding is considered a match against "
                "an open task's. Lower than tasks_duplicates' 0.80 on "
                "purpose: intake is comparing a raw message against a task "
                "description, not two task descriptions, so the honest "
                "same-thing score sits lower — see intake.py's module "
                "docstring for the measurement this default was set from."
            ),
        ),
    },
    # tasks_nudge/tasks_transfer/tasks_accept/tasks_decline (and the routine-
    # level equivalents in routines.py) send a best-effort push via
    # `notifications`' ad-hoc send() path — see tools.py's `_notify`.
    # Resolved via `get_capability`, never a direct import of the package.
    #
    # reminders.query / reminders.write (E chunk 6b, 2026-09-04): the
    # reminders-inlet tick (`reminders_inlet.py`) reads and writes Apple
    # Reminders through apple_reminders' facade. The dependency has to run
    # this direction, not the other way — `apple_reminders` already
    # `provides=["reminders.query"]`, which `system` consumes
    # (`system.alerts`), which `notifications` consumes (`notify.push`),
    # which `tasks` already consumed here. Had apple_reminders instead
    # declared `depends_on=["tasks.*"]`, that chain closes into a cycle
    # (apple_reminders -> tasks -> notifications -> system ->
    # apple_reminders) that boot validation rejects — the same "the package
    # that pushes has to be the one that pulls" shape as `inbox` pulling
    # WhatsApp notes rather than whatsapp pushing to it.
    #
    # snags.query (R5, absence detection): `absence.unanswered_snags` reads
    # the snag register through `snags`' facade rather than importing its
    # models directly (`tests/test_capability_boundaries.py`). Safe in this
    # direction only — `snags` depends on nothing that depends on `tasks`,
    # unlike `system`/`notifications`: this package ALREADY depends on
    # `notify.push` (above), which depends on `system.alerts`, which is why
    # absence findings surface via a dedicated tool
    # (`tasks_absence_alerts`) rather than as a `system_alerts` axis —
    # `system` depending on anything `tasks` provides would close
    # `system -> tasks -> notifications -> system`, the exact cycle shape
    # `app/plugin/validate.py::_check_dependency_graph` rejects at boot (see
    # that function, and the `system` manifest's own note on why it can't
    # depend on `inbox.query` for the identical reason).
    #
    # mail.query / whatsapp.query (S5, 2026-09-07): `tasks_intake_candidates`
    # reads new mail/WhatsApp messages through google_mail's and whatsapp's
    # facades, the same rule as everything else in this list — never a
    # direct import of another integration's internals. Safe in this
    # direction only: both declare `depends_on=[]`, so nothing closes a
    # cycle back through `notify.push` -> `system.alerts` the way an
    # apple_reminders-style reverse dependency would.
    #
    # inbox.query (lios#159): `tasks_intake_candidates` also reads the
    # caller's own pending inbox captures through `inbox`'s facade. Safe in
    # this direction only — `inbox` depends on `notify.push`/`notify.email`/
    # `whatsapp.query`/`corpus.ingest`/`transcription.audio`/`vision.image`,
    # none of which depend on anything `tasks` provides, so this does not
    # close a cycle back through `tasks -> notify.push -> notifications ->
    # system.alerts` (`inbox` never depends on `system.alerts`).
    depends_on=[
        "notify.push", "reminders.query", "reminders.write", "snags.query",
        "mail.query", "whatsapp.query", "inbox.query",
    ],
)
