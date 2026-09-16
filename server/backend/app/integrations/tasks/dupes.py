"""Duplicate detection and merging for the task ledger.

Ported from the work `taskdb` tool on 2026-09-01, where the duplicates view was
the single most useful pass in a backlog sweep: two lines written weeks apart
about the same thing, neither of which gets done because each looks like the
other's responsibility.

🔑 **Tasks ride the platform's embedding pipeline rather than a private index.**
taskdb kept its own SQLite blob table and its own bge-small model. Here a task
is one more `embedding_sources` entry (`task`), so:

  - comparing costs nothing — `EmbeddingService.near_duplicates` is linear
    algebra over vectors already in Postgres, no API call per check;
  - the household's `search_semantic` / `system_search_everything` find open
    tasks for free, which nothing asked for and everyone will use;
  - the queue processor (every five minutes) does the embedding, so a request
    never blocks on Gemini. The cost is honesty: `tasks_duplicates` reports how
    many open tasks are still waiting to be indexed, because an empty pair
    list from a half-built index reads as "no duplicates", which is a lie.

⚠️ **A closed task's vector is deleted, not kept.** Enqueueing empty content
removes the row (see `EmbeddingService.enqueue`). Done work has no business in
a duplicates view, and a search that surfaces tasks finished months ago as if
they were open is worse than one that never surfaces tasks at all.

Merging is a ledger operation, not a delete: the dropped task's text is folded
into the kept one's description, its dependents are re-pointed, its sub-tasks
re-parented, and it is marked `dropped` with a `duplicates` link back — so
`tasks_history` can still say what happened to TASK-0042.
"""

from __future__ import annotations

import difflib
import json
import re
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.integrations.tasks.models import Task, TaskEvent, TaskLink, TaskProject
from app.tools import CustomTool, ToolAnnotations

EMBEDDING_SOURCE = "task"
DISTINCT_FROM = "distinct_from"
DUPLICATES = "duplicates"
OPEN_STATUSES = ("inbox", "next", "waiting", "scheduled")
# 0.80, not 0.85: on the live ledger (2026-09-02) the true duplicates sat at
# 0.82-0.84 (see MEASURED_DUPLICATE_RANGE below) — two lines about the same
# FNIRSI return, two about the same gate lock — mixed with related-but-distinct
# pairs at the same scores. A person rules on both kinds in seconds; a
# threshold that hides the first kind to spare them the second is the wrong
# trade.
DEFAULT_THRESHOLD = 0.80
# The measured duplicate cluster on the live ledger, 2026-09-02 (lios#223 made
# this a constant rather than a comment-only note, so a template describing
# the band can cite it instead of retyping the numbers).
MEASURED_DUPLICATE_RANGE = (0.82, 0.84)
# Wider than DEFAULT_THRESHOLD, so related-but-distinct pairs surface too
# (`/tunetasks` Pass B) without being called duplicates. Below this, a pair
# isn't worth surfacing at all.
RELATED_WORK_THRESHOLD = 0.75
# Title-only near-identity, independent of the embedding: two lines that say
# the same thing in nearly the same words, which a description on one side
# can dilute out of the semantic score.
TITLE_THRESHOLD = 0.6


def embed_text(task: Task) -> str:
    """Title plus description. The title alone is what most duplicates share,
    but two vague titles with distinct descriptions are NOT the same task, and
    the description is what tells them apart."""
    return "\n\n".join(p for p in (task.title, task.description) if p and p.strip())


def enqueue(session: Session, task: Task) -> bool:
    """Keep the vector in step with the task. Open → (re)embed; closed →
    delete. Dedupes by content hash, so calling it on every write is cheap."""
    from app.services.embedding import EmbeddingService

    content = embed_text(task) if task.status in OPEN_STATUSES else ""
    return EmbeddingService.enqueue(
        session, source=EMBEDDING_SOURCE, source_id=task.uid, content=content,
    )


def enqueue_open(session: Session) -> int:
    """Backfill: every open task not yet embedded at its current text. Returns
    how many were actually queued (unchanged ones are skipped)."""
    from app.services.embedding import EmbeddingService

    tasks = session.query(Task).filter(Task.status.in_(OPEN_STATUSES)).all()
    items = [(EMBEDDING_SOURCE, t.uid, embed_text(t), None) for t in tasks if embed_text(t).strip()]
    return EmbeddingService.enqueue_batch(session, items)


def pending_count(session: Session) -> int:
    from app.services.embedding import EmbeddingQueue

    return (
        session.query(EmbeddingQueue)
        .filter(EmbeddingQueue.source == EMBEDDING_SOURCE, EmbeddingQueue.status.in_(("pending", "processing")))
        .count()
    )


def _distinct_pairs(session: Session) -> set[tuple[str, str]]:
    """Pairs a person has ruled 'not a duplicate'. Stored as a link so the
    ruling survives a rebuild of the index — taskdb lost these on reload."""
    uids = dict(session.query(Task.id, Task.uid).all())
    rows = (
        session.query(TaskLink.from_task_id, TaskLink.target_ref)
        .filter(TaskLink.predicate == DISTINCT_FROM, TaskLink.target_type == "task")
        .all()
    )
    return {tuple(sorted((uids.get(a, ""), b))) for a, b in rows}


# ─── the second signal: near-identical titles ──────────────────────────────

_STOP = {"the", "a", "an", "to", "and", "of", "for", "in", "on", "with", "at", "by", "our", "my", "it"}


def _tokens(title: str) -> list[str]:
    text = re.sub(r"\*\*|\[\[|\]\]|`", " ", title or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return [t for t in text.split() if t and t not in _STOP]


def title_similarity(a: str, b: str) -> float:
    """0–1. The larger of token overlap (order-free) and sequence ratio
    (order-aware), so both 'fit the gate lock' / 'fit lock to the gate' and a
    line retyped with one word changed score high."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    jaccard = len(sa & sb) / len(sa | sb)
    ratio = difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    return round(max(jaccard, ratio), 3)


def lexical_pairs(tasks: list[Task], threshold: float = TITLE_THRESHOLD) -> dict[tuple[str, str], float]:
    """Every open pair whose titles are nearly the same words. O(n²) over a
    few hundred short strings — a few tens of thousands of comparisons, well
    under the cost of one embedding call."""
    out: dict[tuple[str, str], float] = {}
    for i in range(len(tasks)):
        for j in range(i + 1, len(tasks)):
            sim = title_similarity(tasks[i].title, tasks[j].title)
            if sim >= threshold:
                out[tuple(sorted((tasks[i].uid, tasks[j].uid)))] = sim
    return out


def duplicate_pairs(
    session: Session, threshold: float = DEFAULT_THRESHOLD, limit: int = 60,
    owner_id: int | None = None,
) -> list[dict]:
    """Pairs of open tasks that look like the same task.

    `owner_id` keeps the pairs that touch one of that person's loops — EITHER
    side, not both. A duplicate between my line and Alex's is exactly the kind
    that goes unnoticed by both of us, and either of us can rule on it; a pair
    between two of Alex's lines is his to tidy. Absent, the household's pairs
    (the Loops summary strip showed Sam that count, 2026-09-06).
    """
    from app.services.embedding import EmbeddingService

    # `exclude_prefixes=[]`: the vault default excludes `Daily Notes/` and
    # friends, which mean nothing for TASK-nnnn ids and would silently drop
    # nothing — but passing the default would tie this to another source's
    # folder layout.
    raw = EmbeddingService.near_duplicates(
        session, EMBEDDING_SOURCE, threshold=threshold, limit=limit * 2, exclude_prefixes=[],
    )
    # `routine_id IS NOT NULL` rows (rounds) are excluded: a recurring round
    # would otherwise permanently "duplicate" its own predecessor and every
    # future sibling, every single cycle — a finding nobody needs to rule on
    # because the ledger already knows they're the same thing on purpose.
    open_list = session.query(Task).filter(Task.status.in_(OPEN_STATUSES), Task.routine_id.is_(None)).all()
    open_tasks = {t.uid: t for t in open_list}
    distinct = _distinct_pairs(session)

    # Two signals, one list. `signal` says which found the pair; a pair both
    # found is the strongest kind.
    found: dict[tuple[str, str], dict] = {}
    for pair in raw:
        a, b = sorted((pair["a"], pair["b"]))
        # Both sides must still be open: a stale vector for a task closed since
        # the last processor run must not put finished work in the view.
        if a not in open_tasks or b not in open_tasks or (a, b) in distinct:
            continue
        found[(a, b)] = {"score": round(float(pair["score"]), 3), "signal": "semantic"}
    for (a, b), sim in lexical_pairs(open_list).items():
        if (a, b) in distinct:
            continue
        if (a, b) in found:
            found[(a, b)]["signal"] = "both"
            found[(a, b)]["score"] = max(found[(a, b)]["score"], sim)
        else:
            found[(a, b)] = {"score": sim, "signal": "title"}

    out = []
    for (a, b), hit in found.items():
        ta, tb = open_tasks[a], open_tasks[b]
        if owner_id is not None and owner_id not in (ta.owner_id, tb.owner_id):
            continue
        out.append({
            "a": a, "b": b, "score": hit["score"], "signal": hit["signal"],
            "a_title": ta.title, "b_title": tb.title,
            # Same project is a strong prior: the review shows those first.
            "same_project": ta.project_id is not None and ta.project_id == tb.project_id,
        })
    out.sort(key=lambda p: (not p["same_project"], p["signal"] != "both", -p["score"]))
    return out[:limit]


def similar_tasks(
    session: Session, uid: str, limit: int = 8, *, owner_id: int | None = None,
) -> list[dict]:
    """The tasks most like one task — the question a person asks when they
    KNOW a duplicate exists and the pair view has not surfaced it. Semantic
    neighbours from the stored vector (free) plus title similarity, open
    tasks only, distinct rulings honoured.

    `owner_id` narrows the CANDIDATES to that person's open tasks plus the
    unowned ones (an imported line nobody has claimed could be anyone's
    duplicate); None is everyone's. The subject task itself is looked up
    unscoped — a uid is an explicit ask."""
    from app.services.embedding import EmbeddingService

    task = _get(session, uid)
    q = session.query(Task).filter(
        Task.status.in_(OPEN_STATUSES), Task.uid != uid, Task.routine_id.is_(None),
    )
    if owner_id is not None:
        q = q.filter(or_(Task.owner_id == owner_id, Task.owner_id.is_(None)))
    open_list = q.all()
    by_uid = {t.uid: t for t in open_list}
    distinct = _distinct_pairs(session)
    hits: dict[str, dict] = {}
    for h in EmbeddingService.similar_to(session, EMBEDDING_SOURCE, uid, limit=limit * 2, min_score=0.6):
        other = h["source_id"]
        if other in by_uid and tuple(sorted((uid, other))) not in distinct:
            hits[other] = {"score": round(float(h["score"]), 3), "signal": "semantic"}
    for t in open_list:
        sim = title_similarity(task.title, t.title)
        if sim >= 0.45 and tuple(sorted((uid, t.uid))) not in distinct:
            if t.uid in hits:
                hits[t.uid]["signal"] = "both"
                hits[t.uid]["score"] = max(hits[t.uid]["score"], sim)
            else:
                hits[t.uid] = {"score": sim, "signal": "title"}
    projects = {p.id: p.title for p in session.query(TaskProject).all()}
    out = [
        {"uid": u, "title": by_uid[u].title, "project": projects.get(by_uid[u].project_id),
         "score": h["score"], "signal": h["signal"]}
        for u, h in hits.items()
    ]
    out.sort(key=lambda r: (r["signal"] != "both", -r["score"]))
    return out[:limit]


# ─── handlers ──────────────────────────────────────────────────────────────


def tasks_duplicates_handler(session: Session, args: dict) -> str:
    threshold = float(args.get("threshold", DEFAULT_THRESHOLD))
    if not 0.5 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0.5 and 1.0")
    queued = enqueue_open(session)
    session.commit()
    from app.integrations.tasks.tools import _scope_owner  # noqa: PLC0415 - avoid import cycle
    owner_id = _scope_owner(session, args)  # the caller's by default; "household" widens
    pairs = duplicate_pairs(session, threshold, limit=min(int(args.get("limit", 60)), 200), owner_id=owner_id)
    # `pending_index` and `open_tasks` stay household-wide on purpose: an
    # un-indexed task ANYONE owns can still form a pair with one of mine, so
    # the honest "this list is a floor" caveat is the whole index's.
    pending = pending_count(session)
    open_count = session.query(Task).filter(Task.status.in_(OPEN_STATUSES), Task.routine_id.is_(None)).count()
    return json.dumps({
        "threshold": threshold,
        "pairs": pairs,
        "pair_count": len(pairs),
        "open_tasks": open_count,
        # The number that keeps an empty list honest.
        "pending_index": pending,
        "queued_now": queued,
        "note": (
            "A score is 'worth a human look', not 'the same task'. "
            + (f"{pending} open tasks are not indexed yet — the embedding processor runs "
               "every five minutes; check again after it has." if pending else
               "Every open task is indexed.")
        ),
    })


def tasks_similar_handler(session: Session, args: dict) -> str:
    from app.integrations.tasks.tools import _scope_owner  # noqa: PLC0415 - avoid import cycle

    limit = min(int(args.get("limit", 8)), 30)
    owner_id = _scope_owner(session, args)  # the caller's by default; "household" widens
    return json.dumps({
        "uid": args["uid"],
        "similar": similar_tasks(session, args["uid"], limit, owner_id=owner_id),
    })


def _get(session: Session, uid: str) -> Task:
    task = session.query(Task).filter(Task.uid == uid).one_or_none()
    if task is None:
        raise ValueError(f"Unknown task uid: {uid}")
    return task


def merge(session: Session, keep: Task, drop: Task, title: str | None = None) -> dict:
    """Fold `drop` into `keep`. See the module docstring for what moves."""
    if keep.id == drop.id:
        raise ValueError("keep and drop are the same task")
    if drop.status not in OPEN_STATUSES:
        raise ValueError(f"{drop.uid} is already {drop.status}; nothing to merge")
    now = datetime.now(timezone.utc)
    actor = current_user_id()

    if title and title.strip() and title.strip() != keep.title:
        session.add(TaskEvent(task_id=keep.id, from_status=None, to_status=keep.status,
                              actor_id=actor, field="title", old_value=keep.title, new_value=title.strip()))
        keep.title = title.strip()[:300]

    # The dropped line's words are kept: a merge that discards text is a delete
    # with a friendlier name.
    folded = f"Merged from {drop.uid}: {drop.title}"
    if drop.description and drop.description.strip():
        folded += "\n" + drop.description.strip()
    new_desc = (keep.description.rstrip() + "\n\n" + folded) if keep.description else folded
    session.add(TaskEvent(task_id=keep.id, from_status=None, to_status=keep.status,
                          actor_id=actor, field="description", old_value=keep.description, new_value=new_desc))
    keep.description = new_desc

    # Anything that waited on, or was part of, the dropped task now points at
    # the kept one — except links from the kept task to itself, which are
    # dropped rather than stored as a self-edge.
    repointed = 0
    for link in session.query(TaskLink).filter(
        TaskLink.target_type == "task", TaskLink.target_ref == drop.uid,
    ).all():
        if link.from_task_id == keep.id:
            session.delete(link)
        else:
            link.target_ref = keep.uid
            repointed += 1
    # Blockers of the dropped task become blockers of the kept one.
    for link in session.query(TaskLink).filter(
        TaskLink.from_task_id == drop.id, TaskLink.predicate.in_(("blocked_by", "waiting_on")),
    ).all():
        if link.target_ref == keep.uid:
            session.delete(link)
            continue
        exists = session.query(TaskLink).filter(
            TaskLink.from_task_id == keep.id, TaskLink.predicate == link.predicate,
            TaskLink.target_ref == link.target_ref,
        ).first()
        if exists:
            session.delete(link)
        else:
            link.from_task_id = keep.id
            repointed += 1

    # Queue: the merged task inherits the stronger commitment.
    rank = {None: 0, "week": 1, "focus": 2}
    if rank.get(drop.queue, 0) > rank.get(keep.queue, 0):
        session.add(TaskEvent(task_id=keep.id, from_status=None, to_status=keep.status,
                              actor_id=actor, field="queue", old_value=keep.queue, new_value=drop.queue))
        keep.queue, keep.queue_set_at = drop.queue, now

    session.add(TaskEvent(task_id=drop.id, from_status=drop.status, to_status="dropped",
                          actor_id=actor, note=f"merged into {keep.uid}"))
    drop.status = "dropped"
    drop.completed_at = now
    session.add(TaskLink(from_task_id=drop.id, target_type="task", target_ref=keep.uid,
                         predicate=DUPLICATES, confidence=1.0, derived_by="human"))
    enqueue(session, keep)
    enqueue(session, drop)
    return {"kept": keep.uid, "dropped": drop.uid, "links_repointed": repointed}


def mark_distinct(session: Session, a: Task, b: Task) -> None:
    if a.id == b.id:
        raise ValueError("a task is not distinct from itself")
    if tuple(sorted((a.uid, b.uid))) in _distinct_pairs(session):
        return
    session.add(TaskLink(from_task_id=a.id, target_type="task", target_ref=b.uid,
                         predicate=DISTINCT_FROM, confidence=1.0, derived_by="human"))


def tasks_merge_handler(session: Session, args: dict) -> str:
    from app.integrations.tasks.tools import _render, _rows

    action = args.get("action", "merge")
    if action == "distinct":
        a, b = _get(session, args["keep"]), _get(session, args["drop"])
        mark_distinct(session, a, b)
        session.commit()
        return json.dumps({"distinct": sorted((a.uid, b.uid))})
    if action != "merge":
        raise ValueError("action must be 'merge' or 'distinct'")
    keep, drop = _get(session, args["keep"]), _get(session, args["drop"])
    result = merge(session, keep, drop, args.get("title"))
    session.commit()
    _render(session)
    result["task"] = _rows(session, [keep])[0]
    return json.dumps(result)


def dupes_tools() -> list[dict]:
    return [
        CustomTool(
            name="tasks_duplicates",
            description=(
                "Pairs of open tasks that look like the same task, by semantic "
                "similarity of title and description. Queues any un-indexed open "
                "task for embedding first and reports how many are still pending, "
                "so an empty list from a half-built index is never mistaken for "
                "'no duplicates'. Pairs a person has marked distinct (tasks_merge "
                "action=distinct) are excluded permanently. Read-only apart from "
                "the queueing. `owner` keeps the pairs touching one person's "
                "loops (either side); it defaults to yours, and 'household' "
                "is everyone's. pending_index "
                "is always the whole index's."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threshold": {"type": "number", "default": DEFAULT_THRESHOLD,
                                  "description": "Semantic similarity floor, 0.5–1.0. Lower finds more, with more noise. Near-identical titles are paired regardless."},
                    "limit": {"type": "integer", "default": 60},
                    "owner": {"type": "string", "description": "Defaults to you. 'household' for everyone's; else 'me', a user id, or a users.name."},
                },
            },
            handler=tasks_duplicates_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="tasks_similar",
            description=(
                "The open tasks most like one task: semantic neighbours from its "
                "stored vector plus near-identical titles. For when you know a "
                "duplicate exists and tasks_duplicates has not paired it. Pairs "
                "ruled distinct are excluded. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "limit": {"type": "integer", "default": 8},
                    "owner": {"type": "string", "description": "Defaults to you. 'household' for everyone's; else 'me', a user id, or a users.name."},
                },
                "required": ["uid"],
            },
            handler=tasks_similar_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="tasks_merge",
            description=(
                "Resolve a duplicate pair. action=merge folds `drop` into `keep`: "
                "its text is appended to keep's description, tasks that waited on "
                "or were part of it are re-pointed, the stronger queue wins, and "
                "it is marked dropped with a `duplicates` link back. Optionally "
                "give keep a new title. action=distinct records that the two are "
                "NOT duplicates so tasks_duplicates stops pairing them. "
                "Re-renders Task Backlog.md."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["merge", "distinct"], "default": "merge"},
                    "keep": {"type": "string", "description": "uid of the task that survives."},
                    "drop": {"type": "string", "description": "uid of the task folded into it."},
                    "title": {"type": "string", "description": "merge only: a better title for the kept task."},
                },
                "required": ["keep", "drop"],
            },
            handler=tasks_merge_handler,
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
        ).build(),
    ]
