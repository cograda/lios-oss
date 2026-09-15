"""intake — the deterministic new-task pass (S5, 2026-09-07).

Splits new-task *creation* from backlog *review*. Today's `/triage` hands raw
mail/WhatsApp/reminders straight to a model; this instead does the
deterministic half server-side — find what's new since a per-user marker,
match each item against the caller's own open tasks — so a model (or a
person) only has to judge the residue that genuinely looks new.
See `vault/Projects/lios/Plans/Daily Kickoff — Rebuilding Daily Note as a
Morning Ritual.md`'s "Review" section, point 1, for the design decision this
implements.

## Matching costs nothing

Mail and WhatsApp messages already carry embeddings in the unified
`embeddings` table (`source="email"`/`"whatsapp"`, keyed by the platform's own
message id, `user_id` = the owning caller — see `google_mail/sync.py::
embed_messages` and `whatsapp/sync.py::embed_messages`). Open tasks carry
`source="task"` embeddings (`dupes.py`). So matching a candidate against the
backlog is a `cosine_distance` join between two rows already in Postgres —
the same "linear algebra, not an API call" rule `EmbeddingService.similar_to`
is built on — and this module never imports an embedding provider or calls
one. A candidate whose own message has no stored vector yet (the 5-minute
queue processor hasn't reached it) is reported `unindexed`, honestly, rather
than folded into `new`: an empty match list from a half-built index is not
the same claim as "nothing matches", the same distinction `tasks_duplicates`
makes for the same reason.

**WhatsApp is embedded by conversation *segment*, not by message**
(`{chat_id}:{start_epoch}:{end_epoch}` — see `whatsapp/sync.py::_segment_id`),
so a single candidate message has no embedding row of its own to look up by
its own id. This module resolves each WhatsApp candidate to the segment
embedding that contains it — same chat_id, message timestamp within
`[start, end]` inclusive — in one pass over the caller's own segments per
call (`_load_whatsapp_segments`), never one query per message, and matches
through *that* row instead. A message whose segment hasn't been embedded
yet (too few messages so far, or still `pending` in the queue) reports
`unindexed`, honestly, same as mail's own gap below. Self-note segments
that were split into `:pN` parts for length are not resolved into (each is
still a single message with `start == end`, so the coverage lost is small,
and the id shape is ambiguous enough not to be worth the parsing).

⚠️ Mail's own gap, unrelated to the above: a mail candidate's match is
looked up by its own `google_message_id`, which only ever exists in the
`embeddings` table for mail this codebase itself embedded — see
`google_mail/sync.py::enqueue_new_mail`.

## The threshold

`intake.match_threshold` (`manifest.py`, default 0.78) was **measured on
production on 2026-09-07**, not constructed. With gemini-embedding-2 the noise
floor is high: the best cosine between an *unrelated* WhatsApp segment or mail
and any open task sat at median 0.67, p90 0.71, max 0.77 (n=250), while
genuinely related pairs — an open task against its nearest other task, or a
vault note against the tasks it names — sat at median 0.77, p90 0.83, max
0.93. The first shipped default of 0.55 (constructed from an assumed 0.1–0.3
"unrelated" floor) classified 140 of 175 real candidates as `matched`, which
would have hidden genuine new tasks behind chance resemblance. 0.78 sits just
above the measured noise maximum; expect to tune it *up* rather than down.
`tasks_duplicates` uses 0.80 for task-vs-task; this is deliberately close to
it now that the register difference turned out not to lower real matches.

## Task-likeness pre-filter (lios#151) and the inbox source (lios#159)

Before this, `tasks_intake_candidates` surfaced every message in the window,
not just the ones that could plausibly be a task — measured 2026-09-07 over a
two-day window: 297 candidates, 108 under 20 characters, 27 with no text at
all (media/reactions, e.g. a thumbs-up), and 0 actual tasks in a random
sample of 25. `_looks_task_like_whatsapp`/`_looks_task_like_mail` below are a
**deterministic** tier-1 filter only — own-outgoing/empty/too-short/pure-
emoji WhatsApp, and promotional-category/no-reply mail — on by default,
switched off with `include_all: true`, with the drop count reported as
`counts.filtered` either way. There is deliberately no tier-2 model judgement
here (a `stt.memo`-style AI role doing "is this actually a task?"): the
brief for this change was to keep the pass deterministic unless a tool
argument asked for a model call, and none does. See this PR's description
for that tier-2 proposal instead of code — it belongs in a design
conversation, not a silent addition to a tool whose docstring's whole point
is "no embedding API call, so this is free and fast".

`inbox` joins `mail`/`whatsapp`/`reminders` as a fourth source (lios#159):
deliberately captured items still sitting in a person's own pending inbox
queue (voice memos, Shortcuts text captures, described photos, WhatsApp
self-notes already routed there by `inbox_route_whatsapp_notes`) are the
highest-signal source intake has — a 2026-09-08 kickoff triaged 80 mail/
WhatsApp candidates while a fully-transcribed, actionable voice memo sat in
`inbox_pending` and was never offered as a task. Inbox candidates always
report `match_status: "unindexed"` — there's no `_EMBEDDING_SOURCE_FOR` entry
for `"inbox"`, same as `reminders` — because `inbox.query`'s facade only ever
returns items still in a *pending* bucket (`scan.PENDING_BUCKETS`); an item
already routed into the vault or corpus has moved to a terminal bucket and
is no longer returned here at all, which is what "already ingested" means
for this source in practice, so there is nothing to look an embedding up by.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.integrations.tasks.dupes import EMBEDDING_SOURCE as TASK_EMBEDDING_SOURCE
from app.integrations.tasks.dupes import OPEN_STATUSES
from app.integrations.tasks.models import IntakeMarker, Task
from app.plugin.capabilities import get_capability
from app.plugin.config_store import plugin_config
from app.tools import CustomTool, ToolAnnotations
from app.tools.base import parse_iso_date

logger = logging.getLogger(__name__)

ALL_SOURCES = ("mail", "whatsapp", "reminders", "inbox")
# {candidate source name -> embedding "source" column value}. Reminders and
# inbox have no embedding_sources at all (apple_reminders/manifest.py has
# none; inbox candidates are, by construction, never-yet-ingested pending
# items — see the module docstring) — absent here on purpose, so
# `_match_for` is never even attempted for either.
_EMBEDDING_SOURCE_FOR = {"mail": "email", "whatsapp": "whatsapp"}
DEFAULT_MATCH_THRESHOLD = 0.78
DEFAULT_LOOKBACK_HOURS = 24
MAX_SINCE_DAYS = 30
DEFAULT_LIMIT = 200
MAX_LIMIT = 500
_MAIL_PAGE = 500
_WHATSAPP_PAGE = 100
_MATCHES_PER_CANDIDATE = 3

# Task-likeness pre-filter (lios#151) — see the module docstring's section
# above for the measurement this was set from.
MIN_TASK_LIKE_CHARS = 20
_PROMOTIONAL_MAIL_CATEGORIES = frozenset({
    "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS",
})
_NOREPLY_SENDER_RE = re.compile(r"no.?reply", re.IGNORECASE)


# ─── fetch: paged, oldest-first within the window ──────────────────────────


def _paged_fetch(method, session: Session, since: datetime, until: datetime, cap: int, page_size: int) -> list[dict]:
    """Walk `method` (a facade's `recent(session, args)`, newest-first,
    `after`/`before`/`limit`) backwards from `until` to `since`, accumulating
    pages until either the window is exhausted or `cap` items are collected.

    Ordinary facade tools cap a single call at their own `max_limit` (100 for
    WhatsApp, 500 for mail) — this is the "paged fetch... no display cap
    other than `limit`" the tool contracts for: as long as the window holds
    more, another page is fetched, walking `before` back to just past the
    oldest item already collected so nothing is double-counted.
    """
    collected: list[dict] = []
    cur_before = until
    while len(collected) < cap:
        args = {"after": since.isoformat(), "before": cur_before.isoformat(), "limit": page_size}
        try:
            rows = json.loads(method(session, args))
        except Exception as e:  # noqa: BLE001 — one bad page must not fail the whole tool
            logger.warning("tasks_intake: paged fetch failed: %s", e)
            break
        if not rows:
            break
        collected.extend(rows)
        if len(rows) < page_size:
            break  # short page: the window is exhausted
        oldest = parse_iso_date(rows[-1].get("date"))
        if oldest is None or oldest <= since:
            break
        cur_before = oldest - timedelta(microseconds=1)
    return collected[:cap]


def _fetch_mail(session: Session, since: datetime, until: datetime, cap: int) -> list[dict]:
    facade = get_capability("mail.query")
    rows = _paged_fetch(facade.recent, session, since, until, cap, _MAIL_PAGE)
    return [
        {
            "source": "mail", "ref": r.get("id"), "date": r.get("date"),
            "sender": r.get("from"), "chat_or_subject": r.get("subject"),
            "text": r.get("snippet") or "",
        }
        for r in rows
    ]


def _fetch_whatsapp(session: Session, since: datetime, until: datetime, cap: int) -> list[dict]:
    facade = get_capability("whatsapp.query")
    rows = _paged_fetch(facade.recent, session, since, until, cap, _WHATSAPP_PAGE)
    # Bug fix (lios#151): `chat_name` (from `whatsapp_recent`'s `_msg_to_dict`)
    # is populated by the bridge for groups but is routinely null for 1:1
    # chats, so the old fallback showed the raw chat_id — a JID — rather
    # than a name a person recognises. One batched lookup of the caller's
    # own contacts covers the whole page; `chat_name` stays the next
    # fallback (a group whose display name changed since the contact row was
    # last synced), then the raw id as the last resort.
    contact_names = facade.contact_names(session, user_id=current_user_id())
    return [
        {
            "source": "whatsapp", "ref": r.get("message_id"), "date": r.get("date"),
            "sender": r.get("sender"),
            "chat_or_subject": contact_names.get(r.get("chat_id")) or r.get("chat_name") or r.get("chat_id"),
            "text": r.get("body") or r.get("caption") or "",
            # Internal only — used for the task-likeness filter and to
            # resolve the message's segment below, never copied into the
            # candidate dict `_build_candidate` emits.
            "chat_id": r.get("chat_id"),
            "is_from_me": bool(r.get("is_from_me")),
        }
        for r in rows
    ]


def _fetch_inbox(session: Session, since: datetime, until: datetime, cap: int) -> list[dict]:
    """New pending inbox captures since `since` (lios#159) — voice memos,
    Shortcuts text, described photos, routed WhatsApp self-notes. See the
    module docstring for why `match_status` is always `unindexed` for this
    source.

    `inbox.query` has no cursor/paging of its own (`InboxFacade.
    pending_candidates` returns the caller's whole pending queue, which in
    practice is small — a backlog measured in tens, not the tens of
    thousands mail/WhatsApp windows can hold), so this filters and caps
    in-process rather than paging like `_fetch_mail`/`_fetch_whatsapp` do.
    """
    try:
        items = get_capability("inbox.query").pending_candidates(current_user_id())
    except Exception as e:  # noqa: BLE001 — one bad source must not fail the whole tool
        logger.warning("tasks_intake: inbox fetch failed: %s", e)
        return []

    out: list[dict] = []
    for it in items:
        date = it.get("modified_at")
        ts = parse_iso_date(date) if date else None
        if ts is None or not (since <= ts <= until):
            continue
        out.append({
            "source": "inbox", "ref": it.get("path"), "date": date,
            # The capture source (e.g. "voicememo"/"shortcut") is more useful
            # here than a filename — issue #159's proposal.
            "sender": it.get("source") or it.get("bucket"),
            "chat_or_subject": it.get("bucket"),
            "text": it.get("note") or it.get("preview") or "",
        })
    out.sort(key=lambda r: r["date"] or "")
    return out[:cap]


def _fetch_reminders(session: Session, since: datetime) -> list[dict]:
    """New reminders since `since` — `added_since` from the same `sync`
    payload `/daily-note` uses (`apple_reminders/tools.py::
    handle_sync_reminders`). Unbounded by construction (no display cap in
    that facade call), so no paging is needed here.

    ⚠️ The facade's row shape has no timestamp field, so `date` is reported
    `None` for every reminder candidate — `created_at` exists on the model
    but is not part of the facade's public dict. Sorted last among same-
    window candidates as a result (see the handler's sort key).
    """
    try:
        facade = get_capability("reminders.query")
        data = json.loads(facade.sync(session, {"since": since.isoformat()}))
    except Exception as e:  # noqa: BLE001
        logger.warning("tasks_intake: reminders fetch failed: %s", e)
        return []
    return [
        {
            "source": "reminders", "ref": r.get("uid"), "date": None,
            "sender": None, "chat_or_subject": r.get("list"),
            "text": r.get("summary") or "",
        }
        for r in data.get("added_since", [])
    ]


# ─── WhatsApp: resolve a message to its conversation-segment embedding ─────


def _parse_whatsapp_segment_id(source_id: str) -> tuple[str, int, int] | None:
    """(chat_id, start_epoch, end_epoch) from a plain WhatsApp segment
    `source_id` (`whatsapp/sync.py::_segment_id`'s `{chat_id}:{start}:{end}`
    shape), or `None` if it doesn't parse that way.

    A self-note chunk split for length carries a trailing `:pN`
    (`_self_note_chunks`) and is deliberately skipped here rather than
    parsed around: those segments are always a single message with
    `start == end`, so resolving into them buys little, and the id shape is
    ambiguous enough (is a fourth colon-part a part number or a chat_id that
    itself contains a colon?) not to be worth it.
    """
    parts = source_id.rsplit(":", 2)
    if len(parts) != 3:
        return None
    chat_id, start_s, end_s = parts
    try:
        return chat_id, int(start_s), int(end_s)
    except ValueError:
        return None


def _load_whatsapp_segments(session: Session, owner_id: int) -> dict[str, list[tuple[int, int, str]]]:
    """`{chat_id: [(start_epoch, end_epoch, source_id), ...]}` for every
    WhatsApp segment embedding this caller owns — one query for the whole
    batch of candidates, so resolving each message to its segment is an
    in-memory range lookup rather than a query per message.
    """
    from app.services.embedding import Embedding

    rows = (
        session.query(Embedding.source_id)
        .filter(Embedding.source == "whatsapp", Embedding.user_id == owner_id)
        .all()
    )
    out: dict[str, list[tuple[int, int, str]]] = {}
    for (source_id,) in rows:
        parsed = _parse_whatsapp_segment_id(source_id)
        if parsed is None:
            continue
        chat_id, start, end = parsed
        out.setdefault(chat_id, []).append((start, end, source_id))
    return out


def _segment_ref_for_message(raw: dict, segments_by_chat: dict[str, list[tuple[int, int, str]]]) -> str | None:
    """The segment `source_id` covering this WhatsApp candidate's timestamp,
    or `None` if none does (not yet embedded, or genuinely outside any
    segment). Inclusive on both ends — a message exactly on a segment
    boundary belongs to that segment (`_segment_id` sets `start`/`end` from
    the first/last message's own timestamps, so a boundary message is
    always a real member, never an edge case to exclude).
    """
    chat_id = raw.get("chat_id")
    date = raw.get("date")
    if not chat_id or not date:
        return None
    ts = parse_iso_date(date)
    if ts is None:
        return None
    epoch = int(ts.timestamp())
    for start, end, source_id in segments_by_chat.get(chat_id, []):
        if start <= epoch <= end:
            return source_id
    return None


# ─── match: pure DB linear algebra, never an embedding call ────────────────


def _match_for(session: Session, embedding_source: str, ref: str, owner_id: int, threshold: float) -> list[dict] | None:
    """Top matches for one candidate's own stored embedding, or `None` if the
    candidate has no vector in any active space yet ("unindexed").

    Mirrors `EmbeddingService.similar_to`'s "first configured space wins"
    rule (comparing across spaces is meaningless), but joins the candidate's
    OWN source against `task` embeddings in the SAME space rather than
    against more of its own source — `similar_to` has no cross-source mode.
    """
    from app.services.embedding import Embedding, _active_spaces

    for _provider, vec_model in _active_spaces():
        seed = (
            session.query(Embedding.id)
            .filter(
                Embedding.source == embedding_source,
                Embedding.source_id == ref,
                Embedding.user_id == owner_id,
            )
            .first()
        )
        if seed is None:
            continue
        seed_vec = (
            session.query(vec_model.embedding)
            .filter(vec_model.embedding_id == seed.id)
            .scalar()
        )
        if seed_vec is None:
            continue

        rows = (
            session.query(
                Task.uid, Task.title, Task.status,
                vec_model.embedding.cosine_distance(seed_vec).label("distance"),
            )
            .join(Embedding, (Embedding.source == TASK_EMBEDDING_SOURCE) & (Embedding.source_id == Task.uid))
            .join(vec_model, vec_model.embedding_id == Embedding.id)
            .filter(Task.status.in_(OPEN_STATUSES), Task.routine_id.is_(None))
            .filter(or_(Task.owner_id == owner_id, Task.owner_id.is_(None)))
            .order_by("distance")
            .limit(20)
            .all()
        )
        matches = [
            {"uid": uid, "title": title, "status": status, "score": round(1 - distance, 4)}
            for uid, title, status, distance in rows
            if (1 - distance) >= threshold
        ]
        matches.sort(key=lambda m: -m["score"])
        return matches[:_MATCHES_PER_CANDIDATE]

    return None


# ─── task-likeness pre-filter (lios#151) — deterministic, tier 1 only ──────


def _looks_task_like_whatsapp(raw: dict) -> bool:
    """Drop the mechanical noise a WhatsApp window is mostly made of, before
    a person (or a model) has to read it. Not a task-likeness *judgement* —
    see the module docstring for why that stays out of this deterministic
    pass. Groups are NOT excluded here: a task can land in a group chat same
    as a 1:1, and `whatsapp_recent`'s own `include_groups` (default true)
    already governs whether group messages are fetched at all.
    """
    if raw.get("is_from_me"):
        return False  # your own outgoing message is never a task FOR you
    text = (raw.get("text") or "").strip()
    if len(text) < MIN_TASK_LIKE_CHARS:
        return False  # covers both "empty" (media/reaction) and "too short"
    if not any(ch.isalnum() for ch in text):
        return False  # pure emoji/punctuation
    return True


def _looks_task_like_mail(raw: dict, labels: str) -> bool:
    """Mail half of the same tier-1 filter. `labels` is the raw comma-
    separated Gmail label-id string from `GoogleMailFacade.labels_for`
    (empty string if this codebase hasn't synced/cached labels for the
    message yet — treated as "keep", since dropping on absent data would
    silently hide a candidate rather than a genuine promo)."""
    cats = set(labels.split(",")) if labels else set()
    if cats & _PROMOTIONAL_MAIL_CATEGORIES:
        return False
    if _NOREPLY_SENDER_RE.search(raw.get("sender") or ""):
        return False
    return True


def _filter_task_like(session: Session, raw: list[dict], owner_id: int) -> tuple[list[dict], int]:
    """Apply the tier-1 filter to mail/WhatsApp candidates; reminders and
    inbox candidates are already a deliberate capture/action, so they pass
    through unfiltered. Returns `(kept, dropped_count)`.

    Mail labels are fetched in one batched call for the whole page (never
    one query per candidate) — the same batching shape as
    `_load_whatsapp_segments`.
    """
    mail_ids = [r["ref"] for r in raw if r["source"] == "mail" and r.get("ref")]
    mail_labels: dict[str, str] = {}
    if mail_ids:
        mail_labels = get_capability("mail.query").labels_for(session, mail_ids, user_id=owner_id)

    kept: list[dict] = []
    dropped = 0
    for r in raw:
        source = r["source"]
        if source == "whatsapp":
            keep = _looks_task_like_whatsapp(r)
        elif source == "mail":
            keep = _looks_task_like_mail(r, mail_labels.get(r["ref"], ""))
        else:
            keep = True
        if keep:
            kept.append(r)
        else:
            dropped += 1
    return kept, dropped


def _build_candidate(
    session: Session, raw: dict, owner_id: int, threshold: float,
    whatsapp_segments: dict[str, list[tuple[int, int, str]]] | None = None,
) -> dict:
    source = raw["source"]
    embedding_source = _EMBEDDING_SOURCE_FOR.get(source)
    matches: list[dict] | None = None
    if embedding_source is not None:
        lookup_ref = raw["ref"]
        if source == "whatsapp":
            lookup_ref = _segment_ref_for_message(raw, whatsapp_segments or {})
        if lookup_ref is not None:
            matches = _match_for(session, embedding_source, lookup_ref, owner_id, threshold)

    if matches is None:
        match_status, matches = "unindexed", []
    elif matches:
        match_status = "matched"
    else:
        match_status = "new"

    return {
        "source": source,
        "ref": raw["ref"],
        "date": raw["date"],
        "sender": raw["sender"],
        "chat_or_subject": raw["chat_or_subject"],
        "text": (raw.get("text") or "")[:500],
        "matches": matches,
        "match_status": match_status,
    }


# ─── handlers ──────────────────────────────────────────────────────────────


def tasks_intake_candidates_handler(session: Session, args: dict) -> str:
    uid = current_user_id()
    now = datetime.now(timezone.utc)

    since_arg = args.get("since")
    marker_used = False
    if since_arg:
        since = parse_iso_date(since_arg)
        if since is None:
            raise ValueError(f"invalid 'since': {since_arg!r}")
    else:
        marker = session.query(IntakeMarker).filter_by(user_id=uid).one_or_none()
        if marker is not None:
            since = marker.seen_until
            marker_used = True
        else:
            since = now - timedelta(hours=DEFAULT_LOOKBACK_HOURS)

    if since < now - timedelta(days=MAX_SINCE_DAYS):
        raise ValueError(f"'since' cannot be more than {MAX_SINCE_DAYS} days ago (got {since.isoformat()})")

    until_arg = args.get("until")
    until = parse_iso_date(until_arg) if until_arg else now
    if until is None:
        raise ValueError(f"invalid 'until': {until_arg!r}")

    limit = min(int(args.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)

    sources = args.get("sources") or list(ALL_SOURCES)
    unknown = sorted(set(sources) - set(ALL_SOURCES))
    if unknown:
        raise ValueError(f"unknown sources: {unknown}")

    cfg = plugin_config("tasks")
    threshold = float(getattr(cfg, "intake_match_threshold", None) or DEFAULT_MATCH_THRESHOLD)

    raw: list[dict] = []
    if "mail" in sources:
        raw.extend(_fetch_mail(session, since, until, limit))
    if "whatsapp" in sources:
        raw.extend(_fetch_whatsapp(session, since, until, limit))
    if "reminders" in sources:
        raw.extend(_fetch_reminders(session, since))
    if "inbox" in sources:
        raw.extend(_fetch_inbox(session, since, until, limit))

    # Task-likeness pre-filter (lios#151), on by default — filtered BEFORE
    # the oldest-first `limit` slice below, so noise dropped here doesn't
    # eat into the budget of genuine candidates the window could offer.
    include_all = bool(args.get("include_all", False))
    filtered_count = 0
    if not include_all:
        raw, filtered_count = _filter_task_like(session, raw, uid)

    # Oldest-first, `limit` the only cap — reminders (no date) sort first,
    # ahead of anything dated, rather than being silently dropped.
    raw.sort(key=lambda r: r["date"] or "")
    raw = raw[:limit]

    whatsapp_segments = _load_whatsapp_segments(session, uid) if "whatsapp" in sources else {}
    candidates = [_build_candidate(session, r, uid, threshold, whatsapp_segments) for r in raw]

    counts: dict[str, Any] = {
        "new": 0, "matched": 0, "unindexed": 0, "by_source": {}, "filtered": filtered_count,
    }
    for c in candidates:
        counts[c["match_status"]] += 1
        counts["by_source"][c["source"]] = counts["by_source"].get(c["source"], 0) + 1

    return json.dumps({
        "since": since.isoformat(),
        "until": until.isoformat(),
        "marker_used": marker_used,
        "counts": counts,
        "candidates": candidates,
    })


def tasks_intake_mark_handler(session: Session, args: dict) -> str:
    uid = current_user_id()
    until_arg = args.get("until")
    until = parse_iso_date(until_arg) if until_arg else datetime.now(timezone.utc)
    if until is None:
        raise ValueError(f"invalid 'until': {until_arg!r}")

    marker = session.query(IntakeMarker).filter_by(user_id=uid).one_or_none()
    old = marker.seen_until.isoformat() if marker is not None else None
    if marker is None:
        session.add(IntakeMarker(user_id=uid, seen_until=until))
    else:
        marker.seen_until = until
    session.commit()
    return json.dumps({"old": old, "new": until.isoformat()})


def intake_tools() -> list[dict]:
    return [
        CustomTool(
            name="tasks_intake_candidates",
            description=(
                "intake — deterministic new-task pass. Returns the caller's NEW "
                "mail/WhatsApp/reminder/inbox items since a per-user marker "
                "(default) or an explicit `since`, each already matched against "
                "the caller's own open tasks by stored-embedding similarity — no "
                "embedding API call, so this is free and fast. Mail matches "
                "through its own message embedding; WhatsApp matches through "
                "the conversation segment its message falls inside; inbox items "
                "always report `unindexed` (they're pending captures, never yet "
                "ingested). Either mail or WhatsApp can also report `unindexed` "
                "if the embedding pipeline hasn't reached it yet. The model "
                "should judge only `new`/`unindexed` candidates; a `matched` one "
                "already has a likely home in the backlog. By default a "
                "deterministic task-likeness filter drops WhatsApp noise (own "
                "outgoing, empty/media-only, under 20 chars, pure emoji) and "
                "promotional/no-reply mail before matching even runs — "
                "`counts.filtered` reports how many; pass `include_all: true` to "
                "see everything in the window instead. `/tunetasks` reviews the "
                "existing backlog; this tool does not — it only surfaces what's "
                "new since last time. Call tasks_intake_mark once candidates "
                "have been confirmed or rejected to advance the marker."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": (
                            "ISO timestamp. Default: the caller's own marker; "
                            "if none, 24h ago. Refused if more than 30 days ago."
                        ),
                    },
                    "until": {"type": "string", "description": "ISO timestamp. Default: now."},
                    "limit": {
                        "type": ["integer", "string"], "default": DEFAULT_LIMIT,
                        "description": f"Max candidates returned, oldest-first (max {MAX_LIMIT}).",
                    },
                    "sources": {
                        "type": "array", "items": {"type": "string", "enum": list(ALL_SOURCES)},
                        "description": "Subset of mail/whatsapp/reminders/inbox. Default: all four.",
                    },
                    "include_all": {
                        "type": "boolean", "default": False,
                        "description": (
                            "Skip the deterministic task-likeness filter and return "
                            "every candidate in the window, not just the ones that "
                            "look task-like. `counts.filtered` is 0 when this is set."
                        ),
                    },
                },
            },
            handler=tasks_intake_candidates_handler,
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        ).build(),
        CustomTool(
            name="tasks_intake_mark",
            description=(
                "intake — advance the caller's marker to `until` (default now), "
                "so the next tasks_intake_candidates call starts from here. Call "
                "only after this run's candidates have actually been confirmed "
                "or rejected — advancing early skips items nobody judged."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "until": {"type": "string", "description": "ISO timestamp. Default: now."},
                },
            },
            handler=tasks_intake_mark_handler,
            annotations=ToolAnnotations(idempotent_hint=True),
        ).build(),
    ]
