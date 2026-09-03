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
    CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base

# GTD's someday/maybe is a project *status*, not a separate list.
PROJECT_STATUSES = ("active", "someday", "complete", "dropped")

# `inbox` is the unprocessed capture; `next` is GTD's next-action; `waiting`
# is delegated-and-blocked; `scheduled` is defer_until in the future.
TASK_STATUSES = (
    "inbox", "next", "waiting", "scheduled", "someday", "done", "dropped",
)

PRIORITIES = ("highest", "high", "medium", "low", "lowest")

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

PROGRAM_STATUSES = PROJECT_STATUSES
LINK_TARGETS = ("task", "project", "note", "snag", "domain", "person")


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
        CheckConstraint(
            "(program_id IS NOT NULL)::int + (project_id IS NOT NULL)::int "
            "+ (task_id IS NOT NULL)::int = 1",
            name="ck_task_domain_tags_one_target",
        ),
        UniqueConstraint("domain_id", "program_id", name="uq_task_domain_tags_program"),
        UniqueConstraint("domain_id", "project_id", name="uq_task_domain_tags_project"),
        UniqueConstraint("domain_id", "task_id", name="uq_task_domain_tags_task"),
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
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(20), unique=True, index=True)

    title: Mapped[str] = mapped_column(String(300))
    # The home of the file's existing 14,592 words of task prose; embedded.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), server_default="inbox", index=True)
    priority: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)

    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_projects.id"), nullable=True, index=True,
    )
    domain_id: Mapped[int | None] = mapped_column(
        ForeignKey("domains.id"), nullable=True, index=True,
    )
    # NULL is "nobody", and says so.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True,
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

    # Capture provenance: whatsapp, voice, sweep, manual, apple_reminders.
    # Reminders is a source, not a peer table — it is a write channel with
    # known staleness.
    source: Mapped[str | None] = mapped_column(String(30), nullable=True)

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
