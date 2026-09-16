"""Task and project ledger — the database IS the source of truth.

`vault/Task Backlog.md` is a generated view, re-rendered after every write.
The file already had this architecture: its own header says *"Focus and This
Week are query lenses (subsets of the same list), not separate copies"*, and
it carries three live ```tasks query blocks executed by the Obsidian Tasks
plugin. This moves the source from a markdown list to a table and the lenses
from that plugin to SQL — it does not impose a new shape.

Household-shared, following `snags`: deliberately NO UserOwnedMixin. The house
has one backlog regardless of who captured an item, so `owner_id` is a field,
not a tenancy boundary — and it is nullable, because NULL means "nobody" and
should say so rather than defaulting to whoever ran the import.

Built here rather than as a lios element because `core` has not moved into
lios yet, so an element in that repo cannot be imported by this process. The
schema below is the one designed for `lios/loops/`, unchanged apart from its
address: when core moves, these tables move with it, and relocating them into
their own namespace at that point is one ALTER statement, not a data
migration. Design of record:
`vault/Projects/lios/Plans/tasks-and-projects-2026-08.md`.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String,
    Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import UserOwnedMixin

# GTD's someday/maybe is a project *status*, not a separate list.
PROJECT_STATUSES = ("active", "someday", "complete", "dropped")

# `inbox` is the unprocessed capture; `next` is GTD's next-action; `waiting`
# is delegated-and-blocked; `scheduled` is defer_until in the future.
TASK_STATUSES = (
    "inbox", "next", "waiting", "scheduled", "someday", "done", "dropped",
)

# C2 "runs with prerequisites" (lios#156): a `requires_task_id` reference is
# `satisfied` once the referenced task is `done` OR `dropped` — a skipped
# round (skip_round sets status="dropped") or a plainly cancelled/declined
# one-off task is a signal, not a stuck state the dependent should sit
# behind. There is no separate "declined" task status; `dropped` is the one
# status that already covers both a routine's skip and an ordinary task
# being called off.
PREREQUISITE_SATISFIED_STATUSES = ("done", "dropped")

PRIORITIES = ("highest", "high", "medium", "low", "lowest")

# `kind`/`severity` stay as columns: a household task can still be a `bug`
# (a broken appliance, a snag) or a `feature` (a new household capability
# wanted). `task` is the default and what every pre-existing row is; `chore`
# is platform upkeep that is neither a defect nor new capability. Since
# 2026-09-07 (decision, Alex), lios development items — bugs and features in
# the platform itself — are tracked as GitHub Issues on cograda/lios, not in
# this ledger (reversing the earlier S5.3 "ledger is the register" decision).
KINDS = ("task", "bug", "feature", "chore")

# Applies to bugs and features; NULL for everything else, and for a bug
# nobody has triaged yet.
SEVERITIES = ("critical", "high", "medium", "low")

# Derived from the live file's existing tags: #quick (88), #deep (31).
ENERGY = ("quick", "deep")

# How a link came to exist. A [[wikilink]] a person typed is 1.0 and no model
# ever second-guesses it; an inferred edge must stay distinguishable from a
# stated one forever. The parked `taskgraph` prototype learned this once.
DERIVED_BY = ("human", "deterministic", "llm")

# `part_of` is CONTAINMENT, not sequencing: a sub-loop inside a bigger loop.
# The loops rule (decided 2026-09-01) is that anything with a definition of
# done can be closed, and may hold sub-loops that must close first — so a task
# with an open `part_of` child refuses to complete. `blocked_by` stays the
# *sequencing* relation. Keep them distinct or a week view cannot tell a stuck
# chain from a big job.
LINK_PREDICATES = (
    "blocked_by", "waiting_on", "concerns", "duplicates", "relates_to", "part_of",
    # A person's ruling that two similar-looking tasks are NOT the same one.
    # Stored so tasks_duplicates stops pairing them; survives an index rebuild.
    "distinct_from",
)

# Explicit work queues, adopted from the work `taskdb` tool on 2026-09-01.
# Focus is a subset of week — you cannot focus on something you are not doing
# this week — and it is stored as ONE value where `focus` implies `week`.
# Explicit rather than inferred from priority, because in a mature backlog the
# top two priorities cover most tasks, so "this week" silently becomes
# "everything".
QUEUES = ("week", "focus")

# The Claude flag, split three ways (Alex, 2026-09-02 self-chat): a task an
# agent session should DO, one it should INVESTIGATE and report on, and one
# where the task line itself is the problem and needs FIXING (rewriting,
# splitting, re-filing). One umbrella tag hid which was meant; /youdoit had
# to guess. Stored as ordinary tags so the ledger, the rendered file and the
# command all see the same thing; `#youdoit` rows were migrated to `do`.
CLAUDE_TAG_PREFIX = "#claude/"
CLAUDE_ROLES = ("do", "investigate", "fix")
CLAUDE_TAGS = {role: f"{CLAUDE_TAG_PREFIX}{role}" for role in CLAUDE_ROLES}

PROGRAM_STATUSES = PROJECT_STATUSES
LINK_TARGETS = ("task", "project", "note", "snag", "domain", "person")

# lios#224: `source` was `String(30)`, nullable, with no validation anywhere —
# a live query of the household ledger found 21 distinct strings, including
# session debris like `kickoff-triage`, `found-2026-09-07` and
# `voice memo 2026-09-10`, none matching the vocabulary the model's own
# docstring named. Worse, an unvalidated free-text caller crashed
# `tasks_add` outright: `source="meeting:2026-09-13 Household Systems &
# Weekly Check-in"` (55+ chars) raised `psycopg2.errors.
# StringDataRightTruncation` against this same `String(30)` column. This is
# the single source of truth for the allowed vocabulary — checked at WRITE
# TIME by every writer (`tasks_add`, `tasks_split`, routines, the reminders
# inlet), never enforced as a DB CHECK constraint: a CHECK would have to be
# altered mid-deploy every time the vocabulary legitimately grows, which is
# exactly the kind of migration this column should never need again. The
# column itself stays `String(30)` — the vocabulary was chosen to fit it,
# not the other way around.
TASK_SOURCES = (
    "manual", "meeting", "kickoff", "seed", "seed-whatsapp", "voice", "sweep",
    "apple_reminders", "routine", "split",
    # backlog_import / someday_import / delegated_import: historical only.
    # The one-off markdown importers that wrote these (`app/integrations/
    # tasks/importer.py` + `parse.py`) were deleted 2026-09-15 once the
    # ledger became the sole source of truth (nothing in production may
    # read `Task Backlog.md` any more) — kept here because existing rows
    # still carry these values, not because anything writes them going
    # forward.
    "backlog_import", "someday_import", "delegated_import", "legacy",
)
DEFAULT_TASK_SOURCE = "manual"
# `legacy` is the migration's own catch-all for whatever didn't match a known
# pattern (see the `confirmed_at`/`source` normalisation migration) — never a
# value a writer should pass going forward, but a real value already sitting
# in the column, so it stays in the allowed set rather than being rejected on
# every subsequent read/update of a legacy row.

# Vocabulary ruled by Alex 2026-09-03 (do not re-open): a **loop** is anything
# with a definition of done; a **routine** is a recurring loop template that
# never closes itself; a **round** is one occurrence of a routine, and it
# closes. A round IS a `tasks` row (`Task.routine_id`) — it gets every
# existing lens, queue, block, comment, history and transfer/accept semantic
# for free. There is deliberately no separate occurrence table. See
# `routines.py` for the minting/scheduling logic.
SCHEDULE_KINDS = ("fixed", "interval", "window")


class TaskProgram(Base):
    """The level above project: domains → programs → projects → tasks.

    Added 2026-09-01 from the work `taskdb` design. A program is bounded work
    with a hub note; unlike a project its `done_when` is NULLABLE — Alex's
    rule is that programs *may* have a definition of done, projects *must*,
    tasks and routines *must*. A program with no `done_when` is a standing
    body of work (taskdb's BAU section) and can still hold finite projects.

    Domains are NOT a column here. They are many-to-many via `TaskDomainTag`,
    because real work is cross-cutting and a single-domain parent forces false
    choices (the same reason taskdb made domains tags rather than headings).
    """

    __tablename__ = "task_programs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), server_default="active", index=True)
    # The hub note in the vault. taskdb's rule: no hub, no program.
    note_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Nullable by design — see the class docstring.
    done_when: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True,
    )
    # Transfer is a REQUEST, not a write — see tools.py's tasks_transfer /
    # tasks_accept / tasks_decline. `pending_owner_id` names who has been
    # asked; `owner_id` does not move until they accept. Nullable: NULL means
    # no transfer in flight.
    pending_owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True,
    )
    transfer_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    sort_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class TaskDomainTag(Base):
    """A domain tag on a program, project or task — many-to-many.

    Exactly one of the three target columns is set (enforced by a CHECK), and
    every target is a real foreign key, which a polymorphic `(type, id)` pair
    would not be. `domains` is the household integration's table of areas of
    responsibility (GTD Areas); this does not create domains, only references
    them — a tag naming a domain that does not exist is refused with the list
    of ones that do.
    """

    __tablename__ = "task_domain_tags"
    __table_args__ = (
        # Extended 2026-09-03 (chunk E3) to a fourth target, `routine_id` —
        # a routine may carry domain tags the same way a program/project/task
        # does, following this same many-to-many shape.
        CheckConstraint(
            "(program_id IS NOT NULL)::int + (project_id IS NOT NULL)::int "
            "+ (task_id IS NOT NULL)::int + (routine_id IS NOT NULL)::int = 1",
            name="ck_task_domain_tags_one_target",
        ),
        UniqueConstraint("domain_id", "program_id", name="uq_task_domain_tags_program"),
        UniqueConstraint("domain_id", "project_id", name="uq_task_domain_tags_project"),
        UniqueConstraint("domain_id", "task_id", name="uq_task_domain_tags_task"),
        UniqueConstraint("domain_id", "routine_id", name="uq_task_domain_tags_routine"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain_id: Mapped[int] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), index=True,
    )
    program_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_programs.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_projects.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    routine_id: Mapped[int | None] = mapped_column(
        ForeignKey("routines.id", ondelete="CASCADE"), nullable=True, index=True,
    )


class TaskProject(Base):
    """A GTD project: an outcome needing more than one action.

    `outcome` is the project's definition of done, and under the loops rule a
    project MUST have one — enforced at the tool boundary rather than as NOT
    NULL, because the 2026-08-31 import carried projects with none and a
    constraint would have refused the ledger's own history.
    """

    __tablename__ = "task_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Human-facing stable id, e.g. 'PROJ-0007'. Immutable across renames.
    uid: Mapped[str] = mapped_column(String(20), unique=True, index=True)

    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), server_default="active", index=True)

    domain_id: Mapped[int | None] = mapped_column(
        ForeignKey("domains.id"), nullable=True, index=True,
    )
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True,
    )
    # See Task.pending_owner_id — transfer is a request, not a write.
    pending_owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True,
    )
    transfer_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # The existing [[wikilinked]] vault note, where one exists. 25 of these
    # are already in the file as H2 headings.
    note_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Prose sitting under the section heading that belongs to no single task.
    body_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # File order. Not decoration: rendering by priority instead would reshuffle
    # every section into a diff nobody could review.
    sort_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # GTD's definition of done, in the owner's words.
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The program this project is a stream of. Nullable: a project with no
    # program is taskdb's "ad hoc" — legitimate, and a signal when it starts
    # attracting neighbours.
    program_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_programs.id"), nullable=True, index=True,
    )

    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class Task(Base):
    """One action.

    ⚠️ There is no `parent_id`. The live file contains zero indented
    sub-tasks — hierarchy is expressed only by the H2 project heading — so the
    column would ship with no instances to validate it. Adding it later is one
    nullable column; shipping it now is untested structure.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        # The three query lenses the file already has (Focus, This Week,
        # Sit-down) all filter on status + priority, and the aging questions
        # sort by due date within a domain.
        Index("ix_tasks_status_priority", "status", "priority"),
        Index("ix_tasks_domain_status", "domain_id", "status"),
        CheckConstraint(f"kind IN {KINDS!r}", name="ck_tasks_kind"),
        CheckConstraint(
            f"severity IS NULL OR severity IN {SEVERITIES!r}", name="ck_tasks_severity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(20), unique=True, index=True)

    title: Mapped[str] = mapped_column(String(300))
    # The home of the file's existing 14,592 words of task prose; embedded.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), server_default="inbox", index=True)
    priority: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    # See KINDS / SEVERITIES. `kind` is NOT NULL with a server default so
    # every row that predates the register reads `task` without a backfill.
    kind: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="task", index=True,
    )
    severity: Mapped[str | None] = mapped_column(String(10), nullable=True)

    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_projects.id"), nullable=True, index=True,
    )
    domain_id: Mapped[int | None] = mapped_column(
        ForeignKey("domains.id"), nullable=True, index=True,
    )
    # Set only on a ROUND — one occurrence minted from a routine template.
    # NULL means this task is an ordinary one-off, which is every task that
    # existed before 2026-09-03 chunk E3. See `routines.py`.
    routine_id: Mapped[int | None] = mapped_column(
        ForeignKey("routines.id"), nullable=True, index=True,
    )
    # C2 "runs with prerequisites" (proposal 2026-09-10, agreed 2026-09-11,
    # lios#156): one nullable, single-edge FK — not a graph. Declared on the
    # TASK row (a round IS a `tasks` row) rather than the routine template,
    # because the real cases are day-specific ("bring the bins in" gates
    # THIS Wednesday's "put the bins out", not every Wednesday). Nothing
    # stops an ordinary one-off task from using this too — see the migration
    # docstring. Soft nudge only: a caller reading this resolves it to a
    # computed `{"text", "satisfied"} | null` (see tools.py's `_rows`) and
    # never blocks completion on it.
    requires_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id"), nullable=True, index=True,
    )
    # NULL is "nobody", and says so.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True,
    )
    # Transfer is a REQUEST, not a write ("TCP not UDP" — Alex, 2026-09-03).
    # `tasks_transfer` sets these; `tasks_accept` moves pending -> owner_id
    # and clears both; `tasks_decline` clears both without moving anything.
    # The requester may call `tasks_transfer` again to cancel or redirect a
    # pending request before it is answered.
    pending_owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True,
    )
    transfer_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # GTD context (@home, @errand, @computer). `#sitdown` in the live file is
    # a context in all but name.
    # The H3 heading this task sits under, where a project has them. Hierarchy
    # was assumed to be one level; the PTA section proves it is two.
    # Every tag the file carries. Four of them (#errand/#sitdown/#quick/#deep)
    # also become columns because they are GTD context and energy; the rest
    # have no column and were being silently dropped until the render diff
    # showed them missing.
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}",
    )

    subsection: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sort_order: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    context: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    energy: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # See QUEUES. NULL is "not this week". `queue_set_at` is when a person
    # put it there, so a task that has sat in "week" for a month says so.
    queue: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    queue_set_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    estimate_min: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # GTD start date: invisible before this. Distinct from due_at — the file
    # has no way to express "not yet", which is why things resurface daily.
    defer_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Content hash of the source line, for import idempotency only. NOT
    # identity — `uid` is that, and it survives a re-worded title where this
    # does not. See the d8f2b6c31a47 migration for the trade.
    import_key: Mapped[str | None] = mapped_column(
        String(20), nullable=True, unique=True, index=True,
    )

    # Capture provenance — see TASK_SOURCES above for the validated
    # vocabulary; every writer checks against it, this column stays a plain
    # String(30) with no DB-level CHECK. Reminders is a source, not a peer
    # table — it is a write channel with known staleness.
    source: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # lios#224: the code gate between "an extraction pass suggested this"
    # and "this is a live, active task". NULL means unconfirmed — written by
    # extraction/suggestion paths (today: `/harvest`'s `tasks_add(confirmed=
    # False, ...)` calls) — and every "active" read (tasks_query's default,
    # absence alerts, the rendered backlog's main sections, the Loops app's
    # default lenses) excludes it. Human-initiated `tasks_add` calls default
    # `confirmed=True`, stamping this immediately, same as `created_at`.
    # `tasks_confirm` is the only thing that sets this on an existing row;
    # `tasks_decline` is the dismiss path for a row that never gets one.
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )

    # ⚠️ NULLABLE, and that is the design. For the 239 imported items the
    # creation date is unrecoverable (the vault is gitignored and
    # Drive-synced; only 28% carry any date). Stamping the import date would
    # manufacture a fact and quietly corrupt every aging metric built on it.
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )


class TaskLink(Base):
    """Typed relationships, polymorphic on the far side."""

    __tablename__ = "task_links"
    __table_args__ = (
        Index("ix_task_links_from_predicate", "from_task_id", "predicate"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True,
    )
    target_type: Mapped[str] = mapped_column(String(20))
    # A uid (TASK-0042, SNAG-0007) or a vault path — deliberately not an FK,
    # because the far side may live outside this schema or outside the DB.
    target_ref: Mapped[str] = mapped_column(String(500), index=True)
    predicate: Mapped[str] = mapped_column(String(20))

    # 1.0 for human-typed. An inferred edge must never be mistaken for a
    # stated one — hence both of these, always populated.
    confidence: Mapped[float] = mapped_column(Float, server_default="1.0")
    derived_by: Mapped[str] = mapped_column(String(30), server_default="human")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )


class TaskComment(Base):
    """Append-only. Never edited, never deleted."""

    __tablename__ = "task_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True,
    )
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True,
    )
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )


class TaskEvent(Base):
    """Every status transition, and the only table that can answer the
    questions the markdown file structurally cannot: what has been open
    longest, what closed last month, which domain accumulates faster than it
    drains.

    Also the home of sweep records: `last-reviewed` is a property of a *sweep*,
    not of a file, so the rendered file's frontmatter is computed from the
    latest one rather than stamped onto it.
    """

    __tablename__ = "task_events"
    __table_args__ = (
        Index("ix_task_events_task_at", "task_id", "at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # NULLABLE: a sweep is a review of the whole backlog and belongs to no
    # single task. `to_status='swept'` with a null task_id is that record, and
    # it is where the rendered file's `last-reviewed` comes from.
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    # Set on routine-level events (a routine's own transfer request/accept/
    # decline) where there is no single round to attach to, and ALSO set
    # alongside `task_id` on a round's 'minted'/'skip' events, so a routine's
    # whole history is one query away without joining through `tasks`. NULL
    # for every event that predates chunk E3 (2026-09-03) and for ordinary,
    # non-round tasks.
    routine_id: Mapped[int | None] = mapped_column(
        ForeignKey("routines.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True,
    )
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Field-level history (2026-09-01, from taskdb's `task_history`). A status
    # change leaves `field` NULL and uses from/to_status as before; any other
    # change sets `field` and the old/new values as text, with `to_status`
    # carrying the (unchanged) status so the column stays NOT NULL. This is
    # what lets a week review separate work that got done from work that got
    # tidied.
    field: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)


class Routine(Base):
    """A recurring loop TEMPLATE. It never closes itself — each occurrence is
    a **round**, a real `tasks` row with `routine_id` set (see `Task`). Only
    the next round exists as an open row at any time; `routines.py` mints it
    when the previous one closes (interval routines) or when the schedule
    fires (fixed/window routines), and drops a still-open round `dropped`
    with a `skipped` event before minting the next — a skip is the signal a
    routine is failing, not a state the ledger stays in silently.

    `done_when` is required (unlike `TaskProgram.done_when`, which is
    nullable): under the loops rule tasks and routines MUST have a
    definition of done, and a routine's `done_when` is copied onto every
    round it mints.

    Household-shared, following every other table in this package:
    `default_owner_id` names who takes each new round, not a tenancy
    boundary. Hand-over of a *round* uses the existing `tasks_transfer` on
    that round's own row; hand-over of the *routine* — who gets every future
    round — is `routines_transfer`/`_accept`/`_decline` here, mirroring the
    task-level request/accept semantics exactly ("TCP not UDP").
    """

    __tablename__ = "routines"
    __table_args__ = (
        CheckConstraint(
            f"schedule_kind IN {SCHEDULE_KINDS!r}", name="ck_routines_schedule_kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    done_when: Mapped[str] = mapped_column(Text)

    default_owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True,
    )
    # Transfer of the ROUTINE itself — see the class docstring. Same
    # request/accept shape as Task.pending_owner_id.
    pending_owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True,
    )
    transfer_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    program_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_programs.id"), nullable=True, index=True,
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_projects.id"), nullable=True, index=True,
    )

    # 'fixed': an RRULE string (e.g. "FREQ=WEEKLY;BYDAY=TU;BYHOUR=19").
    # 'interval': an ISO-8601 duration since the last close (e.g. "P28D").
    # 'window': a named day window (e.g. "bedtime") — resolution is out of
    # scope for this chunk; `routines.py` treats every window as daily at a
    # configured hour and says so at every call site that does it.
    schedule_kind: Mapped[str] = mapped_column(String(10))
    schedule_spec: Mapped[str] = mapped_column(Text)

    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )


class RoutineStep(Base):
    """One ordered step of a routine's process, copied into a round's
    description as a checklist when minted (see `routines.mint_round`).
    Steps do not own — only the routine has an owner."""

    __tablename__ = "routine_steps"
    __table_args__ = (
        UniqueConstraint("routine_id", "ord", name="uq_routine_steps_routine_ord"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    routine_id: Mapped[int] = mapped_column(
        ForeignKey("routines.id", ondelete="CASCADE"), index=True,
    )
    ord: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)


# Absence-detection finding kinds (R5, Wave 2). Stable strings — they're half
# of the dedup key (`AbsenceAlert.dedup_key`), so a rename here is a silent
# re-alert on every currently-open finding of that kind, not just a rename.
ABSENCE_KINDS = ("routine_window", "waiting", "snag")


class AbsenceAlert(Base):
    """One persisted "something didn't happen" alert (R5, absence detection).

    Household-shared, following every other table in this package AND
    `notifications.NotificationSend` (the closest existing analogue — a
    dedup ledger for alerts, not tenancy data): deliberately NO
    `UserOwnedMixin`. `owner_id` is a plain nullable FK because an
    unanswered snag has no single owner (`Snag` itself carries no
    `UserOwnedMixin` either) and would have nothing valid to put in a
    NOT NULL column — the same reasoning `NotificationSend.user_id`'s
    docstring gives for its own nullable owner column.

    **The dedup mechanism.** `dedup_key` identifies one distinct finding —
    `absence:<kind>:<ref>:<since-iso>` — and the partial unique index below
    allows at most one *open* (`resolved_at IS NULL`) row per key, the same
    shape as `NotificationSend`'s open-fingerprint index. `since` is part of
    the key deliberately: a routine's *next* missed window, or a waiting
    item's *next* due date, is a new finding, not a resend of the old one.
    `absence.reconcile_alerts()` is the only writer — see its module
    docstring for the create/resolve lifecycle.
    """

    __tablename__ = "absence_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String(255), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    ref: Mapped[str] = mapped_column(String(20), index=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True,
    )
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    # NULL means "still open" — the normal case. Set the moment a tick no
    # longer sees this dedup key among current findings (see
    # `absence.reconcile_alerts`), which is also what closes the row out for
    # dedup purposes, same as `NotificationSend.resolved_at`.
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )

    __table_args__ = (
        # At most one OPEN alert per dedup key. Partial (WHERE resolved_at IS
        # NULL) so resolved history is unconstrained, same shape as
        # `NotificationSend.__table_args__`'s open-fingerprint index.
        Index(
            "ix_absence_alerts_open_dedup_key",
            "dedup_key",
            unique=True,
            postgresql_where=(resolved_at.is_(None)),
        ),
    )


class IntakeMarker(UserOwnedMixin, Base):
    """One row per caller: the high-water mark for `tasks_intake_candidates`.

    Unlike the rest of this package, this IS `UserOwnedMixin` — a per-user
    watermark is exactly the tenancy shape that mixin is for (see its own
    docstring), and taking it gets `intake_markers` classified for free in
    `app/privacy.py::user_columns_for`/`user_owned_models()` and swept by
    `tests/test_user_scoping.py`'s generic per-model probes with no bespoke
    entry needed there.

    `user_id` doubles as the primary key — overriding the mixin's plain
    (non-PK) column — because there is exactly one marker per caller, ever;
    a separate surrogate `id` would just be a second, pointless uniqueness
    constraint on the same column. `seen_until` is the boundary
    `tasks_intake_candidates` reads by default and `tasks_intake_mark`
    advances; it is NOT NULL because a caller who has never called intake
    has no marker row at all (absent -> the tool's own 24h-ago default), not
    a row with a null boundary.
    """

    __tablename__ = "intake_markers"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True,
    )
    seen_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )
