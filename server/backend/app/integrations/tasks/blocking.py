"""What blocks what.

`TaskLink` has carried a `blocked_by` predicate since the schema shipped and
nothing wrote one, so the ledger could not answer its most useful question:
which single task is holding up several others.

Three rules, all of which come from working the backlog rather than from the
data model:

**Only an OPEN blocker blocks.** Completing a blocker clears its dependents
automatically. The alternative — a `#blocked` tag someone has to remember to
remove — produces a backlog full of tasks marked blocked by work that finished
weeks ago, and a person who has learned the marker means nothing.

**A cycle is refused, not stored.** Two tasks each waiting on the other is not
a state a backlog can be in; it is a mistake made one link at a time, and the
only moment it is cheap to catch is the moment it is created.

**When work is blocked, the real task is the unblocking.** This one is a
habit, not an enforcement, and it is the whole point of recording the edge: a
low-priority task gating three high-priority ones tells you the priority is
wrong at least as loudly as it tells you the link was missing. `blockers()`
exists to make that visible — see `most_blocking`.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.integrations.tasks.models import Task, TaskLink

BLOCKED_BY = "blocked_by"
OPEN_STATUSES = ("inbox", "next", "waiting", "scheduled")


class BlockingError(ValueError):
    """A link that would make the graph incoherent."""


def _by_uid(session: Session, uid: str) -> Task:
    task = session.query(Task).filter(Task.uid == uid).one_or_none()
    if task is None:
        raise BlockingError(f"Unknown task uid: {uid}")
    return task


def _edges(session: Session) -> dict[str, set[str]]:
    """uid -> uids it waits on. Every edge, open or not."""
    out: dict[str, set[str]] = {}
    rows = (
        session.query(TaskLink.from_task_id, TaskLink.target_ref)
        .filter(TaskLink.predicate == BLOCKED_BY, TaskLink.target_type == "task")
        .all()
    )
    uids = dict(session.query(Task.id, Task.uid).all())
    for from_id, target in rows:
        out.setdefault(uids.get(from_id, ""), set()).add(target)
    out.pop("", None)
    return out


def _would_cycle(edges: dict[str, set[str]], blocked: str, blocker: str) -> bool:
    """Does `blocker` already wait, transitively, on `blocked`?"""
    seen, stack = set(), [blocker]
    while stack:
        node = stack.pop()
        if node == blocked:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(edges.get(node, ()))
    return False


def add_blocker(session: Session, blocked_uid: str, blocker_uid: str) -> TaskLink:
    """Record that `blocked_uid` cannot start until `blocker_uid` finishes."""
    if blocked_uid == blocker_uid:
        raise BlockingError("A task cannot block itself.")
    blocked, blocker = _by_uid(session, blocked_uid), _by_uid(session, blocker_uid)

    existing = (
        session.query(TaskLink)
        .filter(
            TaskLink.from_task_id == blocked.id,
            TaskLink.predicate == BLOCKED_BY,
            TaskLink.target_ref == blocker.uid,
        )
        .one_or_none()
    )
    if existing is not None:
        return existing

    if _would_cycle(_edges(session), blocked.uid, blocker.uid):
        raise BlockingError(
            f"{blocker_uid} already waits on {blocked_uid}, directly or "
            f"through another task. Adding this would make a cycle, and a "
            f"cycle is two tasks that can never start."
        )

    link = TaskLink(
        from_task_id=blocked.id, target_type="task", target_ref=blocker.uid,
        predicate=BLOCKED_BY, confidence=1.0, derived_by="human",
    )
    session.add(link)
    session.flush()
    return link


def remove_blocker(session: Session, blocked_uid: str, blocker_uid: str) -> int:
    blocked = _by_uid(session, blocked_uid)
    return (
        session.query(TaskLink)
        .filter(
            TaskLink.from_task_id == blocked.id,
            TaskLink.predicate == BLOCKED_BY,
            TaskLink.target_ref == blocker_uid,
        )
        .delete(synchronize_session=False)
    )


def open_blockers(session: Session) -> dict[str, list[str]]:
    """uid -> the uids of its blockers that are still open.

    ⚠️ Filtered on status, deliberately. A task whose only blocker is done is
    not blocked, and it must stop looking blocked without anyone doing
    anything — otherwise the marker decays into noise.
    """
    open_uids = {
        uid for (uid,) in session.query(Task.uid)
        .filter(Task.status.in_(OPEN_STATUSES)).all()
    }
    return {
        uid: sorted(targets & open_uids)
        for uid, targets in _edges(session).items()
        if targets & open_uids
    }


def most_blocking(session: Session, limit: int = 10) -> list[dict]:
    """Blockers ranked by what they are holding up.

    The finding worth surfacing is not "task X is blocked" — it is one task
    gating several, especially when the blocker is the lower priority of the
    two. That is a statement about the priorities as much as the graph.
    """
    blocked = open_blockers(session)
    counts: dict[str, list[str]] = {}
    for uid, blockers in blocked.items():
        for b in blockers:
            counts.setdefault(b, []).append(uid)

    tasks = {t.uid: t for t in session.query(Task).all()}
    rank = {p: i for i, p in enumerate(
        ("highest", "high", "medium", "low", "lowest"))}

    out = []
    for uid, blocking in sorted(counts.items(), key=lambda kv: -len(kv[1])):
        blocker = tasks.get(uid)
        if blocker is None:
            continue
        # True when the blocker matters less, on paper, than what it holds up.
        inverted = any(
            rank.get(tasks[d].priority or "lowest", 9)
            < rank.get(blocker.priority or "lowest", 9)
            for d in blocking if d in tasks
        )
        out.append({
            "uid": uid,
            "title": blocker.title,
            "status": blocker.status,
            "priority": blocker.priority,
            "blocking": blocking,
            "blocking_count": len(blocking),
            "priority_inverted": inverted,
        })
    return out[:limit]
